"""
Approach: Real IronClaw agents with Firecracker-backed worker VMs.

Each agent runs as a Docker container hosting the real IronClaw binary and the
mock LLM server. Benchmark jobs are allocated as real worker jobs via the
orchestrator's callback API, but the actual worker process runs inside a
Firecracker microVM launched from the agent container.

This keeps the parent agent as real IronClaw while exercising a microVM worker
callback path without requiring a first-class Firecracker sandbox backend in
core IronClaw.
"""

import base64
import subprocess
import time
from pathlib import Path
from typing import Dict, List

from approaches.base import Approach, BenchmarkConfig
from approaches._ironclaw_helpers import (
    IRONCLAW_AGENT_FC_IMAGE,
    ironclaw_agent_env,
    prepare_agent_host_dirs,
    wait_for_gateway,
)

APPROACH_DIR = Path(__file__).resolve().parent
FC_ASSETS_DIR = APPROACH_DIR / "hybrid_firecracker_assets"
KERNEL_FILE = FC_ASSETS_DIR / "vmlinux-5.10.bin"
ROOTFS_FILE = FC_ASSETS_DIR / "ironclaw-worker-rootfs.ext4"


class IronclawHybridFirecrackerApproach(Approach):
    def __init__(self):
        self._agent_ids: List[str] = []
        self._host_ports: Dict[str, int] = {}
        self._agent_roots: Dict[str, Path] = {}
        self._run_id: str = "unknown"

    @property
    def suite(self) -> str:
        return "ironclaw"

    @property
    def name(self) -> str:
        return "ironclaw-hybrid-firecracker"

    def _find_firecracker(self) -> str | None:
        result = subprocess.run(["which", "firecracker"], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        for path in ("/usr/local/bin/firecracker", "/usr/bin/firecracker"):
            candidate = Path(path)
            if candidate.is_file() and candidate.stat().st_mode & 0o111:
                return path
        return None

    def setup(self, config: BenchmarkConfig) -> None:
        self._run_id = config.run_id
        if not Path("/dev/kvm").exists():
            raise RuntimeError("/dev/kvm not found. KVM is required.")
        fc_bin = self._find_firecracker()
        if fc_bin is None:
            raise RuntimeError("firecracker binary not found. Install Firecracker first.")
        if not KERNEL_FILE.exists():
            raise RuntimeError(f"Missing Firecracker kernel at {KERNEL_FILE}. Run 'make fc-kernel'.")
        if not ROOTFS_FILE.exists():
            raise RuntimeError(
                f"Missing IronClaw Firecracker rootfs at {ROOTFS_FILE}. Run 'make ironclaw-fc-rootfs'."
            )
        result = subprocess.run(["docker", "image", "inspect", IRONCLAW_AGENT_FC_IMAGE], capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"Image '{IRONCLAW_AGENT_FC_IMAGE}' not found. Run 'make ironclaw-agent-fc-image'."
            )

    def start_agents(self, n: int, config: BenchmarkConfig) -> List[str]:
        self._agent_ids = []
        self._host_ports = {}
        self._agent_roots = {}
        fc_bin = self._find_firecracker()
        if fc_bin is None:
            raise RuntimeError("firecracker binary not found")

        for i in range(n):
            agent_id = f"agent-{i}"
            container_name = f"bench-ic-agent-{i}"
            gateway_port = config.orchestrator_base_port + i
            host_dirs = prepare_agent_host_dirs(config, agent_id)

            env = ironclaw_agent_env(config, agent_id, 3000)
            env["WORKSPACE_DIR"] = str(host_dirs["workspace_dir"])
            env["IRONCLAW_BASE_DIR"] = str(host_dirs["base_dir"])
            env["BENCH_EVIDENCE_DIR"] = str(host_dirs["evidence_dir"])
            env["LIBSQL_PATH"] = str(host_dirs["base_dir"] / "ironclaw.db")
            env["FC_VM_DIR"] = str(host_dirs["base_dir"] / "firecracker-vms")
            env["FC_KERNEL_PATH"] = "/opt/vmlinux"
            env["FC_ROOTFS_PATH"] = "/opt/ironclaw-worker-rootfs.ext4"
            env["FC_VM_MEMORY_MB"] = str(max(config.worker_memory_mb, 256))
            env["IRONCLAW_BENCHMARK_EXTERNAL_WORKERS_ONLY"] = "true"

            cmd = [
                "docker", "run", "-d",
                "--name", container_name,
                "--memory", f"{config.agent_memory_mb}m",
                "--device", "/dev/kvm",
                "--device", "/dev/net/tun",
                "--cap-add", "NET_ADMIN",
                "-p", f"{gateway_port}:3000",
                "-v", f"{fc_bin}:/usr/local/bin/firecracker:ro",
                "-v", f"{KERNEL_FILE}:/opt/vmlinux:ro",
                "-v", f"{ROOTFS_FILE}:/opt/ironclaw-worker-rootfs.ext4:ro",
                "-v", f"{host_dirs['agent_root']}:{host_dirs['agent_root']}:rw",
                "--label", f"bench_run_id={config.run_id}",
                "--label", "bench_role=agent",
                "--label", f"bench_agent_id={agent_id}",
                "--label", f"bench_approach={self.name}",
            ]
            for k, v in env.items():
                cmd += ["-e", f"{k}={v}"]
            cmd.append(IRONCLAW_AGENT_FC_IMAGE)

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"Failed to start {agent_id}: {result.stderr.strip()}")

            self._agent_ids.append(agent_id)
            self._host_ports[agent_id] = gateway_port
            self._agent_roots[agent_id] = host_dirs["agent_root"]

        for agent_id, port in self._host_ports.items():
            if not wait_for_gateway(port, timeout_s=120, label=agent_id):
                raise RuntimeError(f"{agent_id} gateway not ready after 120s on port {port}")
        return list(self._agent_ids)

    def get_agent_pids(self) -> Dict[str, int]:
        pids = {}
        for i, agent_id in enumerate(self._agent_ids):
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Pid}}", f"bench-ic-agent-{i}"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                try:
                    pid = int(result.stdout.strip())
                except ValueError:
                    continue
                if pid > 0:
                    pids[agent_id] = pid
        return pids

    def get_daemon_pids(self) -> Dict[str, int]:
        pids = {}
        for daemon_name in ("dockerd", "containerd"):
            result = subprocess.run(["pgrep", "-x", daemon_name], capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                try:
                    pids[daemon_name] = int(result.stdout.strip().splitlines()[0])
                except ValueError:
                    pass
        return pids

    def count_active_workers(self) -> int:
        return sum(self.count_active_workers_per_agent().values())

    def count_active_workers_per_agent(self) -> Dict[str, int]:
        counts = {}
        for i, agent_id in enumerate(self._agent_ids):
            result = subprocess.run(
                ["docker", "exec", f"bench-ic-agent-{i}", "pgrep", "-c", "firecracker"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                try:
                    counts[agent_id] = int(result.stdout.strip())
                except ValueError:
                    counts[agent_id] = 0
            else:
                counts[agent_id] = 0
        return counts

    def get_agent_gateways(self) -> Dict[str, int]:
        return dict(self._host_ports)

    def get_agent_roots(self) -> Dict[str, Path]:
        return dict(self._agent_roots)

    def trigger_worker_spawn(
        self,
        agent_id: str,
        command: str | None = None,
        dispatch_mode: str = "worker-job",
    ) -> bool:
        del dispatch_mode
        idx = agent_id.split("-")[-1]
        payload = base64.b64encode((command or "").encode()).decode()
        cmd = (
            "python3 /opt/firecracker_bench_launch.py "
            f"--command-b64 '{payload}' "
            f">/tmp/fc-launch-{int(time.time() * 1000)}.log 2>&1 &"
        )
        result = subprocess.run(
            ["docker", "exec", f"bench-ic-agent-{idx}", "sh", "-lc", cmd],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0

    def verify_worker_absent(self, agent_id: str, job_id: str) -> bool | None:
        vm_dir = self._agent_roots.get(agent_id)
        if vm_dir is None:
            return None
        return not (vm_dir / "ironclaw" / "firecracker-vms" / job_id).exists()

    def verify_agent_absent(self, agent_id: str) -> bool:
        idx = agent_id.split("-")[-1]
        result = subprocess.run(["docker", "inspect", f"bench-ic-agent-{idx}"], capture_output=True, text=True)
        return result.returncode != 0

    def collect_agent_logs(self, agent_ids: List[str], output_dir) -> None:
        output_dir = Path(output_dir)
        for agent_id in agent_ids:
            idx = agent_id.split("-")[-1]
            result = subprocess.run(["docker", "logs", f"bench-ic-agent-{idx}"], capture_output=True, text=True)
            if result.returncode == 0:
                (output_dir / f"{agent_id}.jsonl").write_text(result.stdout + result.stderr)

    def stop_agents(self) -> None:
        for i in range(len(self._agent_ids)):
            subprocess.run(["docker", "stop", "-t", "10", f"bench-ic-agent-{i}"], capture_output=True)
        self._agent_ids = []
        self._host_ports = {}

    def force_cleanup(self) -> None:
        result = subprocess.run(
            ["docker", "ps", "-aq",
             "--filter", f"label=bench_run_id={self._run_id}"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            ids = result.stdout.strip().splitlines()
            subprocess.run(["docker", "rm", "-f"] + ids, capture_output=True)
        self._agent_ids = []
        self._host_ports = {}
        self._agent_roots = {}
        print(f"[{self.name}] Force cleanup complete.")

    def cleanup(self) -> None:
        self.stop_agents()
        for i in range(0, 512):
            subprocess.run(["docker", "rm", "-f", f"bench-ic-agent-{i}"], capture_output=True)
        self._agent_roots = {}
