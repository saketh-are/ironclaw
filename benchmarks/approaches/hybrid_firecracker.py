"""
Approach C: Hybrid — agents in Docker, workers as Firecracker microVMs.

Each agent runs in a Docker container with /dev/kvm passthrough, the
firecracker binary, a pre-built kernel, and a minimal rootfs mounted in.
Agents spawn workers as Firecracker microVMs directly — no Docker socket
needed inside the agent container.

This is closer to a production model where workers get hardware-level KVM
isolation instead of sharing the host kernel via Docker.

Architecture:
  Host
  ├── Docker daemon
  │   ├── agent-0 container (/dev/kvm, firecracker, kernel, rootfs)
  │   │   ├── agent.py (WORKER_BACKEND=firecracker)
  │   │   ├── firecracker VM: worker-0
  │   │   └── firecracker VM: worker-1
  │   ├── agent-1 container
  │   │   └── ...
  │   └── ...
  └── benchmark orchestrator (host, collects /proc metrics)
"""

import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

from approaches.base import Approach, BenchmarkConfig

APPROACH_DIR = Path(__file__).resolve().parent
FC_DIR = APPROACH_DIR / "hybrid_firecracker_assets"
KERNEL_FILE = FC_DIR / "vmlinux-5.10.bin"
ROOTFS_FILE = FC_DIR / "worker-rootfs.ext4"


