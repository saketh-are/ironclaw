"""
Approaches: Real IronClaw with per-agent Docker daemon (DinD).

Two variants sharing nearly identical logic:
  - ironclaw-gvisor-dind: agent in gVisor container with inner dockerd
  - ironclaw-sysbox-dind: agent in Sysbox container with inner dockerd

Each agent gets its own Docker daemon. Ironclaw's SandboxManager uses the
inner socket at /var/run/docker.sock to create sandbox workers. Workers are
fully isolated inside the agent's daemon namespace.
"""

import json
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Dict, List

from approaches.base import Approach, BenchmarkConfig
from approaches._ironclaw_helpers import (
    GATEWAY_AUTH_TOKEN,
    IRONCLAW_AGENT_DIND_IMAGE,
    IRONCLAW_SANDBOX_IMAGE,
    SANDBOX_WORKER_TAR_PATH,
    ironclaw_agent_env,
    wait_for_gateway,
    trigger_worker_spawn,
)  # noqa: F401 — SANDBOX_IMAGE and TAR_PATH kept for DinD setup logic


class _IronclawDindBase(Approach):
    """Shared logic for gVisor and Sysbox DinD ironclaw approaches."""

    _runtime: str = ""
    _extra_docker_args: List[str] = []
    _dockerd_extra_args: str = ""

    @property
    def suite(self) -> str:
        return "ironclaw"

    def __init__(self):
        self._agent_ids: List[str] = []
        self._host_ports: Dict[str, int] = {}
        self._run_id: str = "unknown"

    def setup(self, config: BenchmarkConfig) -> None:
        self._run_id = config.run_id

        # Verify DinD agent image
        result = subprocess.run(
            ["docker", "image", "inspect", IRONCLAW_AGENT_DIND_IMAGE],
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Image '{IRONCLAW_AGENT_DIND_IMAGE}' not found. "
                "Run 'make ironclaw-images' first."
            )

        # Verify sandbox worker image
        result = subprocess.run(
            ["docker", "image", "inspect", IRONCLAW_SANDBOX_IMAGE],
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Image '{IRONCLAW_SANDBOX_IMAGE}' not found. "
                "Run 'make ironclaw-images' first."
            )

        # Verify runtime
        if self._runtime:
            result = subprocess.run(
                ["docker", "info", "--format", "{{.Runtimes}}"],
                capture_output=True, text=True,
            )
            if self._runtime not in result.stdout:
                raise RuntimeError(
                    f"Runtime '{self._runtime}' not found in Docker."
                )

        # Save sandbox worker image to tarball for inner daemon loading
        print(f"[{self.name}] Saving sandbox worker image to tarball...")
        result = subprocess.run(
            ["docker", "save", "-o", SANDBOX_WORKER_TAR_PATH,
             IRONCLAW_SANDBOX_IMAGE],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to save worker image: {result.stderr.strip()}"
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
            # Inner dockerd args
            env["DOCKERD_EXTRA_ARGS"] = self._dockerd_extra_args

            cmd = [
                "docker", "run", "-d",
                "--name", container_name,
                "--memory", f"{config.agent_memory_mb}m",
                "-p", f"{gateway_port}:3000",
                # Mount worker image tarball
                "-v", f"{SANDBOX_WORKER_TAR_PATH}:/opt/.worker-image.tar:ro",
                # Labels
                "--label", f"bench_run_id={config.run_id}",
                "--label", "bench_role=agent",
                "--label", f"bench_agent_id={agent_id}",
                "--label", f"bench_approach={self.name}",
            ]
            # Runtime-specific args
            if self._runtime:
                cmd += [f"--runtime={self._runtime}"]
            cmd += self._extra_docker_args

            for k, v in env.items():
                cmd += ["-e", f"{k}={v}"]
            cmd.append(IRONCLAW_AGENT_DIND_IMAGE)

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to start {agent_id}: {result.stderr.strip()}"
                )

            self._agent_ids.append(agent_id)
            self._host_ports[agent_id] = gateway_port
            print(f"[{self.name}] Started {container_name}")

        # DinD startup is slower (dockerd + image load + ironclaw startup)
        print(f"[{self.name}] Waiting for {n} agents "
              "(dockerd startup + image load + ironclaw)...")
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
        """Query each agent's inner Docker daemon for sandbox containers."""
        total = 0
        for i in range(len(self._agent_ids)):
            container_name = f"bench-ic-agent-{i}"
            try:
                result = subprocess.run(
                    ["docker", "exec", container_name,
                     "docker", "ps", "-q", "--filter", "name=sandbox-"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    total += len(result.stdout.strip().split("\n"))
            except (subprocess.SubprocessError, subprocess.TimeoutExpired):
                pass
        return total

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
                print(f"[{self.name}] Log collection failed for {agent_id}: {e}")

    def stop_agents(self) -> None:
        for i in range(len(self._agent_ids)):
            container_name = f"bench-ic-agent-{i}"
            subprocess.run(
                ["docker", "stop", "-t", "10", container_name],
                capture_output=True,
            )
        print(f"[{self.name}] All agents stopped.")

    def cleanup(self) -> None:
        self.stop_agents()
        result = subprocess.run(
            ["docker", "ps", "-aq",
             "--filter", f"label=bench_run_id={self._run_id}"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            ids = result.stdout.strip().split("\n")
            subprocess.run(["docker", "rm", "-f"] + ids, capture_output=True)
        self._agent_ids = []
        self._host_ports = {}
        print(f"[{self.name}] Cleanup complete.")


class IronclawGvisorDindApproach(_IronclawDindBase):
    """Real ironclaw in gVisor container with private Docker daemon."""

    _runtime = "runsc"
    _extra_docker_args = ["--cap-add", "ALL"]
    _dockerd_extra_args = "--iptables=false --ip6tables=false --storage-driver=vfs"

    @property
    def name(self) -> str:
        return "ironclaw-gvisor-dind"


class IronclawSysboxDindApproach(_IronclawDindBase):
    """Real ironclaw in Sysbox container with private Docker daemon."""

    _runtime = "sysbox-runc"
    _extra_docker_args = []
    _dockerd_extra_args = ""

    @property
    def name(self) -> str:
        return "ironclaw-sysbox-dind"
