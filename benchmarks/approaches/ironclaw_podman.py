"""
Approach: Real IronClaw on Podman rootless.

Each agent gets its own unprivileged host user with a Podman socket.
The Podman Docker-compat API socket is mounted directly into the agent
container. Ironclaw's native ``SANDBOX_PODMAN_COMPAT`` mode handles
Podman-specific container config differences (SecurityOpt format,
no cgroup resource limits, no AutoRemove). Images use the ``localhost/``
prefix required by Podman for locally-loaded images.

The workspace is at a host-visible path (``/home/{user}/workspace``)
shared between the agent container and sandbox containers so that
bind mounts work correctly from Podman's host-side perspective.

Each user's Podman namespace is isolated — agents cannot see each other's
containers.
"""

import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Dict, List

from approaches.base import Approach, BenchmarkConfig
from approaches._ironclaw_helpers import (
    GATEWAY_AUTH_TOKEN,
    IRONCLAW_AGENT_IMAGE,
    IRONCLAW_SANDBOX_IMAGE,
    ironclaw_agent_env,
    wait_for_gateway,
    trigger_worker_spawn,
)

USER_PREFIX = "bench-ic-pm-"
BASE_UID = 5000
SOCKET_TEMPLATE = "/run/user/{uid}/podman/podman.sock"


def _run_as_user(user, cmd, capture=True, stdin=None):
    full_cmd = [
        "sudo", "systemd-run",
        f"--machine={user}@",
        "--quiet", "--user", "--collect", "--pipe", "--wait",
    ] + cmd
    if stdin is None:
        stdin = subprocess.DEVNULL
    return subprocess.run(full_cmd, capture_output=capture, text=True, stdin=stdin)


def _fire_as_user(user, cmd):
    full_cmd = [
        "sudo", "systemd-run",
        f"--machine={user}@",
        "--quiet", "--user", "--collect",
        "--property=RemainAfterExit=yes",
    ] + cmd
    return subprocess.run(full_cmd, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)


