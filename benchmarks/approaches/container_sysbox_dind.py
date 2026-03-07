"""
Approach: Container-per-agent with Sysbox isolation and per-agent Docker daemon.

Each agent runs inside a Sysbox (sysbox-runc) container with its own dockerd.
Workers are spawned by the agent's inner Docker daemon, so they live entirely
within the Sysbox container. This combines strong user-namespace isolation with
full daemon isolation (like vm-qemu) but without the overhead of a full VM.

Sysbox vs gVisor DinD:
  - Sysbox uses Linux user namespaces + shiftfs/ID-mapped mounts for isolation.
    gVisor interposes a userspace kernel (Sentry) on all syscalls.
  - Sysbox supports overlay2 storage driver natively (fast).
    gVisor requires vfs (slow, full copies).
  - Sysbox supports iptables inside the container.
    gVisor's netstack does not.
  - Sysbox does NOT need --cap-add ALL; capabilities are handled by the
    user-namespace remapping. gVisor needs --cap-add ALL (safe because Sentry
    virtualizes them).
  - Sysbox boot is typically faster (overlay2 + native iptables).

Network topology: identical to gVisor DinD. The agent process runs directly in
the Sysbox container's namespace. Workers reach the agent via the inner Docker
bridge gateway (172.17.0.1:8080). No host port mapping needed for worker->agent
communication, but we publish the port anyway for debugging.

Requires sysbox-runc installed and registered as a Docker runtime in
/etc/docker/daemon.json.
"""

import json
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

from approaches.base import Approach, BenchmarkConfig

WORKER_TAR_PATH = "/tmp/bench-worker.tar"


