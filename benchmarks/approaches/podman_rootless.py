"""
Approach: Podman rootless with per-user isolation.

Each agent gets its own unprivileged host user with a dedicated Podman socket.
Workers are sibling containers spawned via that user's socket. Agents are
isolated from each other because each user's Podman instance has its own
container namespace — user A cannot see or touch user B's containers.

No persistent daemon runs. Podman's API is socket-activated (transient
`podman system service` started on demand by systemd).
"""

import subprocess
import time
import json
import urllib.request
from pathlib import Path
from typing import Dict, List

from approaches.base import Approach, BenchmarkConfig

USER_PREFIX = "bench-pm-"
BASE_UID = 4000
SOCKET_TEMPLATE = "/run/user/{uid}/podman/podman.sock"
PROXY_SOCKET_TEMPLATE = "/home/{user}/.podman-proxy.sock"
PROXY_SCRIPT = Path(__file__).resolve().parent.parent / "workload" / "podman_proxy.py"
PROXY_STARTUP_TIMEOUT_S = 5
def _run_as_user(user: str, cmd: List[str],
                 capture: bool = True,
                 stdin=None) -> subprocess.CompletedProcess:
    """Run a short-lived command inside the user's full systemd scope.

    ``sudo -u`` does NOT work reliably with rootless Podman (missing
    XDG_RUNTIME_DIR, no D-Bus session).  ``systemd-run --machine=`` enters
    the user's scope with the correct environment.

    Uses ``--pipe --wait`` so stdout/stderr are captured and we block
    until the command exits.  **Do not use for commands that spawn
    long-running child processes** (e.g. ``podman start``) — use
    :func:`_fire_as_user` instead.

    When no explicit ``stdin`` is passed we use ``DEVNULL`` to prevent
    ``systemd-run --pipe`` from blocking on an open stdin handle.
    """
    full_cmd = [
        "sudo", "systemd-run",
        f"--machine={user}@",
        "--quiet", "--user", "--collect", "--pipe", "--wait",
    ] + cmd
    if stdin is None:
        stdin = subprocess.DEVNULL
    return subprocess.run(full_cmd, capture_output=capture, text=True,
                          stdin=stdin)


def _fire_as_user(user: str, cmd: List[str]) -> subprocess.CompletedProcess:
    """Fire-and-forget a command inside the user's systemd scope.

    Unlike :func:`_run_as_user` this omits ``--pipe`` and ``--wait`` so
    the transient unit is dispatched and we return immediately.  Use this
    for commands that spawn persistent processes (e.g. ``podman start``)
    where ``--wait`` would block until the container exits.

    ``RemainAfterExit=yes`` keeps the transient service unit in
    *active (exited)* state after the main process (e.g. ``podman start``)
    exits.  Without it, systemd considers the unit done, sends SIGTERM to
    remaining cgroup members (conmon), and after ``DefaultTimeoutStopSec``
    (90 s) sends SIGKILL — killing the agent container.
    """
    full_cmd = [
        "sudo", "systemd-run",
        f"--machine={user}@",
        "--quiet", "--user", "--collect",
        "--property=RemainAfterExit=yes",
    ] + cmd
    return subprocess.run(full_cmd, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)


def _load_docker_image_to_user(user: str, image: str) -> None:
    """Pipe a Docker image into a user's rootless Podman store.

    Uses ``docker save <image> | podman load`` via piping to avoid OCI
    archive format incompatibilities between newer Docker (29+, OCI
    default) and older Podman (3.x, expects docker-archive from files).
    Streaming via stdin works with both formats.

    After loading, the image is tagged with ``localhost/<image>`` so that
    ``podman run <image>`` resolves correctly without needing unqualified
    search registries.
    """
    save_proc = subprocess.Popen(
        ["docker", "save", image],
        stdout=subprocess.PIPE,
    )
    load_result = _run_as_user(
        user, ["podman", "load"],
        capture=True, stdin=save_proc.stdout,
    )
    save_proc.stdout.close()
    save_rc = save_proc.wait()
    if save_rc != 0:
        raise RuntimeError(f"docker save {image} failed (rc={save_rc})")
    if load_result.returncode != 0:
        raise RuntimeError(
            f"podman load failed for {user}: {load_result.stderr.strip()}"
        )

    # Podman load may assign a different name (e.g. "localhost/latest:latest")
    # Parse the loaded name and re-tag to the expected name.
    loaded_name = None
    for line in (load_result.stdout or "").splitlines():
        # Podman 4.x outputs "Loaded image: ..." while older versions
        # use "Loaded image(s): ...".  Match both.
        if line.startswith("Loaded image"):
            loaded_name = line.split(":", 1)[1].strip()
            break

    target = f"localhost/{image}"
    if loaded_name and loaded_name != target:
        _run_as_user(user, ["podman", "tag", loaded_name, target])


