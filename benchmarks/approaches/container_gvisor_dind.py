"""
Approach D: Container-per-agent with gVisor isolation and per-agent Docker daemon.

Each agent runs inside a gVisor (runsc) container with its own dockerd. Workers
are spawned by the agent's inner Docker daemon, so they live entirely within the
gVisor sandbox. This combines gVisor kernel isolation with full daemon isolation
(like vm-qemu) but without the overhead of a full VM.

Network topology: identical to vm-qemu. The agent process runs directly in the
gVisor container's namespace. Workers reach the agent via the inner Docker bridge
gateway (172.17.0.1:8080). No host port mapping needed for worker->agent
communication, but we publish the port anyway for debugging.

gVisor's --cap-add ALL is safe because capabilities are virtualized by the Sentry
— the inner dockerd has no host kernel access.

Requires runsc configured with --net-raw and --allow-packet-socket-write in
/etc/docker/daemon.json for Docker-in-gVisor support.
"""

import subprocess
import time
from pathlib import Path
from typing import Dict, List

from approaches.base import Approach, BenchmarkConfig

WORKER_TAR_PATH = "/tmp/bench-worker.tar"


class ContainerGvisorDindApproach(Approach):
    """Container-per-agent with gVisor isolation and private Docker daemon."""

    def __init__(self):
        self._agent_ids: List[str] = []
        self._agent_image = "bench-agent-dind:latest"
        self._run_id: str = "unknown"

    @property
    def name(self) -> str:
        return "container-gvisor-dind"

    def setup(self, config: BenchmarkConfig) -> None:
        """Verify images, gVisor runtime, and save worker image tarball."""
        self._run_id = config.run_id

        # Verify agent-dind image exists
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

        # Verify runsc runtime is configured in Docker
        result = subprocess.run(
            ["docker", "info", "--format", "{{.Runtimes}}"],
            capture_output=True,
            text=True,
        )
        if "runsc" not in result.stdout:
            raise RuntimeError(
                "gVisor runtime 'runsc' not found in Docker. "
                "Install gVisor and add runsc to /etc/docker/daemon.json."
            )

        # Quick smoke test
        result = subprocess.run(
            ["runsc", "--version"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError("runsc binary not found or not working.")

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

        print(f"[{self.name}] Setup complete. Images verified, gVisor available, "
              f"worker tarball saved. run_id={self._run_id}")

    def start_agents(self, n: int, config: BenchmarkConfig) -> List[str]:
        """Start N gVisor DinD agent containers, each with its own Docker daemon."""
        self._agent_ids = []

        for i in range(n):
            agent_id = f"agent-{i}"
            container_name = f"bench-agent-{i}"
            host_port = config.orchestrator_base_port + i

            cmd = [
                "docker", "run", "-d",
                "--name", container_name,
                "--runtime=runsc",
                "--cap-add", "ALL",
                "--memory", f"{config.agent_memory_mb}m",
                # Publish orchestrator HTTP port (for debugging / external access)
                "-p", f"{host_port}:8080",
                # Mount worker image tarball (read-only)
                "-v", f"{WORKER_TAR_PATH}:/opt/.worker-image.tar:ro",
                # Labels for identification and cleanup
                "--label", f"bench_run_id={config.run_id}",
                "--label", "bench_role=agent",
                "--label", f"bench_agent_id={agent_id}",
                "--label", f"bench_approach={self.name}",
                # Pass configuration via environment
                "-e", f"AGENT_ID={agent_id}",
                "-e", f"AGENT_BASELINE_MB={config.agent_baseline_mb}",
                "-e", f"SPAWN_INTERVAL_MEAN_S={config.spawn_interval_mean_s}",
                "-e", f"MAX_CONCURRENT_WORKERS={config.max_concurrent_workers}",
                "-e", f"BENCHMARK_DURATION_S={config.benchmark_duration_s}",
                "-e", f"WORKER_IMAGE={config.worker_image}",
                "-e", f"WORKER_MEMORY_LIMIT_MB={config.worker_memory_limit_mb}",
                "-e", f"WORKER_MEMORY_MB={config.worker_memory_mb}",
                "-e", f"WORKER_DURATION_MIN_S={config.worker_duration_min_s}",
                "-e", f"WORKER_DURATION_MAX_S={config.worker_duration_max_s}",
                "-e", f"RNG_SEED={config.rng_seed}",
                "-e", f"BENCH_RUN_ID={config.run_id}",
                "-e", f"BENCH_APPROACH={self.name}",
                # Orchestrator networking: workers reach agent via inner bridge
                "-e", "ORCHESTRATOR_PORT=8080",
                # No ORCHESTRATOR_HOST_PORT — workers use inner bridge gateway
                # No Docker socket mount — each agent has its own daemon
                # No WORKER_RUNTIME — inner daemon uses default (runc inside gVisor)
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

            self._agent_ids.append(agent_id)
            print(f"[{self.name}] Started {container_name}")

        # Wait for dockerd startup + worker image load inside each gVisor container
        boot_wait = 30
        print(f"[{self.name}] Waiting {boot_wait}s for {n} agents "
              f"(dockerd startup + image load)...")
        time.sleep(boot_wait)

        print(f"[{self.name}] {n} agents started (gVisor DinD, private daemon per agent).")
        return list(self._agent_ids)

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
        """Workers run inside gVisor containers, not visible from host.

        Return -1 to indicate "unknown" — accurate counts come from
        agent JSONL logs collected after the run.
        """
        return -1

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
        print(f"[{self.name}] All containers removed.")

    def cleanup(self) -> None:
        """Stop containers and clean up worker tarball."""
        self.stop_agents()
        self.remove_containers()
