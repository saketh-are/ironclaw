"""
Approach: Real IronClaw on shared Docker daemon.

Each agent runs as a Docker container with the host Docker socket bind-mounted.
Ironclaw's SandboxManager creates sandbox worker containers as siblings on the
host daemon — the same production topology ironclaw uses by default.

This is the simplest real-ironclaw approach: no inner daemon, no proxy needed.
"""

import json
import subprocess
import time
from pathlib import Path
from typing import Dict, List

from approaches.base import Approach, BenchmarkConfig
from approaches._ironclaw_helpers import (
    GATEWAY_AUTH_TOKEN,
    IRONCLAW_AGENT_IMAGE,
    IRONCLAW_SANDBOX_IMAGE,
    ironclaw_agent_env,
    prepare_agent_host_dirs,
    wait_for_gateway,
    trigger_worker_spawn,
)


class IronclawDockerApproach(Approach):
    """Real ironclaw agent with shared host Docker daemon."""

    def __init__(self):
        self._agent_ids: List[str] = []
        self._host_ports: Dict[str, int] = {}
        self._run_id: str = "unknown"
        self._workspace_dirs: Dict[str, Path] = {}
        self._agent_roots: Dict[str, Path] = {}

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
        self._workspace_dirs = {}
        self._agent_roots = {}

        for i in range(n):
            agent_id = f"agent-{i}"
            container_name = f"bench-ic-agent-{i}"
            gateway_port = config.orchestrator_base_port + i

            env = ironclaw_agent_env(config, agent_id, 3000)
            host_dirs = prepare_agent_host_dirs(config, agent_id)

            # In the shared-daemon topology, paths used by the agent must exist
            # on the host so sibling sandbox containers can bind-mount them.
            ws_host = host_dirs["workspace_dir"]
            self._workspace_dirs[agent_id] = ws_host
            self._agent_roots[agent_id] = host_dirs["agent_root"]

            # Override paths so they remain host-visible to sibling workers.
            env["WORKSPACE_DIR"] = str(ws_host)
            env["IRONCLAW_BASE_DIR"] = str(host_dirs["base_dir"])
            env["BENCH_EVIDENCE_DIR"] = str(host_dirs["evidence_dir"])
            # Real worker-mode jobs need a callback route back into the parent
            # agent's internal API. In the benchmark shared-daemon topology,
            # the cleanest route is to join the agent container's network namespace.
            env["IRONCLAW_WORKER_ORCHESTRATOR_URL"] = "http://127.0.0.1:50051"
            env["IRONCLAW_WORKER_NETWORK_MODE"] = f"container:{container_name}"

            cmd = [
                "docker", "run", "-d",
                "--name", container_name,
                "--memory", f"{config.agent_memory_mb}m",
                "-p", f"{gateway_port}:3000",
                # Shared host Docker socket
                "-v", "/var/run/docker.sock:/var/run/docker.sock",
                # Mount host-visible benchmark root at same path inside container.
                "-v", f"{host_dirs['agent_root']}:{host_dirs['agent_root']}:rw",
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
                ["docker", "ps", "-q", "--filter", "name=ironclaw-worker-"],
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

    def get_agent_roots(self) -> Dict[str, Path]:
        return dict(self._agent_roots)

    def verify_worker_absent(self, agent_id: str, job_id: str) -> bool | None:
        result = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"name=ironclaw-worker-{job_id}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return None
        return not result.stdout.strip()

    def verify_agent_absent(self, agent_id: str) -> bool:
        idx = agent_id.split("-")[-1]
        result = subprocess.run(
            ["docker", "inspect", f"bench-ic-agent-{idx}"],
            capture_output=True, text=True,
        )
        return result.returncode != 0

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
                ["docker", "ps", "-q", "--filter", "name=ironclaw-worker-"],
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
        self._agent_roots = {}
        print(f"[{self.name}] Cleanup complete.")