def _start_proxy(user: str, socket_path: str, allowed_image: str) -> str:
    """Start the filtering API proxy for a user and return the proxy socket path.

    Copies the proxy script to the user's home directory, launches it via
    ``_fire_as_user``, and polls until the proxy socket appears.
    """
    proxy_socket = PROXY_SOCKET_TEMPLATE.format(user=user)
    home_dir = f"/home/{user}"
    dest = f"{home_dir}/podman_proxy.py"

    # Copy the proxy script into the user's home dir
    subprocess.run(
        ["sudo", "cp", str(PROXY_SCRIPT), dest],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["sudo", "chown", f"{user}:{user}", dest],
        capture_output=True, check=True,
    )

    # Launch the proxy as the user (fire-and-forget)
    _fire_as_user(user, [
        "python3", dest,
        "--listen", proxy_socket,
        "--upstream", socket_path,
        "--allowed-image", allowed_image,
    ])

    # Poll until the proxy socket appears
    deadline = time.monotonic() + PROXY_STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if Path(proxy_socket).exists():
            return proxy_socket
        time.sleep(0.1)

    raise RuntimeError(
        f"Proxy socket {proxy_socket} did not appear within "
        f"{PROXY_STARTUP_TIMEOUT_S}s for user {user}"
    )


class PodmanRootlessApproach(Approach):
    """Per-user rootless Podman isolation — no persistent daemon."""

    def __init__(self):
        self._agent_ids: List[str] = []
        self._users: List[str] = []
        self._host_ports: Dict[str, int] = {}
        self._agent_pids: Dict[str, int] = {}
        self._last_worker_counts: Dict[str, int] = {}
        self._agent_image = "bench-agent:latest"
        self._run_id: str = "unknown"

    @property
    def name(self) -> str:
        return "podman-rootless"

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------

    def setup(self, config: BenchmarkConfig) -> None:
        self._run_id = config.run_id

        # Verify podman
        result = subprocess.run(
            ["podman", "--version"], capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "podman not found. Install with: "
                "sudo apt install podman uidmap systemd-container"
            )
        print(f"[{self.name}] podman: {result.stdout.strip()}")

        # Verify systemd-container (provides machinectl / systemd-run --machine)
        result = subprocess.run(
            ["machinectl", "--version"], capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "systemd-container not found. Install with: "
                "sudo apt install systemd-container"
            )

        # Verify Docker images exist (we'll docker-save them for podman load)
        for image in [self._agent_image, config.worker_image]:
            result = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Docker image '{image}' not found. Run 'make images' first."
                )

        print(f"[{self.name}] Setup complete. run_id={self._run_id}")

    # ------------------------------------------------------------------
    # start_agents
    # ------------------------------------------------------------------

    def start_agents(self, n: int, config: BenchmarkConfig) -> List[str]:
        self._agent_ids = []
        self._users = []
        self._host_ports = {}
        self._agent_pids = {}
        self._last_worker_counts = {}

        images_to_load = [self._agent_image, config.worker_image]

        for i in range(n):
            agent_id = f"agent-{i}"
            user = f"{USER_PREFIX}{i}"
            uid = BASE_UID + i
            container_name = f"bench-agent-{i}"
            socket_path = SOCKET_TEMPLATE.format(uid=uid)

            # 1. Create unprivileged user
            subprocess.run(
                ["sudo", "useradd", "--create-home", "--shell", "/bin/bash",
                 "--uid", str(uid), user],
                capture_output=True, text=True,
            )
            # Make home dir traversable so orchestrator can stat proxy socket
            subprocess.run(
                ["sudo", "chmod", "711", f"/home/{user}"],
                capture_output=True,
            )

            # 2. Enable linger so systemd user instance stays alive
            subprocess.run(
                ["sudo", "loginctl", "enable-linger", user],
                capture_output=True, text=True,
            )

            # 3. Start podman.socket for this user.
            #    Wait briefly for the user's systemd instance to become
            #    reachable via D-Bus after enable-linger — without this,
            #    rapid user creation can hit "Transport endpoint is not
            #    connected" when there are many agents.
            for attempt in range(5):
                r = _run_as_user(user, [
                    "systemctl", "--user", "enable", "--now", "podman.socket",
                ])
                if r.returncode == 0:
                    break
                time.sleep(2)
            if r.returncode != 0:
                raise RuntimeError(
                    f"Failed to start podman.socket for {user}: "
                    f"{r.stderr.strip()}"
                )

            # 4. Load images into this user's Podman store
            #    Pipe via stdin to avoid OCI/docker-archive format issues.
            for image in images_to_load:
                print(f"[{self.name}] Loading {image} into {user}...")
                _load_docker_image_to_user(user, image)

            # 4.5. Start filtering API proxy for this user
            allowed_image = f"localhost/{config.worker_image}"
            proxy_socket = _start_proxy(user, socket_path, allowed_image)
            print(f"[{self.name}] API proxy started for {user}: "
                  f"{proxy_socket}")

            # 5. Create agent container (does not start a process, safe
            #    to use with --pipe --wait).
            host_port = config.orchestrator_base_port + i
            create_cmd = [
                "podman", "create",
                "--name", container_name,
                "--memory", f"{config.agent_memory_mb}m",
                "--security-opt", "label=disable",
                # Publish orchestrator HTTP port
                "-p", f"{host_port}:8080",
                # Mount the filtering proxy socket (NOT the real Podman socket)
                "-v", f"{proxy_socket}:/run/podman/podman.sock",
                # Docker SDK will talk to Podman's compat API
                "-e", "DOCKER_HOST=unix:///run/podman/podman.sock",
                "-e", "WORKER_BACKEND=docker",
                # Standard config env vars
                "-e", f"AGENT_ID={agent_id}",
                "-e", f"BENCHMARK_MODE={config.benchmark_mode}",
                "-e", f"AGENT_BASELINE_MB={config.agent_baseline_mb}",
                "-e", f"SPAWN_INTERVAL_MEAN_S={config.spawn_interval_mean_s}",
                "-e", f"MAX_CONCURRENT_WORKERS={config.max_concurrent_workers}",
                "-e", f"BENCHMARK_DURATION_S={config.benchmark_duration_s}",
                "-e", f"WORKER_IMAGE=localhost/{config.worker_image}",
                "-e", f"WORKER_MEMORY_LIMIT_MB={config.worker_memory_limit_mb}",
                "-e", f"WORKER_MEMORY_MB={config.worker_memory_mb}",
                "-e", f"WORKER_DURATION_MIN_S={config.worker_duration_min_s}",
                "-e", f"WORKER_DURATION_MAX_S={config.worker_duration_max_s}",
                "-e", f"WORKER_LIFETIME_MODE={config.worker_lifetime_mode}",
                "-e", f"PLATEAU_WORKERS_PER_AGENT={config.plateau_workers_csv()}",
                "-e", f"PLATEAU_HOLD_S={config.plateau_hold_s}",
                "-e", f"PLATEAU_SETTLE_S={config.plateau_settle_s}",
                "-e", f"RNG_SEED={config.rng_seed}",
                "-e", f"BENCH_RUN_ID={config.run_id}",
                "-e", f"BENCH_APPROACH={self.name}",
                # Orchestrator networking
                # Workers share the agent's network namespace via
                # WORKER_NETWORK_MODE=container:<name>, so they
                # reach the agent at localhost:8080.
                "-e", "DOCKER_BRIDGE_GATEWAY=127.0.0.1",
                "-e", "ORCHESTRATOR_PORT=8080",
                "-e", f"WORKER_NETWORK_MODE=container:{container_name}",
            ]

            # Storage validation: shared Podman daemon needs host-path indirection
            if config.storage_validation:
                host_dir = f"/home/{user}/bench-workspaces"
                subprocess.run(
                    ["sudo", "mkdir", "-p", host_dir], check=True,
                )
                subprocess.run(
                    ["sudo", "chown", f"{user}:{user}", host_dir],
                    check=True,
                )
                create_cmd += [
                    "-v", f"{host_dir}:/tmp/bench-workspaces",
                    "-e", "STORAGE_VALIDATION=1",
                    "-e", "WORKSPACE_BASE=/tmp/bench-workspaces",
                    "-e", f"WORKSPACE_HOST_BASE={host_dir}",
                ]

            create_cmd += [
                # Labels for identification
                "--label", f"bench_run_id={config.run_id}",
                "--label", "bench_role=agent",
                "--label", f"bench_agent_id={agent_id}",
                "--label", f"bench_approach={self.name}",
                f"localhost/{self._agent_image}",
            ]
            r = _run_as_user(user, create_cmd)
            if r.returncode != 0:
                detail = (r.stderr or r.stdout or "").strip()
                raise RuntimeError(
                    f"Failed to create agent {agent_id} as {user}: {detail}"
                )

            # 6. Start the container.  Fire-and-forget because
            #    systemd-run --pipe --wait would block until the
            #    container exits (it tracks child processes in the scope).
            r = _fire_as_user(user, ["podman", "start", container_name])
            if r.returncode != 0:
                detail = (r.stderr or r.stdout or "").strip()
                raise RuntimeError(
                    f"Failed to start agent {agent_id} as {user}: {detail}"
                )

            self._agent_ids.append(agent_id)
            self._users.append(user)
            self._host_ports[agent_id] = host_port
            print(f"[{self.name}] Started {container_name} as user {user}")

        health_timeout_s = max(60, 5 * n)
        print(f"[{self.name}] Waiting for {n} agents to become healthy...")
        deadline = time.monotonic() + health_timeout_s
        ready = set()
        while time.monotonic() < deadline and len(ready) < n:
            for agent_id, port in self._host_ports.items():
                if agent_id in ready:
                    continue
                try:
                    resp = urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health", timeout=2
                    )
                    if resp.status == 200:
                        ready.add(agent_id)
                        pid = self._inspect_agent_pid(agent_id)
                        if pid is not None:
                            self._agent_pids[agent_id] = pid
                        print(f"[{self.name}] {agent_id} healthy")
                except Exception:
                    pass
            if len(ready) < n:
                time.sleep(2)

        if len(ready) < n:
            not_ready = set(self._host_ports) - ready
            raise RuntimeError(
                f"Agents not ready after {health_timeout_s}s: {sorted(not_ready)}"
            )

        print(f"[{self.name}] {n} agents started.")
        return list(self._agent_ids)

    # ------------------------------------------------------------------
    # monitoring
    # ------------------------------------------------------------------

    def _inspect_agent_pid(self, agent_id: str) -> int | None:
        try:
            index = self._agent_ids.index(agent_id)
        except ValueError:
            return None

        user = self._users[index]
        container_name = f"bench-agent-{index}"
        try:
            r = _run_as_user(
                user,
                ["podman", "inspect", "--format", "{{.State.Pid}}", container_name],
            )
            if r.returncode != 0:
                return None
            pid = int((r.stdout or "").strip())
            return pid if pid > 0 else None
        except (ValueError, subprocess.SubprocessError):
            return None

    def get_agent_pids(self) -> Dict[str, int]:
        if len(self._agent_pids) != len(self._agent_ids):
            for agent_id in self._agent_ids:
                if agent_id in self._agent_pids:
                    continue
                pid = self._inspect_agent_pid(agent_id)
                if pid is not None:
                    self._agent_pids[agent_id] = pid
        return dict(self._agent_pids)

    def get_daemon_pids(self) -> Dict[str, int]:
        # No persistent daemon — this is the point of rootless Podman.
        return {}

    def count_active_workers(self) -> int:
        total = 0
        for agent_id, port in self._host_ports.items():
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/status",
                    timeout=0.2,
                ) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode() or "{}")
                        count = int(data.get("active_workers", 0))
                        self._last_worker_counts[agent_id] = count
                        total += count
                        continue
            except Exception:
                pass
            total += self._last_worker_counts.get(agent_id, 0)
        return total

    def start_benchmark(self) -> None:
        for agent_id, port in self._host_ports.items():
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/control/start",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                resp = urllib.request.urlopen(req, timeout=5)
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
            except Exception as e:
                raise RuntimeError(
                    f"Failed to start benchmark on {agent_id}:{port}: {e}"
                ) from e

    # ------------------------------------------------------------------
    # logs
    # ------------------------------------------------------------------

    def collect_agent_logs(self, agent_ids: List[str], output_dir) -> None:
        output_dir = Path(output_dir)
        for i, (agent_id, user) in enumerate(
            zip(agent_ids, self._users)
        ):
            container_name = f"bench-agent-{i}"
            uid = BASE_UID + i
            log_file = output_dir / f"{agent_id}.jsonl"
            log_data = ""

            # Try systemd-run first (works when user session is active)
            try:
                r = _run_as_user(user, ["podman", "logs", container_name])
                log_data = r.stdout or ""
                if not log_data.strip() and r.stderr:
                    log_data = r.stderr
            except subprocess.SubprocessError:
                pass

            # Fallback: su - user (login shell with full environment).
            # systemd-run --pipe can return empty for exited containers
            # when the user's systemd instance has gone idle.
            if not log_data.strip():
                try:
                    r = subprocess.run(
                        ["sudo", "su", "-", user, "-c",
                         f"podman logs {container_name}"],
                        capture_output=True, text=True,
                        stdin=subprocess.DEVNULL,
                    )
                    log_data = r.stdout or ""
                except subprocess.SubprocessError:
                    pass

            if log_data.strip():
                log_file.write_text(log_data)
                print(f"[{self.name}] Collected logs for {agent_id}"
                      f" ({len(log_data.splitlines())} lines)")
            else:
                print(f"[{self.name}] No logs for {agent_id}")

    # ------------------------------------------------------------------
    # teardown
    # ------------------------------------------------------------------

    def stop_agents(self) -> None:
        """Stop agent containers (keeps them for log collection)."""
        for i, user in enumerate(self._users):
            container_name = f"bench-agent-{i}"
            _run_as_user(user, ["podman", "stop", "-t", "30", container_name])
        print(f"[{self.name}] All agents stopped.")

    def remove_containers(self) -> None:
        """Remove all containers and stop Podman sockets."""
        for user in self._users:
            _run_as_user(user, ["podman", "rm", "-f", "--all"])
            _run_as_user(user, [
                "systemctl", "--user", "stop", "podman.socket",
            ])
        self._agent_ids = []
        self._host_ports = {}
        self._agent_pids = {}
        self._last_worker_counts = {}
        print(f"[{self.name}] All containers removed.")

    def cleanup(self) -> None:
        if self._agent_ids:
            self.stop_agents()
            self.remove_containers()

        # Disable linger, terminate user sessions, and remove users.
        for user in self._users:
            subprocess.run(
                ["sudo", "loginctl", "disable-linger", user],
                capture_output=True,
            )
            subprocess.run(
                ["sudo", "loginctl", "terminate-user", user],
                capture_output=True,
            )
            subprocess.run(
                ["sudo", "userdel", "--force", "--remove", user],
                capture_output=True,
            )

        self._users = []
        self._host_ports = {}
        self._agent_pids = {}
        self._last_worker_counts = {}

        print(f"[{self.name}] Cleanup complete (users removed).")