class IronclawPodmanApproach(Approach):
    """Real ironclaw agent with per-user Podman rootless isolation."""

    def __init__(self):
        self._agent_ids: List[str] = []
        self._host_ports: Dict[str, int] = {}
        self._users: Dict[str, str] = {}   # agent_id → username
        self._uids: Dict[str, int] = {}    # agent_id → uid
        self._run_id: str = "unknown"

    @property
    def name(self) -> str:
        return "ironclaw-podman"

    def setup(self, config: BenchmarkConfig) -> None:
        self._run_id = config.run_id

        # Verify ironclaw agent image exists (in host Docker, for export)
        for image in [IRONCLAW_AGENT_IMAGE, IRONCLAW_SANDBOX_IMAGE]:
            result = subprocess.run(
                ["sudo", "docker", "image", "inspect", image],
                capture_output=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Image '{image}' not found. Run 'make ironclaw-images' first."
                )

        # Verify Podman is available
        result = subprocess.run(
            ["podman", "--version"], capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError("Podman not found. Install podman first.")

        print(f"[{self.name}] Setup complete. run_id={self._run_id}")

    def _create_user(self, agent_id, index):
        """Create an unprivileged user for this agent."""
        username = f"{USER_PREFIX}{index}"
        uid = BASE_UID + index

        # Create user if doesn't exist
        result = subprocess.run(
            ["id", "-u", username], capture_output=True, text=True,
        )
        if result.returncode != 0:
            subprocess.run(
                ["sudo", "useradd", "-m", "-u", str(uid), "-s", "/bin/bash",
                 username],
                check=True, capture_output=True,
            )

        # Enable lingering for systemd user session
        subprocess.run(
            ["sudo", "loginctl", "enable-linger", username],
            capture_output=True,
        )

        # Start the user's systemd session
        subprocess.run(
            ["sudo", "machinectl", "shell", f"{username}@", "/bin/true"],
            capture_output=True, timeout=10,
        )
        time.sleep(1)

        return username, uid

    def _load_image_to_user(self, user, image):
        """Export a Docker image and load it into the user's Podman store."""
        save_proc = subprocess.Popen(
            ["sudo", "docker", "save", image],
            stdout=subprocess.PIPE,
        )
        load_result = _run_as_user(
            user, ["podman", "load"],
            capture=True, stdin=save_proc.stdout,
        )
        save_proc.stdout.close()
        save_proc.wait()

        if load_result.returncode != 0:
            print(f"[{self.name}] WARNING: podman load failed for {user}: "
                  f"{load_result.stderr.strip()}")
            return

        # Podman 3.x loads images as "localhost/latest:latest" instead of
        # preserving the original Docker tag. Parse the loaded name from
        # stdout and re-tag to the expected name.
        loaded_name = None
        for line in (load_result.stdout + load_result.stderr).splitlines():
            if "Loaded image" in line:
                # Format: "Loaded image(s): localhost/latest:latest"
                parts = line.split(":", 1)
                if len(parts) == 2:
                    loaded_name = parts[1].strip()
                    break

        # Tag to the Docker image name so ironclaw can find it
        src = loaded_name or "localhost/latest:latest"
        _run_as_user(user, ["podman", "tag", src, image])
        _run_as_user(user, ["podman", "tag", src, f"localhost/{image}"])
        print(f"[{self.name}]   Loaded {image} as {src} for {user}")

    def start_agents(self, n: int, config: BenchmarkConfig) -> List[str]:
        self._agent_ids = []
        self._host_ports = {}
        self._users = {}
        self._uids = {}

        for i in range(n):
            agent_id = f"agent-{i}"
            username, uid = self._create_user(agent_id, i)
            self._users[agent_id] = username
            self._uids[agent_id] = uid

            # Load images into user's Podman store
            print(f"[{self.name}] Loading images for {username}...")
            self._load_image_to_user(username, IRONCLAW_AGENT_IMAGE)
            self._load_image_to_user(username, IRONCLAW_SANDBOX_IMAGE)

            # Start Podman API socket
            _fire_as_user(username, [
                "podman", "system", "service",
                "--timeout=0",
                f"unix:///run/user/{uid}/podman/podman.sock",
            ])
            time.sleep(1)

            gateway_port = config.orchestrator_base_port + i
            podman_socket = SOCKET_TEMPLATE.format(uid=uid)
            workspace_host = f"/home/{username}/workspace"

            # Create workspace dir on host, writable by container uid 1000
            _run_as_user(username, ["mkdir", "-p", workspace_host])
            _run_as_user(username, ["chmod", "1777", workspace_host])

            env = ironclaw_agent_env(config, agent_id, 3000)
            # Workspace at host path so sandbox bind mounts work
            env["WORKSPACE_DIR"] = workspace_host
            # Native Podman compat (SecurityOpt, no mem limits, no AutoRemove)
            env["SANDBOX_PODMAN_COMPAT"] = "true"
            # Image with localhost/ prefix required by Podman for local images
            env["SANDBOX_IMAGE"] = f"localhost/{IRONCLAW_SANDBOX_IMAGE}"

            # Run agent container under user's Podman
            env_args = []
            for k, v in env.items():
                env_args += ["-e", f"{k}={v}"]

            _fire_as_user(username, [
                "podman", "run", "-d",
                "--name", f"bench-ic-agent-{i}",
                "-p", f"{gateway_port}:3000",
                # Mount Podman socket directly (no proxy needed with
                # SANDBOX_PODMAN_COMPAT + localhost/ image prefix)
                "-v", f"{podman_socket}:/var/run/docker.sock",
                # Shared workspace: same path on host and in container
                "-v", f"{workspace_host}:{workspace_host}:rw",
            ] + env_args + [
                f"localhost/{IRONCLAW_AGENT_IMAGE}",
            ])

            self._agent_ids.append(agent_id)
            self._host_ports[agent_id] = gateway_port
            print(f"[{self.name}] Started {agent_id} as {username}")

        print(f"[{self.name}] Waiting for {n} agents...")
        for agent_id, port in self._host_ports.items():
            if wait_for_gateway(port, timeout_s=180, label=agent_id):
                print(f"[{self.name}] {agent_id} healthy")
            else:
                print(f"[{self.name}] WARNING: {agent_id} not ready after 180s")

        print(f"[{self.name}] {len(self._agent_ids)} agents started.")
        return list(self._agent_ids)

    def start_benchmark(self) -> None:
        for agent_id, port in self._host_ports.items():
            ok = trigger_worker_spawn(port)
            if not ok:
                print(f"[{self.name}] WARNING: trigger failed for {agent_id}")

    def get_agent_pids(self) -> Dict[str, int]:
        pids = {}
        for agent_id, username in self._users.items():
            try:
                result = _run_as_user(username, [
                    "podman", "inspect", "--format", "{{.State.Pid}}",
                    f"bench-ic-agent-{agent_id.split('-')[-1]}",
                ])
                if result.returncode == 0:
                    pid = int(result.stdout.strip())
                    if pid > 0:
                        pids[agent_id] = pid
            except (ValueError, subprocess.SubprocessError):
                pass
        return pids

    def count_active_workers(self) -> int:
        total = 0
        for agent_id, username in self._users.items():
            try:
                result = _run_as_user(username, [
                    "podman", "ps", "-q", "--filter", "name=sandbox-",
                ])
                if result.returncode == 0 and result.stdout.strip():
                    total += len(result.stdout.strip().split("\n"))
            except subprocess.SubprocessError:
                pass
        return total

    def collect_agent_logs(self, agent_ids: List[str], output_dir) -> None:
        output_dir = Path(output_dir)
        for i, agent_id in enumerate(agent_ids):
            username = self._users.get(agent_id)
            if not username:
                continue
            log_file = output_dir / f"{agent_id}.jsonl"
            try:
                result = _run_as_user(username, [
                    "podman", "logs", f"bench-ic-agent-{i}",
                ])
                if result.returncode == 0:
                    log_file.write_text(result.stdout + result.stderr)
                    print(f"[{self.name}] Collected logs for {agent_id}")
            except subprocess.SubprocessError as e:
                print(f"[{self.name}] Log collection failed for {agent_id}: {e}")

    def stop_agents(self) -> None:
        for agent_id, username in self._users.items():
            idx = agent_id.split("-")[-1]
            _run_as_user(username, [
                "podman", "stop", "-t", "10", f"bench-ic-agent-{idx}",
            ])
            _run_as_user(username, [
                "podman", "rm", "-f", f"bench-ic-agent-{idx}",
            ])
            # Stop sandbox containers too
            _run_as_user(username, [
                "sh", "-c",
                "podman ps -q --filter name=sandbox- | xargs -r podman rm -f",
            ])
        print(f"[{self.name}] All agents stopped.")

    def cleanup(self) -> None:
        self.stop_agents()
        self._agent_ids = []
        self._host_ports = {}
        self._users = {}
        self._uids = {}
        print(f"[{self.name}] Cleanup complete.")
