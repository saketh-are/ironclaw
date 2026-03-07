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

import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Dict, List

from approaches.base import Approach, BenchmarkConfig
from approaches._ironclaw_helpers import (
    GATEWAY_AUTH_TOKEN,
    ironclaw_agent_env,
    wait_for_gateway,
    trigger_worker_spawn,
)

BENCH_DIR = Path(__file__).resolve().parent.parent
VM_DIR = BENCH_DIR / "vm"
# The ironclaw VM image is separate from the synthetic benchmark's image.
IRONCLAW_VM_IMAGE = VM_DIR / "ironclaw-agent.qcow2"
LAUNCH_SCRIPT = VM_DIR / "launch.sh"


class IronclawVmQemuApproach(Approach):
    """Real ironclaw in QEMU/KVM VM with inner Docker daemon."""

    def __init__(self):
        self._agent_ids: List[str] = []
        self._host_ports: Dict[str, int] = {}
        self._config: BenchmarkConfig = None

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

        print(f"[{self.name}] Setup complete.")

    def start_agents(self, n: int, config: BenchmarkConfig) -> List[str]:
        self._agent_ids = []
        self._host_ports = {}

        for i in range(n):
            agent_id = f"agent-{i}"
            gateway_port = config.orchestrator_base_port + i

            env = ironclaw_agent_env(config, agent_id, 3000)

            # Build env string for kernel command line
            env_pairs = " ".join(f"{k}={v}" for k, v in env.items())

            # Create per-VM overlay disk
            overlay_path = VM_DIR / f"overlay-{agent_id}.qcow2"
            subprocess.run([
                "qemu-img", "create", "-f", "qcow2",
                "-b", str(IRONCLAW_VM_IMAGE), "-F", "qcow2",
                str(overlay_path),
            ], check=True, capture_output=True)

            # Launch QEMU
            vm_memory = max(config.agent_memory_mb, 2048)
            cmd = [
                "qemu-system-x86_64",
                "-enable-kvm",
                "-m", str(vm_memory),
                "-smp", "2",
                "-display", "none",
                "-drive", f"file={overlay_path},format=qcow2,if=virtio",
                # Port forwarding: host gateway_port → guest 3000
                "-netdev", f"user,id=net0,hostfwd=tcp::{gateway_port}-:3000",
                "-device", "virtio-net-pci,netdev=net0",
                # Serial console to file for log collection
                "-serial", f"file:{VM_DIR / f'{agent_id}.log'}",
                "-daemonize",
                "-pidfile", str(VM_DIR / f"{agent_id}.pid"),
            ]

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to start VM for {agent_id}: {result.stderr.strip()}"
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
        for agent_id, port in self._host_ports.items():
            ok = trigger_worker_spawn(port)
            if not ok:
                print(f"[{self.name}] WARNING: trigger failed for {agent_id}")

    def get_agent_pids(self) -> Dict[str, int]:
        pids = {}
        for agent_id in self._agent_ids:
            pid_file = VM_DIR / f"{agent_id}.pid"
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
        # Cannot directly query inner Docker from host.
        # Approximate by querying each agent's gateway API.
        total = 0
        for agent_id, port in self._host_ports.items():
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/jobs",
                    headers={"Authorization": f"Bearer {GATEWAY_AUTH_TOKEN}"},
                )
                resp = urllib.request.urlopen(req, timeout=5)
                data = json.loads(resp.read())
                if isinstance(data, list):
                    total += len([j for j in data
                                  if j.get("status") == "in_progress"])
            except Exception:
                pass
        return total

    def collect_agent_logs(self, agent_ids: List[str], output_dir) -> None:
        output_dir = Path(output_dir)
        for agent_id in agent_ids:
            src = VM_DIR / f"{agent_id}.log"
            dst = output_dir / f"{agent_id}.log"
            if src.exists():
                shutil.copy2(src, dst)
                print(f"[{self.name}] Collected logs for {agent_id}")

    def stop_agents(self) -> None:
        for agent_id in self._agent_ids:
            pid_file = VM_DIR / f"{agent_id}.pid"
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 15)  # SIGTERM
            except (FileNotFoundError, ValueError, ProcessLookupError):
                pass
        # Wait briefly for graceful shutdown
        time.sleep(3)
        # Force kill any remaining
        for agent_id in self._agent_ids:
            pid_file = VM_DIR / f"{agent_id}.pid"
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 9)  # SIGKILL
            except (FileNotFoundError, ValueError, ProcessLookupError):
                pass
        print(f"[{self.name}] All VMs stopped.")

    def cleanup(self) -> None:
        self.stop_agents()
        # Clean up overlay images, PID files, and logs
        for agent_id in self._agent_ids:
            for ext in [".qcow2", ".pid", ".log"]:
                path = VM_DIR / f"{'overlay-' if ext == '.qcow2' else ''}{agent_id}{ext}"
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        self._agent_ids = []
        self._host_ports = {}
        print(f"[{self.name}] Cleanup complete.")
