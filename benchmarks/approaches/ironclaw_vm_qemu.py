"""
Approach: Real IronClaw in QEMU/KVM virtual machines.

Each agent runs in its own QEMU VM with Alpine Linux, Docker daemon,
ironclaw binary, and mock LLM server. Workers are sandbox containers
spawned by ironclaw inside the VM's Docker daemon.

This approach provides the strongest isolation (full hardware VM boundary)
at the cost of higher overhead.

NOTE: Requires a custom VM image with ironclaw pre-installed.
Run `make ironclaw-vm-image` to build it.
"""

import os
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, List

from approaches.base import Approach, BenchmarkConfig
from approaches._ironclaw_helpers import ironclaw_agent_env, wait_for_gateway

APPROACH_DIR = Path(__file__).resolve().parent
BENCH_DIR = APPROACH_DIR.parent
VM_IMAGE_DIR = BENCH_DIR / "vm"
VM_ASSETS_DIR = APPROACH_DIR / "vm_qemu_assets"
IRONCLAW_VM_IMAGE = VM_IMAGE_DIR / "ironclaw-agent.qcow2"
LAUNCH_SCRIPT = VM_ASSETS_DIR / "launch.sh"


class IronclawVmQemuApproach(Approach):
    """Real ironclaw in QEMU/KVM VM with inner Docker daemon."""

    def __init__(self):
        self._agent_ids: List[str] = []
        self._host_ports: Dict[str, int] = {}
        self._agent_roots: Dict[str, Path] = {}
        self._vm_base_dir: Path | None = None
        self._config: BenchmarkConfig = None

    @property
    def suite(self) -> str:
        return "ironclaw"

    @property
    def name(self) -> str:
        return "ironclaw-vm-qemu"

    def setup(self, config: BenchmarkConfig) -> None:
        self._config = config

        # Check QEMU
        result = subprocess.run(
            ["qemu-system-x86_64", "--version"], capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError("qemu-system-x86_64 not found.")

        # Check KVM
        if not os.path.exists("/dev/kvm"):
            raise RuntimeError("/dev/kvm not found. KVM required.")

        # Check VM image
        if not IRONCLAW_VM_IMAGE.exists():
            raise RuntimeError(
                f"VM image not found at {IRONCLAW_VM_IMAGE}. "
                "Run 'make ironclaw-vm-image' first."
            )

        if not LAUNCH_SCRIPT.exists():
            raise RuntimeError(f"VM launch script not found at {LAUNCH_SCRIPT}")

        print(f"[{self.name}] Setup complete.")

    def start_agents(self, n: int, config: BenchmarkConfig) -> List[str]:
        self._agent_ids = []
        self._host_ports = {}
        self._agent_roots = {}
        self._vm_base_dir = Path(config.run_dir) / "agents" if config.run_dir else Path("/tmp/ironclaw-bench-vms")
        self._vm_base_dir.mkdir(parents=True, exist_ok=True)

        for i in range(n):
            agent_id = f"agent-{i}"
            gateway_port = config.orchestrator_base_port + i

            env = ironclaw_agent_env(config, agent_id, 3000)
            agent_root = self._vm_base_dir / agent_id / "shared"
            for path in (
                agent_root,
                agent_root / "workspace",
                agent_root / "ironclaw",
                agent_root / "evidence",
            ):
                path.mkdir(parents=True, exist_ok=True)
                path.chmod(0o777)
            self._agent_roots[agent_id] = agent_root

            env["WORKSPACE_DIR"] = "/mnt/benchshare/workspace"
            env["BENCH_EVIDENCE_DIR"] = "/mnt/benchshare/evidence"
            # Keep projects/evidence host-visible on the 9p share so the smoke
            # harness can inspect worker lifecycle artifacts, but place the
            # SQLite database itself on guest-local storage to avoid 9p I/O
            # errors during migrations.
            env["IRONCLAW_BASE_DIR"] = "/mnt/benchshare/ironclaw"
            env["LIBSQL_PATH"] = "/var/lib/ironclaw-bench/ironclaw.db"

            env_pairs = [f"{k}={v}" for k, v in env.items()]
            cmd = [
                str(LAUNCH_SCRIPT),
                "start",
                agent_id,
                str(max(config.agent_memory_mb, 2048)),
                "2",
                str(IRONCLAW_VM_IMAGE),
            ] + env_pairs

            launch_env = os.environ.copy()
            launch_env["VM_BASE_DIR"] = str(self._vm_base_dir)
            launch_env["ORCH_HOST_PORT"] = str(gateway_port)
            launch_env["GATEWAY_GUEST_PORT"] = "3000"
            result = subprocess.run(cmd, capture_output=True, text=True, env=launch_env)
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to start VM for {agent_id}: {result.stderr.strip() or result.stdout.strip()}"
                )

            self._agent_ids.append(agent_id)
            self._host_ports[agent_id] = gateway_port
            print(f"[{self.name}] Started VM {agent_id}")

        # Wait for VMs to boot and ironclaw to start
        print(f"[{self.name}] Waiting for {n} VMs to boot...")
        for agent_id, port in self._host_ports.items():
            if wait_for_gateway(port, timeout_s=300, label=agent_id):
                print(f"[{self.name}] {agent_id} healthy")
            else:
                print(f"[{self.name}] WARNING: {agent_id} not ready after 300s")

        print(f"[{self.name}] {len(self._agent_ids)} VMs started.")
        return list(self._agent_ids)

    def start_benchmark(self) -> None:
        pass

    def get_agent_pids(self) -> Dict[str, int]:
        pids = {}
        for agent_id in self._agent_ids:
            if self._vm_base_dir is None:
                continue
            pid_file = self._vm_base_dir / agent_id / "qemu.pid"
            try:
                pid = int(pid_file.read_text().strip())
                if pid > 0:
                    pids[agent_id] = pid
            except (FileNotFoundError, ValueError):
                pass
        return pids

    def get_daemon_pids(self) -> Dict[str, int]:
        # No host-side daemons for VM approach
        return {}

    def count_active_workers(self) -> int:
        return sum(self.count_active_workers_per_agent().values())

    def count_active_workers_per_agent(self) -> Dict[str, int]:
        counts = {}
        for agent_id, agent_root in self._agent_roots.items():
            evidence_dir = agent_root / "evidence"
            created = {
                path.stem.removeprefix("job-created-")
                for path in evidence_dir.glob("job-created-*.json")
            }
            cleaned = {
                path.stem.removeprefix("worker-cleaned-")
                for path in evidence_dir.glob("worker-cleaned-*.json")
            }
            counts[agent_id] = len(created - cleaned)
        return counts

    def get_agent_gateways(self) -> Dict[str, int]:
        return dict(self._host_ports)

    def get_agent_roots(self) -> Dict[str, Path]:
        return dict(self._agent_roots)

    def translate_agent_path(self, agent_id: str, path: str | Path | None) -> Path | None:
        if path is None:
            return None
        raw = str(path)
        if raw.startswith("/mnt/benchshare/"):
            root = self._agent_roots.get(agent_id)
            if root is None:
                return None
            return root / raw.removeprefix("/mnt/benchshare/")
        return Path(raw)

    def verify_worker_absent(self, agent_id: str, job_id: str) -> bool | None:
        evidence = self._agent_roots.get(agent_id)
        if evidence is None:
            return None
        payload_path = evidence / "evidence" / f"worker-cleaned-{job_id}.json"
        if not payload_path.exists():
            return None
        try:
            payload = json.loads(payload_path.read_text())
            return bool(payload.get("container_removed"))
        except Exception:
            return None

    def verify_agent_absent(self, agent_id: str) -> bool | None:
        if self._vm_base_dir is None:
            return None
        pid_file = self._vm_base_dir / agent_id / "qemu.pid"
        if not pid_file.exists():
            return True
        try:
            pid = int(pid_file.read_text().strip())
        except ValueError:
            return True
        return not Path(f"/proc/{pid}").exists()

    def collect_agent_logs(self, agent_ids: List[str], output_dir) -> None:
        output_dir = Path(output_dir)
        for agent_id in agent_ids:
            if self._vm_base_dir is None:
                continue
            for name in ("console.log", "qemu.log"):
                src = self._vm_base_dir / agent_id / name
                if src.exists():
                    dst = output_dir / f"{agent_id}-{name}"
                    shutil.copy2(src, dst)
                    print(f"[{self.name}] Collected {name} for {agent_id}")

    def stop_agents(self) -> None:
        if self._vm_base_dir is None:
            return
        env = os.environ.copy()
        env["VM_BASE_DIR"] = str(self._vm_base_dir)
        for agent_id in self._agent_ids:
            subprocess.run(
                [str(LAUNCH_SCRIPT), "stop", agent_id],
                capture_output=True,
                env=env,
            )
        print(f"[{self.name}] All VMs stopped.")

    def cleanup(self) -> None:
        self.stop_agents()
        if self._vm_base_dir is not None:
            env = os.environ.copy()
            env["VM_BASE_DIR"] = str(self._vm_base_dir)
            for agent_id in self._agent_ids:
                subprocess.run(
                    [str(LAUNCH_SCRIPT), "clean", agent_id],
                    capture_output=True,
                    env=env,
                )
        self._agent_ids = []
        self._host_ports = {}
        self._agent_roots = {}
        print(f"[{self.name}] Cleanup complete.")
