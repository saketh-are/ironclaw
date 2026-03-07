#!/usr/bin/env python3
"""
Benchmark runner for real IronClaw approaches.

Unlike the synthetic suite, this runner controls load from the host while using
real IronClaw agents and real sandbox worker containers. It supports:
  - idle: agents only, no sandbox jobs
  - loaded: stochastic per-agent spawning with max concurrency
  - plateau: host-driven step function of workers per agent

The mock LLM is still used, but each trigger can carry an arbitrary shell
command so benchmark jobs are not limited to a single fixed worker payload.
"""

import argparse
import json
import os
import random
import sys
import textwrap
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, List

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR))

from approaches.base import BenchmarkConfig, discover_approaches
from approaches._ironclaw_helpers import trigger_worker_spawn
from runner.collect import Collector, HOST_CPU_FIELDS
from runner.orchestrate import build_plateau_summary, compute_drift_slope, compute_percentiles


LAUNCH_VISIBILITY_TIMEOUT_S = 30.0
BASELINE_DURATION_S = 10.0
IDLE_WARMUP_S = 30.0
LOADED_WARMUP_S = 60.0


def parse_int_list(raw: str) -> List[int]:
    raw = (raw or "").strip()
    if not raw:
        return []
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


def per_agent_rng(base_seed: int, agent_id: str) -> random.Random:
    seed = hash((base_seed, agent_id)) & 0xFFFFFFFF
    return random.Random(seed)


def exponential_delay_s(rng: random.Random, mean_s: float) -> float:
    if mean_s <= 0:
        return 0.0
    return rng.expovariate(1.0 / mean_s)