class ContainerSysboxDindApproach(Approach):
    """Container-per-agent with Sysbox isolation and private Docker daemon."""

    def __init__(self):
        self._agent_ids: List[str] = []
        self._agent_ips: Dict[str, str] = {}  # agent_id → outer container IP
        self._agent_image = "bench-agent-sysbox:latest"
        self._run_id: str = "unknown"

    @property
    def name(self) -> str:
        return "container-sysbox-dind"

    def setup(self, config: BenchmarkConfig) -> None:
        """Verify images, Sysbox runtime, and save worker image tarball."""
        self._run_id = config.run_id

        # Verify agent-sysbox image exists
        result = subprocess.run(
            ["docker", "image", "inspect", self._agent_image],
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Docker image '{self._agent_image}' not found. "
                "Run 'make images' first."
            )

        # Verify worker image exists
        result = subprocess.run(
            ["docker", "image", "inspect", config.worker_image],
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Docker image '{config.worker_image}' not found. "
                "Run 'make images' first."
            )

        # Verify sysbox-runc runtime is configured in Docker
        result = subprocess.run(
            ["docker", "info", "--format", "{{.Runtimes}}"],
            capture_output=True,
            text=True,
        )
        if "sysbox-runc" not in result.stdout:
            raise RuntimeError(
                "Sysbox runtime 'sysbox-runc' not found in Docker. "
                "Install Sysbox and add sysbox-runc to /etc/docker/daemon.json."
            )

        # Save worker image to tarball for loading into per-agent daemons
        print(f"[{self.name}] Saving worker image to {WORKER_TAR_PATH}...")
        result = subprocess.run(
            ["docker", "save", "-o", WORKER_TAR_PATH, config.worker_image],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to save worker image: {result.stderr.strip()}"
            )

        print(f"[{self.name}] Setup complete. Images verified, Sysbox available, "
              f"worker tarball saved. run_id={self._run_id}")

    def start_agents(self, n: int, config: BenchmarkConfig) -> List[str]:
        """Start N Sysbox DinD agent containers, each with its own Docker daemon."""
        self._agent_ids = []
        self._agent_ips = {}

        for i in range(n):
            agent_id = f"agent-{i}"
            container_name = f"bench-agent-{i}"

            cmd = [
                "docker", "run", "-d",
                "--name", container_name,
                "--runtime=sysbox-runc",
                # No --cap-add ALL: Sysbox handles capabilities via user-ns
                "--memory", f"{config.agent_memory_mb}m",
                # Mount worker image tarball (read-only)
                "-v", f"{WORKER_TAR_PATH}:/opt/.worker-image.tar:ro",
                # Labels for identification and cleanup
                "--label", f"bench_run_id={config.run_id}",
                "--label", "bench_role=agent",
                "--label", f"bench_agent_id={agent_id}",
                "--label", f"bench_approach={self.name}",
                # Pass configuration via environment
                "-e", f"AGENT_ID={agent_id}",
                "-e", f"BENCHMARK_MODE={config.benchmark_mode}",
                "-e", f"AGENT_BASELINE_MB={config.agent_baseline_mb}",
                "-e", f"SPAWN_INTERVAL_MEAN_S={config.spawn_interval_mean_s}",
                "-e", f"MAX_CONCURRENT_WORKERS={config.max_concurrent_workers}",
                "-e", f"BENCHMARK_DURATION_S={config.benchmark_duration_s}",
                "-e", f"WORKER_IMAGE={config.worker_image}",
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
                # Orchestrator networking: workers reach agent via inner bridge
                "-e", "ORCHESTRATOR_PORT=8080",
                # No ORCHESTRATOR_HOST_PORT — workers use inner bridge gateway
                # No Docker socket mount — each agent has its own daemon
            ]

            # Storage validation: inner dockerd resolves paths locally, no host-path indirection
            if config.storage_validation:
                cmd += [
                    "-e", "STORAGE_VALIDATION=1",
                    "-e", "WORKSPACE_BASE=/tmp/bench-workspaces",
                ]

            cmd.append(self._agent_image)

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to start agent {agent_id}: {result.stderr.strip()}"
                )

            ip_addr = self._get_container_ip(container_name)
            if not ip_addr:
                raise RuntimeError(
                    f"Failed to resolve container IP for agent {agent_id}"
                )

            self._agent_ids.append(agent_id)
            self._agent_ips[agent_id] = ip_addr
            print(f"[{self.name}] Started {container_name}")

        # Poll /health on each agent container IP until all are ready (max 60s)
        print(f"[{self.name}] Waiting for {n} agents to become healthy "
              f"(dockerd startup + image load)...")
        deadline = time.monotonic() + 60
        ready = set()
        while time.monotonic() < deadline and len(ready) < n:
            for agent_id, ip_addr in self._agent_ips.items():
                if agent_id in ready:
                    continue
                try:
                    resp = urllib.request.urlopen(
                        self._agent_url(ip_addr, "/health"), timeout=2
                    )
                    if resp.status == 200:
                        ready.add(agent_id)
                        print(f"[{self.name}] {agent_id} healthy")
                except Exception:
                    pass
            if len(ready) < n:
                time.sleep(2)

        if len(ready) < n:
            not_ready = set(self._agent_ips) - ready
            print(f"[{self.name}] WARNING: agents not ready after 60s: {not_ready}")

        print(f"[{self.name}] {len(ready)}/{n} agents started (Sysbox DinD, private daemon per agent).")
        return list(self._agent_ids)

    def start_benchmark(self) -> None:
        for agent_id, ip_addr in self._agent_ips.items():
            req = urllib.request.Request(
                self._agent_url(ip_addr, "/control/start"),
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
                    f"Failed to start benchmark on {agent_id}@{ip_addr}: {e}"
                ) from e

    def get_agent_pids(self) -> Dict[str, int]:
        """Get host PIDs for agent containers via docker inspect."""
        pids = {}
        for i, agent_id in enumerate(self._agent_ids):
            container_name = f"bench-agent-{i}"
            try:
                result = subprocess.run(
                    ["docker", "inspect", "--format", "{{.State.Pid}}", container_name],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    pid = int(result.stdout.strip())
                    if pid > 0:
                        pids[agent_id] = pid
            except (ValueError, subprocess.SubprocessError):
                pass
        return pids

    def get_daemon_pids(self) -> Dict[str, int]:
        """Get PIDs for dockerd and containerd on the host.

        Note: sysbox-fs and sysbox-mgr also run on the host as part of the
        Sysbox runtime. We track them for overhead measurement.
        """
        pids = {}
        for daemon_name in ["dockerd", "containerd", "sysbox-fs", "sysbox-mgr"]:
            try:
                result = subprocess.run(
                    ["pgrep", "-x", daemon_name],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0 and result.stdout.strip():
                    pid = int(result.stdout.strip().split("\n")[0])
                    pids[daemon_name] = pid
            except (ValueError, subprocess.SubprocessError):
                pass
        return pids

    def count_active_workers(self) -> int:
        """Query each agent's /status endpoint and sum active workers."""
        total = 0
        for agent_id, ip_addr in self._agent_ips.items():
            try:
                resp = urllib.request.urlopen(
                    self._agent_url(ip_addr, "/status"), timeout=2
                )
                data = json.loads(resp.read())
                total += data.get("active_workers", 0)
            except Exception:
                pass
        return total

    def collect_agent_logs(self, agent_ids: List[str], output_dir) -> None:
        """Collect agent stdout logs via docker logs."""
        output_dir = Path(output_dir)
        for i, agent_id in enumerate(agent_ids):
            container_name = f"bench-agent-{i}"
            log_file = output_dir / f"{agent_id}.jsonl"
            try:
                result = subprocess.run(
                    ["docker", "logs", container_name],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    log_file.write_text(result.stdout)
                    print(f"[{self.name}] Collected logs for {agent_id}")
                else:
                    print(f"[{self.name}] docker logs failed for {agent_id}: "
                          f"rc={result.returncode} stderr={result.stderr.strip()}")
            except subprocess.SubprocessError as e:
                print(f"[{self.name}] Failed to collect logs for {agent_id}: {e}")

    def stop_agents(self) -> None:
        """Gracefully stop agent containers (SIGTERM with timeout).

        Agents remain as stopped containers so logs can still be collected
        via `docker logs`. Call remove_containers() after log collection.
        """
        for i in range(len(self._agent_ids)):
            container_name = f"bench-agent-{i}"
            subprocess.run(
                ["docker", "stop", "-t", "30", container_name],
                capture_output=True,
            )
        print(f"[{self.name}] All agents stopped.")

    def remove_containers(self) -> None:
        """Force-remove all containers from this benchmark run."""
        result = subprocess.run(
            [
                "docker", "ps", "-aq",
                "--filter", f"label=bench_run_id={self._run_id}",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            container_ids = result.stdout.strip().split("\n")
            if container_ids:
                subprocess.run(
                    ["docker", "rm", "-f"] + container_ids,
                    capture_output=True,
                )
        self._agent_ids = []
        self._agent_ips = {}
        print(f"[{self.name}] All containers removed.")

    def cleanup(self) -> None:
        """Stop containers and clean up worker tarball."""
        self.stop_agents()
        self.remove_containers()

    @staticmethod
    def _agent_url(ip_addr: str, path: str) -> str:
        return f"http://{ip_addr}:8080{path}"

    @staticmethod
    def _get_container_ip(container_name: str) -> Optional[str]:
        result = subprocess.run(
            [
                "docker", "inspect",
                "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                container_name,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        ip_addr = result.stdout.strip()
        return ip_addr or None