class HybridFirecrackerApproach(Approach):
    """Agents in Docker containers, workers as Firecracker microVMs."""

    def __init__(self):
        self._agent_ids: List[str] = []
        self._host_ports: Dict[str, int] = {}
        self._agent_image = "bench-agent-fc:latest"
        self._run_id: str = "unknown"
        self._host_log_dir: Optional[Path] = None

    @property
    def name(self) -> str:
        return "hybrid-firecracker"

    def setup(self, config: BenchmarkConfig) -> None:
        """Verify firecracker binary, /dev/kvm, kernel, rootfs, and Docker image."""
        self._run_id = config.run_id

        # Check /dev/kvm
        if not os.path.exists("/dev/kvm"):
            raise RuntimeError(
                "/dev/kvm not found. KVM support required for Firecracker."
            )

        # Check firecracker binary on the host (will be bind-mounted into containers)
        fc_bin = self._find_firecracker()
        if fc_bin is None:
            raise RuntimeError(
                "firecracker binary not found on PATH or in /usr/local/bin. "
                "Run 'make fc-setup' or install Firecracker first."
            )

        # Check kernel
        if not KERNEL_FILE.exists():
            raise RuntimeError(
                f"Firecracker kernel not found at {KERNEL_FILE}. "
                "Run 'make fc-kernel' first."
            )

        # Check rootfs
        if not ROOTFS_FILE.exists():
            raise RuntimeError(
                f"Firecracker rootfs not found at {ROOTFS_FILE}. "
                "Run 'make fc-rootfs' first."
            )

        # Check agent Docker image
        result = subprocess.run(
            ["docker", "image", "inspect", self._agent_image],
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Docker image '{self._agent_image}' not found. "
                "Run 'make fc-image' first."
            )

        print(
            f"[{self.name}] Setup complete. Firecracker={fc_bin}, "
            f"kernel={KERNEL_FILE.name}, rootfs={ROOTFS_FILE.name}, "
            f"run_id={self._run_id}"
        )

    def _find_firecracker(self) -> str:
        """Find the firecracker binary on the host."""
        # Check PATH
        result = subprocess.run(
            ["which", "firecracker"], capture_output=True, text=True
        )
        if result.returncode == 0:
            return result.stdout.strip()

        # Check common install locations
        for path in ["/usr/local/bin/firecracker", "/usr/bin/firecracker"]:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path

        return None

    def start_agents(self, n: int, config: BenchmarkConfig) -> List[str]:
        """Start N agent Docker containers with KVM + Firecracker access."""
        self._agent_ids = []
        self._host_ports = {}
        fc_bin = self._find_firecracker()
        run_output_dir = getattr(config, "run_output_dir", "")
        self._host_log_dir = Path(run_output_dir) if run_output_dir else None

        for i in range(n):
            agent_id = f"agent-{i}"
            container_name = f"bench-agent-{i}"
            host_log_file = None
            if self._host_log_dir is not None:
                host_log_file = self._host_log_dir / f"{agent_id}.jsonl"
                host_log_file.touch()
                host_log_file.chmod(0o666)

            # Orchestrator port: base + agent index
            orch_port = config.orchestrator_base_port + i

            cmd = [
                "docker", "run", "-d",
                "--name", container_name,
                "--memory", f"{config.agent_memory_mb}m",
                # Labels for identification and cleanup
                "--label", f"bench_run_id={config.run_id}",
                "--label", "bench_role=agent",
                "--label", f"bench_agent_id={agent_id}",
                "--label", f"bench_approach={self.name}",
                # KVM passthrough for Firecracker
                "--device", "/dev/kvm",
                # TUN device for TAP networking
                "--device", "/dev/net/tun",
                # NET_ADMIN for creating TAP devices
                "--cap-add", "NET_ADMIN",
                # Mount firecracker binary (read-only)
                "-v", f"{fc_bin}:/usr/local/bin/firecracker:ro",
                # Mount kernel (read-only)
                "-v", f"{KERNEL_FILE}:/opt/vmlinux:ro",
                # Mount rootfs (read-only; agent copies per-VM if needed)
                "-v", f"{ROOTFS_FILE}:/opt/worker-rootfs.ext4:ro",
                # Port mapping for orchestrator HTTP
                "-p", f"{orch_port}:{orch_port}",
                # Pass configuration via environment
                "-e", f"AGENT_ID={agent_id}",
                "-e", f"BENCHMARK_MODE={config.benchmark_mode}",
                "-e", f"AGENT_BASELINE_MB={config.agent_baseline_mb}",
                "-e", f"SPAWN_INTERVAL_MEAN_S={config.spawn_interval_mean_s}",
                "-e", f"MAX_CONCURRENT_WORKERS={config.max_concurrent_workers}",
                "-e", f"BENCHMARK_DURATION_S={config.benchmark_duration_s}",
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
                # Firecracker-specific config
                "-e", "WORKER_BACKEND=firecracker",
                "-e", "FC_KERNEL_PATH=/opt/vmlinux",
                "-e", "FC_ROOTFS_PATH=/opt/worker-rootfs.ext4",
                # Orchestrator HTTP server inside the agent container
                "-e", f"ORCHESTRATOR_PORT={orch_port}",
                # Storage validation
                "-e", "STORAGE_VALIDATION=1",
                "-e", "WORKSPACE_BASE=/tmp/bench-workspaces",
                self._agent_image,
            ]
            if host_log_file is not None:
                cmd[-1:-1] = [
                    "-v", f"{self._host_log_dir}:/bench-output",
                    "-e", f"EVENT_LOG_PATH=/bench-output/{agent_id}.jsonl",
                ]

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to start agent {agent_id}: {result.stderr.strip()}"
                )

            self._agent_ids.append(agent_id)
            self._host_ports[agent_id] = orch_port
            print(f"[{self.name}] Started {container_name}")

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
        """Get PIDs for dockerd and containerd on the host."""
        pids = {}
        for daemon_name in ["dockerd", "containerd"]:
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
        """
        Count active Firecracker VMM processes across all agent containers.
        Uses docker exec + pgrep to count firecracker processes inside each
        agent container.
        """
        total = 0
        for i in range(len(self._agent_ids)):
            container_name = f"bench-agent-{i}"
            try:
                result = subprocess.run(
                    ["docker", "exec", container_name, "pgrep", "-c", "firecracker"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    total += int(result.stdout.strip())
            except (ValueError, subprocess.SubprocessError, subprocess.TimeoutExpired):
                pass
        return total

    def collect_agent_logs(self, agent_ids: List[str], output_dir) -> None:
        """Collect agent stdout logs via docker logs."""
        output_dir = Path(output_dir)
        for i, agent_id in enumerate(agent_ids):
            container_name = f"bench-agent-{i}"
            log_file = output_dir / f"{agent_id}.jsonl"
            if log_file.exists() and log_file.stat().st_size > 0:
                print(f"[{self.name}] Reusing direct host log for {agent_id}")
                continue
            try:
                result = subprocess.run(
                    ["docker", "logs", container_name],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    log_file.write_text(result.stdout)
                    print(f"[{self.name}] Collected logs for {agent_id}")
            except subprocess.SubprocessError as e:
                print(f"[{self.name}] Failed to collect logs for {agent_id}: {e}")

    def live_event_log_paths(
        self,
        agent_ids: List[str],
        output_dir: Path,
    ) -> Dict[str, Path]:
        if self._host_log_dir is None:
            return {}
        return {
            agent_id: self._host_log_dir / f"{agent_id}.jsonl"
            for agent_id in agent_ids
        }

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

        print(f"[{self.name}] All agents and Firecracker VMs stopped.")

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
            subprocess.run(
                ["docker", "rm", "-f"] + container_ids,
                capture_output=True,
            )
            print(f"[{self.name}] Removed {len(container_ids)} containers.")

        self._agent_ids = []
        self._host_ports = {}

    def cleanup(self) -> None:
        """Stop and remove agents (artifact cleanup is manual via make clean)."""
        self.stop_agents()
        self.remove_containers()

    # Storage validation is supported via per-VM ext4 workspace images.
    # The agent creates a small ext4 image with challenge.txt, attaches it as
    # /dev/vdb, and reads reply.txt back via debugfs after VM exit.
    # Networking is provided via per-VM TAP devices.