def ramp_delay_s(agent_id: str, batch_size: int, interval_s: float) -> float:
    if batch_size <= 0 or interval_s <= 0:
        return 0.0
    try:
        ordinal = int(agent_id.rsplit("-", 1)[-1])
    except (IndexError, ValueError):
        ordinal = 0
    return (ordinal // batch_size) * interval_s


def sampled_duration_s(
    rng: random.Random,
    duration_min_s: int,
    duration_max_s: int,
    lifetime_mode: str,
    benchmark_duration_s: float,
) -> int:
    if lifetime_mode == "hold":
        floor = max(duration_max_s, int(benchmark_duration_s) + 300)
        return max(duration_min_s, floor)
    if duration_max_s <= duration_min_s:
        return max(0, duration_min_s)
    return rng.randint(duration_min_s, duration_max_s)


def render_job_command(
    profile: str,
    custom_command: str,
    agent_id: str,
    trigger_index: int,
    duration_s: int,
    memory_mb: int,
) -> str:
    proof_dir = "/workspace/bench-test"
    proof_file = f"{proof_dir}/output-{agent_id}-{trigger_index}.txt"
    alloc_file = f"/tmp/bench-alloc-{agent_id}-{trigger_index}.bin"
    context = {
        "agent_id": agent_id,
        "trigger_index": trigger_index,
        "duration_s": duration_s,
        "memory_mb": memory_mb,
        "alloc_bytes": memory_mb * 1024 * 1024,
        "proof_dir": proof_dir,
        "proof_file": proof_file,
        "alloc_file": alloc_file,
    }

    if custom_command:
        return custom_command.format(**context)

    parts = [
        "set -eu",
        f"mkdir -p {proof_dir}",
        f"echo proof-{agent_id}-{trigger_index} > {proof_file}",
    ]

    if profile == "custom":
        raise ValueError("custom job profile requires --job-command")
    if profile == "memory-touch":
        if memory_mb <= 0:
            raise ValueError("memory-touch profile requires --job-memory-mb > 0")
        parts.append(textwrap.dedent(f"""\
            python3 - <<'PY'
            import random
            import time

            size = {memory_mb} * 1024 * 1024
            step = 4096
            buf = bytearray(size)
            rnd = random.Random({trigger_index})
            for offset in range(0, size, step):
                buf[offset] = rnd.randrange(256)
            time.sleep({duration_s})
            PY"""))

    if duration_s > 0 and profile != "memory-touch":
        parts.append(f"sleep {duration_s}")

    return " && ".join(parts)


def fetch_active_counts(
    approach,
    agent_ids: List[str],
    previous_counts: Dict[str, int],
) -> (Dict[str, int], Dict[str, str]):
    try:
        counts = approach.count_active_workers_per_agent()
    except Exception as exc:  # pragma: no cover - defensive
        return dict(previous_counts), {agent_id: str(exc) for agent_id in agent_ids}

    if not counts and agent_ids:
        return dict(previous_counts), {
            agent_id: "per-agent worker counts unavailable"
            for agent_id in agent_ids
        }

    merged = {}
    for agent_id in agent_ids:
        merged[agent_id] = counts.get(agent_id, previous_counts.get(agent_id, 0))
    return merged, {}


def summarize_timeseries(run_dir: Path, params: dict, num_agents: int) -> dict:
    samples = []
    with open(run_dir / "timeseries.jsonl") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            samples.append(json.loads(line))

    if not samples:
        return {"error": "No samples collected"}

    baseline_samples = [s for s in samples if s.get("phase") == "baseline"]
    running_samples = [s for s in samples if s.get("phase") == "running"]
    baseline_kb = (
        sum(s["host_consumed_kb"] for s in baseline_samples) / len(baseline_samples)
        if baseline_samples
        else 0.0
    )

    if not running_samples:
        return {"error": "No running samples collected"}

    mode = params["mode"]
    config = params["config"]
    plateau = None

    if mode == "plateau":
        plateau_offset = params.get("plateau_start_offset_s", 0.0)
        shifted_samples = [
            {**s, "timestamp_s": max(0.0, s["timestamp_s"] - plateau_offset)}
            for s in running_samples
            if s["timestamp_s"] >= plateau_offset
        ]
        plateau = build_plateau_summary(shifted_samples, baseline_kb, params, num_agents)
        steady_samples = []
        hold_s = config.get("plateau_hold_s", 0)
        settle_s = config.get("plateau_settle_s", 0)
        for index, _target in enumerate(config.get("plateau_workers_per_agent") or []):
            stage_start = plateau_offset + index * hold_s
            stage_end = stage_start + hold_s
            steady_start = stage_start + min(settle_s, hold_s)
            steady_samples.extend(
                s for s in running_samples
                if steady_start <= s["timestamp_s"] < stage_end
            )
        if not steady_samples:
            steady_samples = running_samples
    elif mode == "loaded":
        warmup = params.get("pre_trigger_settle_s", 0.0) + min(
            LOADED_WARMUP_S,
            max(config.get("benchmark_duration_s", 0) / 3.0, 0.0),
        )
        steady_samples = [s for s in running_samples if s["timestamp_s"] >= warmup]
        if not steady_samples:
            steady_samples = running_samples
    else:
        warmup = min(IDLE_WARMUP_S, max(config.get("benchmark_duration_s", 0) / 3.0, 0.0))
        steady_samples = [s for s in running_samples if s["timestamp_s"] >= warmup]
        if not steady_samples:
            steady_samples = running_samples

    consumed_values = [s["host_consumed_kb"] for s in steady_samples]
    worker_values = [s.get("active_workers", 0) for s in steady_samples]
    mean_consumed_kb = sum(consumed_values) / len(consumed_values)
    peak_consumed_kb = max(consumed_values)
    net_values = [v - baseline_kb for v in consumed_values]
    pcts = compute_percentiles(net_values, [50, 95, 99])
    timestamps = [s["timestamp_s"] for s in steady_samples]
    drift = compute_drift_slope(timestamps, consumed_values)

    daemon_baseline = {}
    daemon_running = {}
    for sample, sink in ((baseline_samples, daemon_baseline), (steady_samples, daemon_running)):
        for s in sample:
            for daemon_name, daemon_data in s.get("daemons", {}).items():
                entry = sink.setdefault(daemon_name, {"rss_sum": 0, "pss_sum": 0, "count": 0})
                entry["rss_sum"] += daemon_data.get("rss_kb", 0)
                entry["pss_sum"] += daemon_data.get("pss_kb", 0)
                entry["count"] += 1

    daemon_summary = {}
    for daemon_name, data in daemon_running.items():
        if data["count"] <= 0:
            continue
        mean_rss_kb = data["rss_sum"] / data["count"]
        mean_pss_kb = data["pss_sum"] / data["count"]
        entry = {
            "mean_rss_mib": mean_rss_kb / 1024,
            "mean_pss_mib": mean_pss_kb / 1024,
        }
        baseline = daemon_baseline.get(daemon_name)
        if baseline and baseline["count"] > 0:
            baseline_rss_kb = baseline["rss_sum"] / baseline["count"]
            baseline_pss_kb = baseline["pss_sum"] / baseline["count"]
            entry.update({
                "baseline_rss_mib": baseline_rss_kb / 1024,
                "baseline_pss_mib": baseline_pss_kb / 1024,
                "delta_rss_mib": (mean_rss_kb - baseline_rss_kb) / 1024,
                "delta_pss_mib": (mean_pss_kb - baseline_pss_kb) / 1024,
            })
        daemon_summary[daemon_name] = entry

    host_cpu_pct = None
    cpu_samples = [s for s in steady_samples if s.get("host_cpu")]
    if len(cpu_samples) >= 2:
        first_cpu = cpu_samples[0]["host_cpu"]
        last_cpu = cpu_samples[-1]["host_cpu"]
        busy_fields = [f for f in HOST_CPU_FIELDS if f not in ("idle", "iowait")]
        delta_busy = sum(last_cpu.get(f, 0) - first_cpu.get(f, 0) for f in busy_fields)
        delta_total = sum(last_cpu.get(f, 0) - first_cpu.get(f, 0) for f in HOST_CPU_FIELDS)
        if delta_total > 0:
            host_cpu_pct = round(100.0 * delta_busy / delta_total, 1)

    clk_tck = os.sysconf("SC_CLK_TCK")
    agent_cpu_seconds = []
    entity_cpu_samples = [s for s in steady_samples if s.get("entity_cpu")]
    if entity_cpu_samples:
        all_agents = set()
        for s in entity_cpu_samples:
            all_agents.update(s["entity_cpu"].keys())
        for agent_id in sorted(all_agents):
            first = None
            last = None
            for s in entity_cpu_samples:
                if agent_id in s["entity_cpu"]:
                    if first is None:
                        first = s["entity_cpu"][agent_id]
                    last = s["entity_cpu"][agent_id]
            if first is not None and last is not None:
                agent_cpu_seconds.append(
                    (last["utime"] - first["utime"] + last["stime"] - first["stime"]) / clk_tck
                )

    summary = {
        "baseline_mib": baseline_kb / 1024,
        "steady_state_mean_mib": (mean_consumed_kb - baseline_kb) / 1024,
        "peak_mib": (peak_consumed_kb - baseline_kb) / 1024,
        "p50_mib": pcts.get("p50", 0) / 1024 if pcts else 0.0,
        "p95_mib": pcts.get("p95", 0) / 1024 if pcts else 0.0,
        "p99_mib": pcts.get("p99", 0) / 1024 if pcts else 0.0,
        "per_agent_mean_mib": ((mean_consumed_kb - baseline_kb) / 1024) / max(num_agents, 1),
        "avg_workers": sum(worker_values) / len(worker_values) if worker_values else 0.0,
        "drift_kb_per_s": drift,
        "daemon_overhead": daemon_summary,
        "host_cpu_pct": host_cpu_pct,
    }
    if agent_cpu_seconds:
        summary["per_agent_cpu_s"] = round(sum(agent_cpu_seconds) / len(agent_cpu_seconds), 1)
    if plateau:
        summary["plateau"] = plateau
    return summary


class AgentState:
    def __init__(self, rng: random.Random, release_at: float, next_spawn_at: float):
        self.rng = rng
        self.release_at = release_at
        self.next_spawn_at = next_spawn_at
        self.pending_launches: Deque[float] = deque()
        self.prev_observed_active = 0
        self.trigger_index = 0
        self.trigger_attempts = 0
        self.trigger_ok = 0
        self.trigger_failed = 0
        self.skipped_due_to_limit = 0
        self.pending_timeouts = 0

    def effective_active(self, observed_active: int) -> int:
        return observed_active + len(self.pending_launches)

    def reconcile(self, observed_active: int, now: float) -> None:
        increased = max(0, observed_active - self.prev_observed_active)
        while increased > 0 and self.pending_launches:
            self.pending_launches.popleft()
            increased -= 1
        while self.pending_launches and (now - self.pending_launches[0]) > LAUNCH_VISIBILITY_TIMEOUT_S:
            self.pending_launches.popleft()
            self.pending_timeouts += 1
        self.prev_observed_active = observed_active


def build_params(args, config, run_label: str, run_dir: Path) -> dict:
    total_duration = config.benchmark_duration_s
    return {
        "run_label": run_label,
        "run_dir": str(run_dir),
        "approach": args.approach,
        "mode": args.mode,
        "num_agents": args.agents,
        "run_id": config.run_id,
        "rng_seed": args.rng_seed,
        "sample_interval_ms": args.sample_interval_ms,
        "control_interval_s": args.control_interval_s,
        "pre_trigger_settle_s": args.pre_trigger_settle_s,
        "batch_size": args.batch_size,
        "batch_interval_s": args.batch_interval_s,
        "job_profile": args.job_profile,
        "job_command": args.job_command,
        "timestamp": datetime.now().strftime("%Y%m%dT%H%M%S"),
        "config": {
            "benchmark_mode": args.mode,
            "agent_memory_mb": config.agent_memory_mb,
            "agent_baseline_mb": config.agent_baseline_mb,
            "max_concurrent_workers": config.max_concurrent_workers,
            "spawn_interval_mean_s": config.spawn_interval_mean_s,
            "benchmark_duration_s": total_duration,
            "worker_image": config.worker_image,
            "worker_memory_limit_mb": config.worker_memory_limit_mb,
            "worker_memory_mb": config.worker_memory_mb,
            "worker_duration_min_s": config.worker_duration_min_s,
            "worker_duration_max_s": config.worker_duration_max_s,
            "worker_lifetime_mode": config.worker_lifetime_mode,
            "plateau_workers_per_agent": config.plateau_workers_per_agent,
            "plateau_hold_s": config.plateau_hold_s,
            "plateau_settle_s": config.plateau_settle_s,
            "job_profile": args.job_profile,
            "job_command": args.job_command,
        },
    }


def control_loaded(
    args,
    approach,
    agent_ids: List[str],
    agent_ports: Dict[str, int],
    states: Dict[str, AgentState],
    benchmark_duration_s: float,
) -> dict:
    start_time = time.monotonic()
    end_time = time.monotonic() + benchmark_duration_s
    control_samples = []
    last_counts = {agent_id: 0 for agent_id in agent_ids}
    last_errors = {}
    while time.monotonic() < end_time:
        now = time.monotonic()
        counts, errors = fetch_active_counts(approach, agent_ids, last_counts)
        for agent_id in agent_ids:
            states[agent_id].reconcile(counts[agent_id], now)
        last_counts = counts
        last_errors = errors

        total_active = sum(counts.values())
        control_samples.append({
            "t_s": round(now - start_time, 3),
            "active_workers": total_active,
        })

        for agent_id in agent_ids:
            state = states[agent_id]
            if now < state.release_at or now < state.next_spawn_at:
                continue
            observed = counts[agent_id]
            if state.effective_active(observed) < args.max_concurrent_workers:
                duration_s = sampled_duration_s(
                    state.rng,
                    args.job_duration_min_s,
                    args.job_duration_max_s,
                    args.job_lifetime_mode,
                    benchmark_duration_s,
                )
                command = render_job_command(
                    args.job_profile,
                    args.job_command,
                    agent_id,
                    state.trigger_index,
                    duration_s,
                    args.job_memory_mb,
                )
                state.trigger_attempts += 1
                ok = trigger_worker_spawn(agent_ports[agent_id], command=command)
                if ok:
                    state.trigger_ok += 1
                    state.pending_launches.append(now)
                    state.trigger_index += 1
                else:
                    state.trigger_failed += 1
            else:
                state.skipped_due_to_limit += 1
            state.next_spawn_at = now + exponential_delay_s(state.rng, args.spawn_interval_mean_s)

        time.sleep(args.control_interval_s)

    final_counts, final_errors = fetch_active_counts(approach, agent_ids, last_counts)
    return {
        "control_samples": control_samples,
        "last_errors": last_errors,
        "final_errors": final_errors,
        "final_counts": final_counts,
    }


def control_plateau(
    args,
    approach,
    agent_ids: List[str],
    agent_ports: Dict[str, int],
    states: Dict[str, AgentState],
) -> dict:
    targets = args.plateau_workers_per_agent
    hold_s = args.plateau_hold_s
    total_duration_s = hold_s * len(targets)
    end_time = time.monotonic() + total_duration_s
    plateau_start = time.monotonic()
    control_samples = []
    last_counts = {agent_id: 0 for agent_id in agent_ids}
    last_errors = {}

    while time.monotonic() < end_time:
        now = time.monotonic()
        elapsed = now - plateau_start
        stage_index = min(int(elapsed // hold_s), max(len(targets) - 1, 0))
        target = targets[stage_index]

        counts, errors = fetch_active_counts(approach, agent_ids, last_counts)
        for agent_id in agent_ids:
            states[agent_id].reconcile(counts[agent_id], now)
        last_counts = counts
        last_errors = errors

        total_active = sum(counts.values())
        control_samples.append({
            "t_s": round(elapsed, 3),
            "active_workers": total_active,
            "stage_index": stage_index,
            "target_workers_per_agent": target,
        })

        for agent_id in agent_ids:
            state = states[agent_id]
            if now < state.release_at:
                continue
            observed = counts[agent_id]
            if state.effective_active(observed) < target:
                duration_s = sampled_duration_s(
                    state.rng,
                    args.job_duration_min_s,
                    args.job_duration_max_s,
                    args.job_lifetime_mode,
                    total_duration_s,
                )
                command = render_job_command(
                    args.job_profile,
                    args.job_command,
                    agent_id,
                    state.trigger_index,
                    duration_s,
                    args.job_memory_mb,
                )
                state.trigger_attempts += 1
                ok = trigger_worker_spawn(agent_ports[agent_id], command=command)
                if ok:
                    state.trigger_ok += 1
                    state.pending_launches.append(now)
                    state.trigger_index += 1
                else:
                    state.trigger_failed += 1
            elif state.effective_active(observed) > target:
                state.skipped_due_to_limit += 1

        time.sleep(args.control_interval_s)

    final_counts, final_errors = fetch_active_counts(approach, agent_ids, last_counts)
    return {
        "control_samples": control_samples,
        "last_errors": last_errors,
        "final_errors": final_errors,
        "final_counts": final_counts,
        "plateau_start_offset_s": args.pre_trigger_settle_s,
    }


def control_idle(args, approach, agent_ids: List[str], agent_ports: Dict[str, int]) -> dict:
    start_time = time.monotonic()
    end_time = time.monotonic() + args.benchmark_duration_s
    control_samples = []
    last_counts = {agent_id: 0 for agent_id in agent_ids}
    last_errors = {}
    while time.monotonic() < end_time:
        counts, errors = fetch_active_counts(approach, agent_ids, last_counts)
        last_counts = counts
        last_errors = errors
        control_samples.append({
            "t_s": round(time.monotonic() - start_time, 3),
            "active_workers": sum(counts.values()),
        })
        time.sleep(args.control_interval_s)
    final_counts, final_errors = fetch_active_counts(approach, agent_ids, last_counts)
    return {
        "control_samples": control_samples,
        "last_errors": last_errors,
        "final_errors": final_errors,
        "final_counts": final_counts,
    }


def main():
    parser = argparse.ArgumentParser(description="Run a real IronClaw benchmark.")
    parser.add_argument("--approach", required=True, help="IronClaw approach name")
    parser.add_argument("--agents", type=int, required=True, help="Number of agents")
    parser.add_argument("--mode", choices=["idle", "loaded", "plateau"], default="loaded")
    parser.add_argument("--benchmark-duration-s", type=float, default=180.0,
                        help="Loaded/idle benchmark duration in seconds")
    parser.add_argument("--spawn-interval-mean-s", type=float, default=5.0,
                        help="Mean inter-arrival time per agent for loaded mode")
    parser.add_argument("--max-concurrent-workers", type=int, default=5,
                        help="Max concurrent sandbox jobs per agent in loaded mode")
    parser.add_argument("--plateau-workers-per-agent", default="0,1,2,3,4,5",
                        help="Comma-separated plateau worker targets per agent")
    parser.add_argument("--plateau-hold-s", type=float, default=60.0,
                        help="Duration of each plateau stage")
    parser.add_argument("--plateau-settle-s", type=float, default=20.0,
                        help="Ignore this much of each plateau stage before sampling")
    parser.add_argument("--batch-size", type=int, default=0,
                        help="Release agents in batches of this size (0 = no ramp)")
    parser.add_argument("--batch-interval-s", type=float, default=0.0,
                        help="Delay between agent release batches")
    parser.add_argument("--pre-trigger-settle-s", type=float, default=10.0,
                        help="Collect zero-worker samples after agents start before control begins")
    parser.add_argument("--control-interval-s", type=float, default=1.0,
                        help="Host control-loop polling interval")
    parser.add_argument("--sample-interval-ms", type=int, default=1000,
                        help="Collector sampling interval")
    parser.add_argument("--agent-memory-mb", type=int, default=2048,
                        help="Outer agent memory limit in MB")
    parser.add_argument("--orchestrator-base-port", type=int, default=56000,
                        help="Base host port for agent gateways")
    parser.add_argument("--rng-seed", type=int, default=42,
                        help="Base RNG seed for job scheduling")
    parser.add_argument("--job-profile", choices=["sleep", "memory-touch", "custom"], default="sleep",
                        help="Built-in benchmark job profile")
    parser.add_argument("--job-command", default="",
                        help="Custom shell command template for each trigger; "
                             "supports {agent_id}, {trigger_index}, {duration_s}, "
                             "{memory_mb}, {proof_dir}, {proof_file}")
    parser.add_argument("--job-duration-min-s", type=int, default=30,
                        help="Minimum worker lifetime")
    parser.add_argument("--job-duration-max-s", type=int, default=30,
                        help="Maximum worker lifetime")
    parser.add_argument("--job-lifetime-mode", choices=["timed", "hold"], default="timed",
                        help="Hold jobs until teardown or use sampled durations")
    parser.add_argument("--job-memory-mb", type=int, default=0,
                        help="Memory payload for the built-in memory-touch job profile")
    parser.add_argument("--output-dir", default="",
                        help="Optional explicit results directory")
    args = parser.parse_args()

    if args.job_profile == "custom" and not args.job_command:
        parser.error("--job-profile custom requires --job-command")
    if args.mode == "plateau":
        args.plateau_workers_per_agent = parse_int_list(args.plateau_workers_per_agent)
        if not args.plateau_workers_per_agent:
            parser.error("--plateau-workers-per-agent must not be empty in plateau mode")
        args.benchmark_duration_s = args.plateau_hold_s * len(args.plateau_workers_per_agent)
    else:
        args.plateau_workers_per_agent = parse_int_list(args.plateau_workers_per_agent)

    approaches = discover_approaches(suite="ironclaw")
    if args.approach not in approaches:
        print(f"Unknown approach '{args.approach}'. Available:")
        for name in sorted(approaches):
            print(f"  {name}")
        sys.exit(1)

    approach = approaches[args.approach]
    timestamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    run_label = (
        Path(args.output_dir).name if args.output_dir else
        f"{args.approach}-{args.mode}-n{args.agents}-{timestamp}"
    )
    run_dir = Path(args.output_dir) if args.output_dir else (BENCH_DIR / "results" / run_label)
    run_dir.mkdir(parents=True, exist_ok=True)

    config = BenchmarkConfig(
        benchmark_mode=args.mode,
        agent_memory_mb=args.agent_memory_mb,
        orchestrator_base_port=args.orchestrator_base_port,
        max_concurrent_workers=args.max_concurrent_workers,
        spawn_interval_mean_s=args.spawn_interval_mean_s,
        benchmark_duration_s=int(args.benchmark_duration_s),
        worker_memory_mb=args.job_memory_mb,
        worker_duration_min_s=args.job_duration_min_s,
        worker_duration_max_s=args.job_duration_max_s,
        worker_lifetime_mode=args.job_lifetime_mode,
        plateau_workers_per_agent=args.plateau_workers_per_agent,
        plateau_hold_s=int(args.plateau_hold_s),
        plateau_settle_s=int(args.plateau_settle_s),
        rng_seed=args.rng_seed,
    )

    params = build_params(args, config, run_label, run_dir)
    with open(run_dir / "params.json", "w") as f:
        json.dump(params, f, indent=2)

    start_ts = time.time()
    baseline_collector = None
    baseline_thread = None
    running_collector = None
    running_thread = None
    ts_file = None
    agent_ids = []
    summary = dict(params)
    run_error = None
    cleanup_error = None

    try:
        print(f"[ironclaw-benchmark] setup {args.approach}", flush=True)
        approach.setup(config)

        ts_file = open(run_dir / "timeseries.jsonl", "w")
        baseline_collector = Collector(interval_ms=args.sample_interval_ms, phase="baseline")
        baseline_thread = baseline_collector.run_in_thread(
            output=ts_file,
            get_agent_pids=lambda: {},
            get_daemon_pids=approach.get_daemon_pids,
            count_workers=lambda: 0,
        )
        print(f"[ironclaw-benchmark] collecting no-agent baseline for {BASELINE_DURATION_S:.0f}s", flush=True)
        time.sleep(BASELINE_DURATION_S)
        baseline_collector.stop()
        baseline_thread.join(timeout=10)

        print(f"[ironclaw-benchmark] starting {args.agents} agents", flush=True)
        agent_ids = approach.start_agents(args.agents, config)
        summary["agent_ids"] = agent_ids
        summary["agent_start_elapsed_s"] = round(time.time() - start_ts, 1)

        agent_ports = approach.get_agent_gateways()
        if set(agent_ports) != set(agent_ids):
            raise RuntimeError("Approach did not expose gateway ports for all agents")

        states = {}
        control_start_ref = time.monotonic() + args.pre_trigger_settle_s
        for agent_id in agent_ids:
            rng = per_agent_rng(args.rng_seed, agent_id)
            release_at = control_start_ref + ramp_delay_s(agent_id, args.batch_size, args.batch_interval_s)
            next_spawn_at = release_at + exponential_delay_s(rng, args.spawn_interval_mean_s)
            states[agent_id] = AgentState(rng, release_at, next_spawn_at)

        running_collector = Collector(interval_ms=args.sample_interval_ms, phase="running")
        running_thread = running_collector.run_in_thread(
            output=ts_file,
            get_agent_pids=approach.get_agent_pids,
            get_daemon_pids=approach.get_daemon_pids,
            count_workers=approach.count_active_workers,
        )

        if args.pre_trigger_settle_s > 0:
            print(
                f"[ironclaw-benchmark] collecting zero-worker settle window for {args.pre_trigger_settle_s:.1f}s",
                flush=True,
            )
            time.sleep(args.pre_trigger_settle_s)

        if args.mode != "idle":
            probe_counts = approach.count_active_workers_per_agent()
            if not probe_counts:
                raise RuntimeError(
                    f"{args.approach} does not expose per-agent worker counts for real loaded/plateau control"
                )

        if args.mode == "idle":
            control_result = control_idle(args, approach, agent_ids, agent_ports)
        elif args.mode == "loaded":
            control_result = control_loaded(
                args, approach, agent_ids, agent_ports, states, args.benchmark_duration_s
            )
        else:
            control_result = control_plateau(args, approach, agent_ids, agent_ports, states)
            params["plateau_start_offset_s"] = control_result.get("plateau_start_offset_s", 0.0)
            summary["plateau_start_offset_s"] = params["plateau_start_offset_s"]
            with open(run_dir / "params.json", "w") as f:
                json.dump(params, f, indent=2)

        running_collector.stop()
        running_thread.join(timeout=30)
        ts_file.close()
        ts_file = None

        summary["control_samples"] = control_result["control_samples"]
        summary["final_active_workers"] = sum(control_result["final_counts"].values())
        summary["final_per_agent_active_workers"] = control_result["final_counts"]
        summary["control_errors"] = {
            "during_run": control_result.get("last_errors", {}),
            "final": control_result.get("final_errors", {}),
        }
        summary["workers_spawned"] = sum(state.trigger_ok for state in states.values())
        summary["trigger_attempts"] = sum(state.trigger_attempts for state in states.values())
        summary["trigger_failed"] = sum(state.trigger_failed for state in states.values())
        summary["per_agent_triggers_ok"] = {agent_id: state.trigger_ok for agent_id, state in states.items()}
        summary["per_agent_trigger_attempts"] = {agent_id: state.trigger_attempts for agent_id, state in states.items()}
        summary["per_agent_pending_timeouts"] = {agent_id: state.pending_timeouts for agent_id, state in states.items()}
        summary["elapsed_s"] = round(time.time() - start_ts, 1)

    except Exception as exc:
        summary["error"] = str(exc)
        run_error = exc
    finally:
        if baseline_collector is not None:
            baseline_collector.stop()
        if baseline_thread is not None and baseline_thread.is_alive():
            baseline_thread.join(timeout=5)
        if running_collector is not None:
            running_collector.stop()
        if running_thread is not None and running_thread.is_alive():
            running_thread.join(timeout=10)
        if ts_file is not None:
            try:
                ts_file.close()
            except Exception:
                pass

    if (run_dir / "timeseries.jsonl").exists():
        summary.update(summarize_timeseries(run_dir, params, args.agents))

    with open(run_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps({
        "run_dir": str(run_dir),
        "approach": args.approach,
        "mode": args.mode,
        "workers_spawned": summary.get("workers_spawned", 0),
        "avg_workers": round(summary.get("avg_workers", 0.0), 3),
        "per_agent_mean_mib": round(summary.get("per_agent_mean_mib", 0.0), 3),
        "final_active_workers": summary.get("final_active_workers", 0),
        "peak_mib": round(summary.get("peak_mib", 0.0), 3),
        "p95_mib": round(summary.get("p95_mib", 0.0), 3),
    }, indent=2), flush=True)

    print("[ironclaw-benchmark] cleaning up...", flush=True)
    try:
        approach.cleanup()
    except Exception as exc:
        cleanup_error = exc
        print(f"[ironclaw-benchmark] cleanup error: {exc}", flush=True)

    if cleanup_error is not None:
        raise cleanup_error
    if run_error is not None:
        raise run_error


if __name__ == "__main__":
    main()
