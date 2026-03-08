"""
Approach A: VM-per-agent with QEMU/KVM.

Each agent runs in its own lightweight VM (Alpine Linux with Docker).
Inside each VM: Docker daemon + agent process + worker containers.
The VM overhead (guest kernel, guest Docker daemon, QEMU process) is what
we're measuring compared to the container approach.

Note on VM memory behavior: Once the guest touches pages, host pages
generally remain allocated to the VM even after the process inside the
guest frees them (unless ballooning/free-page reporting is enabled).
This means VM memory acts as a high-water mark, which is a real property
of the isolation approach and affects density economics.
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

APPROACH_DIR = Path(__file__).resolve().parent
VM_DIR = APPROACH_DIR / "vm_qemu_assets"
VM_IMAGE = VM_DIR / "alpine-agent.qcow2"
LAUNCH_SCRIPT = VM_DIR / "launch.sh"


class VmQemuApproach(Approach):
    """VM-per-agent isolation using QEMU/KVM."""

    def __init__(self):
        self._agent_ids: List[str] = []
        self._host_ports: Dict[str, int] = {}  # agent_id → host port
        self._config: BenchmarkConfig = None

    def _abort_startup(self, message: str) -> None:
        """Stop partially started VMs before surfacing a fatal startup error."""
        try:
            self.cleanup()
        except Exception as cleanup_error:
            raise RuntimeError(
                f"{message} Cleanup also failed: {cleanup_error}"
            ) from cleanup_error
        raise RuntimeError(message)

    @property
    def name(self) -> str:
        return "vm-qemu"

    def setup(self, config: BenchmarkConfig) -> None:
        """Verify QEMU and VM image are available."""
        self._config = config

        # Check for QEMU
        result = subprocess.run(
            ["qemu-system-x86_64", "--version"],
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError("qemu-system-x86_64 not found. Install QEMU first.")

        # Check for KVM
        if not os.path.exists("/dev/kvm"):
            raise RuntimeError("/dev/kvm not found. KVM support required.")

        # Check for VM image
        if not VM_IMAGE.exists():
            raise RuntimeError(
                f"VM image not found at {VM_IMAGE}. "
                "Run 'make vm-image' first."
            )

        print(f"[{self.name}] Setup complete. QEMU and VM image verified.")

    def start_agents(self, n: int, config: BenchmarkConfig) -> List[str]:
        """Start N QEMU VMs, each running an agent."""
        self._agent_ids = []

        for i in range(n):
            agent_id = f"agent-{i}"

            # Build environment variables to pass into the VM
            env_vars = [
                f"AGENT_BASELINE_MB={config.agent_baseline_mb}",
                f"BENCHMARK_MODE={config.benchmark_mode}",
                f"SPAWN_INTERVAL_MEAN_S={config.spawn_interval_mean_s}",
                f"MAX_CONCURRENT_WORKERS={config.max_concurrent_workers}",
                f"BENCHMARK_DURATION_S={config.benchmark_duration_s}",
                f"WORKER_IMAGE={config.worker_image}",
                f"WORKER_MEMORY_LIMIT_MB={config.worker_memory_limit_mb}",
                f"WORKER_MEMORY_MB={config.worker_memory_mb}",
                f"WORKER_DURATION_MIN_S={config.worker_duration_min_s}",
                f"WORKER_DURATION_MAX_S={config.worker_duration_max_s}",
                f"WORKER_LIFETIME_MODE={config.worker_lifetime_mode}",
                f"PLATEAU_WORKERS_PER_AGENT={config.plateau_workers_csv()}",
                f"PLATEAU_HOLD_S={config.plateau_hold_s}",
                f"PLATEAU_SETTLE_S={config.plateau_settle_s}",
                f"RNG_SEED={config.rng_seed}",
                f"BENCH_RUN_ID={config.run_id}",
                f"BENCH_APPROACH={self.name}",
                "ORCHESTRATOR_PORT=8080",
            ]

            # Storage validation: in-VM Docker resolves paths within VM
            env_vars += [
                "STORAGE_VALIDATION=1",
                "WORKSPACE_BASE=/tmp/bench-workspaces",
            ]

            cmd = [
                str(LAUNCH_SCRIPT), "start",
                agent_id,
                str(config.agent_memory_mb),
                "2",  # CPUs per VM
                str(VM_IMAGE),
            ] + env_vars

            host_port = config.orchestrator_base_port + i
            env = os.environ.copy()
            env["ORCH_HOST_PORT"] = str(host_port)

            result = subprocess.run(cmd, capture_output=True, text=True, env=env)
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to start VM {agent_id}: {result.stderr.strip()}"
                )

            self._agent_ids.append(agent_id)
            self._host_ports[agent_id] = host_port
            print(f"[{self.name}] Started VM {agent_id} (orch port {host_port})")

        # Wait for each VM to emit agent_start (max 120s)
        vm_base = Path(os.environ.get("VM_BASE_DIR", "/tmp/bench-vms"))
        print(f"[{self.name}] Waiting for {n} VMs to boot and emit agent_start...")
        deadline = time.monotonic() + 120
        ready = set()
        while time.monotonic() < deadline and len(ready) < n:
            for agent_id in self._agent_ids:
                if agent_id in ready:
                    continue
                jsonl = vm_base / agent_id / "shared" / "agent.jsonl"
                if jsonl.exists():
                    try:
                        with open(jsonl) as f:
                            for line in f:
                                if '"agent_start"' in line:
                                    ready.add(agent_id)
                                    break
                    except OSError:
                        pass
            if len(ready) < n:
                time.sleep(2)

        if len(ready) < n:
            missing = set(self._agent_ids) - ready
            self._abort_startup(
                f"{len(missing)} VMs never emitted agent_start: {sorted(missing)}"
            )

        # Verify /health reachable via port forward for ready VMs
        print(f"[{self.name}] Verifying HTTP port forwards...")
        unhealthy = []
        for agent_id in ready:
            port = self._host_ports.get(agent_id)
            if port is None:
                continue
            reachable = False
            for _ in range(15):  # 30s max
                try:
                    resp = urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health", timeout=2
                    )
                    if resp.status == 200:
                        reachable = True
                        break
                except Exception:
                    time.sleep(2)
            if not reachable:
                unhealthy.append(f"{agent_id}:{port}")

        if unhealthy:
            self._abort_startup(
                "VM agent /health endpoint never became reachable via port "
                f"forward: {', '.join(unhealthy)}"
            )

        print(f"[{self.name}] {len(ready)}/{n} VMs started.")
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
        """Get QEMU process PIDs from pidfiles."""
        pids = {}
        vm_base = Path(os.environ.get("VM_BASE_DIR", "/tmp/bench-vms"))
        for agent_id in self._agent_ids:
            pidfile = vm_base / agent_id / "qemu.pid"
            try:
                if pidfile.exists():
                    pid = int(pidfile.read_text().strip())
                    # Verify process still exists
                    os.kill(pid, 0)
                    pids[agent_id] = pid
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        return pids

    def count_active_workers(self) -> int:
        """Count workers across all VMs by querying each agent's /status endpoint."""
        total = 0
        for agent_id, port in self._host_ports.items():
            try:
                resp = urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/status", timeout=2
                )
                data = json.loads(resp.read())
                total += data.get("active_workers", 0)
            except Exception:
                pass
        return total

    def collect_agent_logs(self, agent_ids: List[str], output_dir) -> None:
        """Collect agent JSONL from 9p shared dir and console.log as debug artifact."""
        output_dir = Path(output_dir)
        vm_base = Path(os.environ.get("VM_BASE_DIR", "/tmp/bench-vms"))
        for agent_id in agent_ids:
            vm_dir = vm_base / agent_id
            # Primary: clean JSONL from 9p share
            jsonl_src = vm_dir / "shared" / "agent.jsonl"
            if jsonl_src.exists():
                try:
                    shutil.copy2(jsonl_src, output_dir / f"{agent_id}.jsonl")
                    print(f"[{self.name}] Collected JSONL for {agent_id}")
                except Exception as e:
                    print(f"[{self.name}] Failed to collect JSONL for {agent_id}: {e}")
            # Also keep console.log as debug artifact
            console_src = vm_dir / "console.log"
            if console_src.exists():
                try:
                    shutil.copy2(console_src, output_dir / f"{agent_id}.log")
                    print(f"[{self.name}] Collected console log for {agent_id}")
                except Exception as e:
                    print(f"[{self.name}] Failed to collect log for {agent_id}: {e}")

    def live_event_log_paths(
        self,
        agent_ids: List[str],
        output_dir: Path,
    ) -> Dict[str, Path]:
        vm_base = Path(os.environ.get("VM_BASE_DIR", "/tmp/bench-vms"))
        return {
            agent_id: vm_base / agent_id / "shared" / "agent.jsonl"
            for agent_id in agent_ids
        }

    def stop_agents(self) -> None:
        """Stop all VMs."""
        result = subprocess.run(
            [str(LAUNCH_SCRIPT), "stop-all"],
            capture_output=True,
            text=True,
        )
        self._agent_ids = []
        print(f"[{self.name}] All VMs stopped.")

    def remove_containers(self) -> None:
        """Remove VM directories (logs, overlays, pidfiles)."""
        result = subprocess.run(
            [str(LAUNCH_SCRIPT), "clean-all"],
            capture_output=True,
            text=True,
        )
        print(f"[{self.name}] All VM directories cleaned.")

    def cleanup(self) -> None:
        """Stop VMs and clean up directories."""
        self.stop_agents()
        self.remove_containers()
