"""
Approach: Real IronClaw on shared Docker daemon.

Each agent runs as a Docker container with the host Docker socket bind-mounted.
Ironclaw's SandboxManager creates sandbox worker containers as siblings on the
host daemon — the same production topology ironclaw uses by default.

This is the simplest real-ironclaw approach: no inner daemon, no proxy needed.
"""

import json
import subprocess
import tempfile
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


class IronclawDockerApproach(Approach):
    """Real ironclaw agent with shared host Docker daemon."""

    def __init__(self):
        self._agent_ids: List[str] = []
        self._host_ports: Dict[str, int] = {}
        self._run_id: str = "unknown"
        self._workspace_dirs: Dict[str, Path] = {}  # agent_id -> host temp dir

    @property
    def suite(self) -> str:
        return "ironclaw"

    @property
    def name(self) -> str:
        return "ironclaw-docker"

    def setup(self, config: BenchmarkConfig) -> None:
        self._run_id = config.run_id
        for image in [IRONCLAW_AGENT_IMAGE, IRONCLAW_SANDBOX_IMAGE]:
            result = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Docker image '{image}' not found. "
                    "Run 'make ironclaw-images' first."
                )
        print(f"[{self.name}] Setup complete. run_id={self._run_id}")

    def start_agents(self, n: int, config: BenchmarkConfig) -> List[str]:
        self._agent_ids = []
        self._host_ports = {}

        for i in range(n):
            agent_id = f"agent-{i}"
            container_name = f"bench-ic-agent-{i}"
            gateway_port = config.orchestrator_base_port + i

            env = ironclaw_agent_env(config, agent_id, 3000)

            # In the shared-daemon topology, sandbox containers are siblings
            # on the host daemon.  Ironclaw bind-mounts its cwd (the
            # workspace) into sandbox containers.  For this to work, the
            # workspace path inside the agent must also exist on the HOST
            # so Docker can find it.  We create a unique host directory
            # and mount it at the SAME path inside the agent container.
            ws_host = Path(f"/tmp/ic-bench-ws-{config.run_id}-{i}")
            ws_host.mkdir(parents=True, exist_ok=True)
            ws_host.chmod(0o777)  # writable by sandbox user 1000
            self._workspace_dirs[agent_id] = ws_host

            # Override WORKSPACE_DIR so entrypoint.sh uses this path
            env["WORKSPACE_DIR"] = str(ws_host)

            cmd = [
                "docker", "run", "-d",
                "--name", container_name,
                "--memory", f"{config.agent_memory_mb}m",
                "-p", f"{gateway_port}:3000",
                # Shared host Docker socket
                "-v", "/var/run/docker.sock:/var/run/docker.sock",
                # Workspace: same path on host and in container so that
                # sandbox sibling containers can bind-mount the same path.
                "-v", f"{ws_host}:{ws_host}",
                # Labels
                "--label", f"bench_run_id={config.run_id}",
                "--label", "bench_role=agent",
                "--label", f"bench_agent_id={agent_id}",
                "--label", f"bench_approach={self.name}",
            ]
            for k, v in env.items():
                cmd += ["-e", f"{k}={v}"]
            cmd.append(IRONCLAW_AGENT_IMAGE)

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to start {agent_id}: {result.stderr.strip()}"
                )

            self._agent_ids.append(agent_id)
            self._host_ports[agent_id] = gateway_port
            print(f"[{self.name}] Started {container_name}")

        print(f"[{self.name}] Waiting for {n} agents to become healthy...")
        for agent_id, port in self._host_ports.items():
            if wait_for_gateway(port, timeout_s=120, label=agent_id):
                print(f"[{self.name}] {agent_id} healthy")
            else:
                raise RuntimeError(
                    f"{agent_id} gateway not ready after 120s on port {port}"
                )

        print(f"[{self.name}] {n} agents started.")
        return list(self._agent_ids)

    def start_benchmark(self) -> None:
        """Send a trigger message to each agent to spawn sandbox workers."""
        for agent_id, port in self._host_ports.items():
            ok = trigger_worker_spawn(port)
            if not ok:
                print(f"[{self.name}] WARNING: trigger failed for {agent_id}")

    def get_agent_pids(self) -> Dict[str, int]:
        pids = {}
        for i, agent_id in enumerate(self._agent_ids):
            container_name = f"bench-ic-agent-{i}"
            try:
                result = subprocess.run(
                    ["docker", "inspect", "--format", "{{.State.Pid}}",
                     container_name],
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    pid = int(result.stdout.strip())
                    if pid > 0:
                        pids[agent_id] = pid
            except (ValueError, subprocess.SubprocessError):
                pass
        return pids

    def get_daemon_pids(self) -> Dict[str, int]:
        pids = {}
        for daemon_name in ["dockerd", "containerd"]:
            try:
                result = subprocess.run(
                    ["pgrep", "-x", daemon_name],
                    capture_output=True, text=True,
                )
                if result.returncode == 0 and result.stdout.strip():
                    pid = int(result.stdout.strip().split("\n")[0])
                    pids[daemon_name] = pid
            except (ValueError, subprocess.SubprocessError):
                pass
        return pids

    def count_active_workers(self) -> int:
        """Count sandbox containers on the shared Docker daemon."""
        return sum(self.count_active_workers_per_agent().values())

    def count_active_workers_per_agent(self) -> Dict[str, int]:
        counts = {agent_id: 0 for agent_id in self._agent_ids}
        try:
            result = subprocess.run(
                ["docker", "ps", "-q", "--filter", "name=sandbox-"],
                capture_output=True, text=True,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return counts
            ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if not ids:
                return counts
            inspect = subprocess.run(
                ["docker", "inspect"] + ids,
                capture_output=True, text=True,
            )
            if inspect.returncode != 0:
                return counts
            containers = json.loads(inspect.stdout)
            workspace_to_agent = {
                str(path): agent_id for agent_id, path in self._workspace_dirs.items()
            }
            for container in containers:
                for mount in container.get("Mounts", []):
                    source = mount.get("Source")
                    agent_id = workspace_to_agent.get(source)
                    if agent_id:
                        counts[agent_id] += 1
                        break
        except subprocess.SubprocessError:
            pass
        return counts

    def get_agent_gateways(self) -> Dict[str, int]:
        return dict(self._host_ports)

    def collect_agent_logs(self, agent_ids: List[str], output_dir) -> None:
        output_dir = Path(output_dir)
        for i, agent_id in enumerate(agent_ids):
            container_name = f"bench-ic-agent-{i}"
            log_file = output_dir / f"{agent_id}.jsonl"
            try:
                result = subprocess.run(
                    ["docker", "logs", container_name],
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    log_file.write_text(result.stdout + result.stderr)
                    print(f"[{self.name}] Collected logs for {agent_id}")
            except subprocess.SubprocessError as e:
                print(f"[{self.name}] Failed to collect logs for {agent_id}: {e}")

    def stop_agents(self) -> None:
        for i in range(len(self._agent_ids)):
            container_name = f"bench-ic-agent-{i}"
            subprocess.run(
                ["docker", "stop", "-t", "10", container_name],
                capture_output=True,
            )
        # Also stop any lingering sandbox containers
        try:
            result = subprocess.run(
                ["docker", "ps", "-q", "--filter", "name=sandbox-"],
                capture_output=True, text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                ids = result.stdout.strip().split("\n")
                subprocess.run(
                    ["docker", "rm", "-f"] + ids, capture_output=True,
                )
        except subprocess.SubprocessError:
            pass
        print(f"[{self.name}] All agents stopped.")

    def cleanup(self) -> None:
        self.stop_agents()
        # Remove agent containers
        result = subprocess.run(
            ["docker", "ps", "-aq",
             "--filter", f"label=bench_run_id={self._run_id}"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            ids = result.stdout.strip().split("\n")
            subprocess.run(["docker", "rm", "-f"] + ids, capture_output=True)
        # Remove host workspace temp dirs
        import shutil
        for agent_id, ws_dir in self._workspace_dirs.items():
            try:
                shutil.rmtree(ws_dir, ignore_errors=True)
            except Exception:
                pass
        self._agent_ids = []
        self._host_ports = {}
        self._workspace_dirs = {}
        print(f"[{self.name}] Cleanup complete.")
