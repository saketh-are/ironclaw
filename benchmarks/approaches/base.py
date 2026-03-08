"""
Abstract base class for benchmark approaches.

Each approach implements a different isolation strategy for running agents.
The orchestrator uses this interface to start/stop agents and collect PIDs
for memory measurement, without knowing the details of the isolation mechanism.

To add a new approach (e.g., Podman rootless, Firecracker):
  1. Create a new file in approaches/ (e.g., podman_rootless.py)
  2. Implement a class that extends Approach
  3. Register it in approaches/__init__.py

Each approach belongs to a *suite*:
  - "synthetic" (default) — memory-pressure workloads driven by agent.py/worker.py
  - "ironclaw" — real IronClaw binary with mock LLM, sandbox containers
"""

import importlib
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class BenchmarkConfig:
    """Configuration passed to approaches from config.env / CLI."""

    # Benchmark mode
    benchmark_mode: str = "loaded"

    # Agent settings
    agent_memory_mb: int = 4096
    agent_baseline_mb: int = 50
    max_concurrent_workers: int = 5
    spawn_interval_mean_s: int = 30
    benchmark_duration_s: int = 300

    # Worker settings
    worker_image: str = "bench-worker:latest"
    worker_memory_limit_mb: int = 2048
    worker_memory_mb: int = 500
    worker_duration_min_s: int = 30
    worker_duration_max_s: int = 120
    worker_lifetime_mode: str = "timed"

    # Plateau mode
    plateau_workers_per_agent: List[int] = field(default_factory=list)
    plateau_hold_s: int = 60
    plateau_settle_s: int = 20

    # Networking
    orchestrator_base_port: int = 50100  # Starting host port for container approaches

    # Storage validation
    storage_validation: bool = False

    # Run metadata
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    run_dir: str = ""
    rng_seed: int = 42

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "BenchmarkConfig":
        """Load config from environment variables (or a dict)."""
        import os

        def parse_int_list(raw: str) -> List[int]:
            raw = (raw or "").strip()
            if not raw:
                return []
            return [int(part.strip()) for part in raw.split(",") if part.strip()]

        e = env or os.environ
        return cls(
            benchmark_mode=e.get("BENCHMARK_MODE", "loaded"),
            agent_memory_mb=int(e.get("AGENT_MEMORY_MB", "4096")),
            agent_baseline_mb=int(e.get("AGENT_BASELINE_MB", "50")),
            max_concurrent_workers=int(e.get("MAX_CONCURRENT_WORKERS", "5")),
            spawn_interval_mean_s=int(e.get("SPAWN_INTERVAL_MEAN_S", "30")),
            benchmark_duration_s=int(e.get("BENCHMARK_DURATION_S", "300")),
            worker_image=e.get("WORKER_IMAGE", "bench-worker:latest"),
            worker_memory_limit_mb=int(e.get("WORKER_MEMORY_LIMIT_MB", "2048")),
            worker_memory_mb=int(e.get("WORKER_MEMORY_MB", "500")),
            worker_duration_min_s=int(e.get("WORKER_DURATION_MIN_S", "30")),
            worker_duration_max_s=int(e.get("WORKER_DURATION_MAX_S", "120")),
            worker_lifetime_mode=e.get("WORKER_LIFETIME_MODE", "timed"),
            plateau_workers_per_agent=parse_int_list(
                e.get("PLATEAU_WORKERS_PER_AGENT", "")
            ),
            plateau_hold_s=int(e.get("PLATEAU_HOLD_S", "60")),
            plateau_settle_s=int(e.get("PLATEAU_SETTLE_S", "20")),
            orchestrator_base_port=int(e.get("ORCHESTRATOR_BASE_PORT", "50100")),
            storage_validation=e.get("STORAGE_VALIDATION", "").lower() in ("1", "true", "yes"),
            run_dir=e.get("RUN_DIR", ""),
            rng_seed=int(e.get("RNG_SEED", "42")),
        )

    def plateau_workers_csv(self) -> str:
        return ",".join(str(v) for v in self.plateau_workers_per_agent)


class Approach(ABC):
    """Abstract base class for isolation approaches."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier, e.g. 'vm-qemu', 'container-docker'."""
        ...

    @property
    def suite(self) -> str:
        """Suite this approach belongs to: 'synthetic' or 'ironclaw'."""
        return "synthetic"

    @abstractmethod
    def setup(self, config: BenchmarkConfig) -> None:
        """One-time setup: build images, VM disks, etc."""
        ...

    @abstractmethod
    def start_agents(self, n: int, config: BenchmarkConfig) -> List[str]:
        """
        Start N agents. Returns a list of agent IDs.
        Agents should begin their spawn loops immediately.
        """
        ...

    @abstractmethod
    def get_agent_pids(self) -> Dict[str, int]:
        """
        Return a mapping of agent_id -> host PID for memory measurement.
        For VMs: the QEMU process PID.
        For containers: the container init PID on the host.
        """
        ...

    def get_daemon_pids(self) -> Dict[str, int]:
        """
        Return PIDs for container runtime daemons (dockerd, containerd, etc.)
        for memory measurement. Default returns empty dict.

        Override in approaches where daemons are on the host
        (e.g., container-docker). For VM approaches, daemons run inside
        the guest and are not directly measurable from the host.
        """
        return {}

    @abstractmethod
    def count_active_workers(self) -> int:
        """Count the total number of active worker containers across all agents."""
        ...

    def count_active_workers_per_agent(self) -> Dict[str, int]:
        """Count active worker containers per agent when the approach can expose it."""
        return {}

    def get_agent_gateways(self) -> Dict[str, int]:
        """Return agent_id -> host gateway port for host-side control."""
        return {}

    def trigger_worker_spawn(
        self,
        agent_id: str,
        command: str | None = None,
        dispatch_mode: str = "shell",
    ) -> bool:
        """Trigger one benchmark job for a specific agent."""
        from approaches._ironclaw_helpers import trigger_worker_spawn

        port = self.get_agent_gateways().get(agent_id)
        if port is None:
            raise RuntimeError(
                f"{self.name} does not expose a control route for {agent_id}"
            )
        return trigger_worker_spawn(
            port,
            command=command,
            dispatch_mode=dispatch_mode,
        )

    def get_agent_roots(self) -> Dict[str, Path]:
        """Return agent_id -> host-visible benchmark root for direct verification."""
        return {}

    def translate_agent_path(self, agent_id: str, path: str | Path | None) -> Path | None:
        """Map an agent-reported path into a host-visible path when needed."""
        if path is None:
            return None
        return Path(path)

    def verify_worker_absent(self, agent_id: str, job_id: str) -> bool | None:
        """Return whether a worker container is absent after cleanup, if supported."""
        return None

    def verify_agent_absent(self, agent_id: str) -> bool | None:
        """Return whether an outer agent container is absent after cleanup, if supported."""
        return None

    def collect_agent_logs(self, agent_ids: List[str], output_dir) -> None:
        """
        Collect agent stdout logs (JSONL events) after benchmark completes.
        Override per approach: docker logs for containers, console logs for VMs.
        Default is a no-op.
        """
        pass

    def start_benchmark(self) -> None:
        """Optional synchronization hook before the timed benchmark window begins."""
        pass

    @abstractmethod
    def stop_agents(self) -> None:
        """Stop all agents and clean up their workers."""
        ...

    def cleanup(self) -> None:
        """Optional: remove build artifacts (images, VM disks, etc.)."""
        pass


def discover_approaches(suite: Optional[str] = None) -> Dict[str, "Approach"]:
    """Auto-discover all approach modules in approaches/.

    Args:
        suite: If given, only return approaches matching this suite
               ("synthetic" or "ironclaw"). None returns all.

    Returns:
        Dict mapping approach name to instance.
    """
    approaches_dir = Path(__file__).resolve().parent
    approaches = {}
    for py_file in approaches_dir.glob("*.py"):
        if py_file.name.startswith("_") or py_file.name == "base.py":
            continue
        module_name = f"approaches.{py_file.stem}"
        try:
            mod = importlib.import_module(module_name)
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, Approach)
                    and attr is not Approach
                    and not attr_name.startswith("_")
                ):
                    instance = attr()
                    if suite is None or instance.suite == suite:
                        approaches[instance.name] = instance
        except Exception as e:
            print(f"Warning: could not load {py_file.name}: {e}")
    return approaches
