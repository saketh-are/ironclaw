#!/usr/bin/env python3
"""
Benchmark orchestrator.

Runs a single approach with a single agent count. The user runs this
separately for each approach and agent count, then compares results
offline with analysis/compare.py.

Usage:
    python3 -m runner.orchestrate --approach container-docker --agents 5
    python3 -m runner.orchestrate --approach vm-qemu --agents 10
    python3 -m runner.orchestrate --approach container-docker --agents 100 --mode idle

Modes:
    loaded  (default) — stochastic workload with workers
    idle    — no workers spawned, measures pure isolation overhead per agent

Results are saved to results/<approach>-<mode>-<agents>-<timestamp>/.
"""

import argparse
import importlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Add parent directory to path so we can import approaches/runner modules
BENCH_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCH_DIR))

from approaches.base import Approach, BenchmarkConfig
from runner.collect import Collector, read_meminfo, read_vmstat_swap, HOST_CPU_FIELDS

# Registry of available approaches
APPROACHES = {}


def register_approach(cls: type) -> None:
    """Register an approach class."""
    instance = cls()
    APPROACHES[instance.name] = instance


def discover_approaches() -> None:
    """Auto-discover approach modules in approaches/."""
    approaches_dir = BENCH_DIR / "approaches"
    for py_file in approaches_dir.glob("*.py"):
        if py_file.name.startswith("_") or py_file.name == "base.py":
            continue
        module_name = f"approaches.{py_file.stem}"
        try:
            mod = importlib.import_module(module_name)
            # Find Approach subclasses in the module
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, Approach)
                    and attr is not Approach
                ):
                    register_approach(attr)
        except Exception as e:
            print(f"Warning: could not load approach from {py_file.name}: {e}")


def load_config_env() -> dict:
    """Load config.env file as environment variable defaults."""
    config_path = BENCH_DIR / "config.env"
    env = {}
    if config_path.exists():
        with open(config_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    if "=" in line:
                        key, value = line.split("=", 1)
                        env[key.strip()] = value.strip()
    return env


def check_swap_status() -> dict:
    """Check if swap is enabled and return status info."""
    meminfo = read_meminfo()
    swap_total = meminfo.get("SwapTotal", 0)
    swap_free = meminfo.get("SwapFree", 0)
    vmstat = read_vmstat_swap()

    status = {
        "swap_enabled": swap_total > 0,
        "swap_total_kb": swap_total,
        "swap_used_kb": swap_total - swap_free,
        "pswpin_start": vmstat.get("pswpin", 0),
        "pswpout_start": vmstat.get("pswpout", 0),
    }

    if swap_total > 0:
        print(f"WARNING: Swap is enabled ({swap_total // 1024} MiB total, "
              f"{(swap_total - swap_free) // 1024} MiB used).")
        print("  For accurate density measurements, disable swap: sudo swapoff -a")
        print("  Continuing anyway, but results may be distorted by swap activity.\n")

    return status


def check_swap_activity(start_counters: dict) -> dict:
    """Check if swap was used during the benchmark."""
    vmstat = read_vmstat_swap()
    pswpin_delta = vmstat.get("pswpin", 0) - start_counters.get("pswpin_start", 0)
    pswpout_delta = vmstat.get("pswpout", 0) - start_counters.get("pswpout_start", 0)

    result = {
        "pswpin_delta": pswpin_delta,
        "pswpout_delta": pswpout_delta,
        "swap_occurred": (pswpin_delta > 0 or pswpout_delta > 0),
    }

    if result["swap_occurred"]:
        print(f"\nWARNING: Swap activity detected during benchmark!")
        print(f"  Pages swapped in:  {pswpin_delta}")
        print(f"  Pages swapped out: {pswpout_delta}")
        print(f"  Results may not reflect true physical memory usage.")

    return result


def parse_agent_logs(run_dir: Path) -> dict:
    """Parse agent JSONL logs for worker event statistics."""
    stats = {
        "total_workers_spawned": 0,
        "max_concurrent_workers": 0,
    }

    log_files = sorted(run_dir.glob("agent-*.jsonl"))
    if not log_files:
        # VM approach uses .log files with embedded JSONL
        log_files = sorted(run_dir.glob("agent-*.log"))

    for log_file in log_files:
        active = 0
        try:
            with open(log_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("event") == "worker_start":
                        stats["total_workers_spawned"] += 1
                        if "active_workers" in event:
                            active = event["active_workers"]
                            stats["max_concurrent_workers"] = max(
                                stats["max_concurrent_workers"], active
                            )
                    elif event.get("event") == "worker_end":
                        if "active_workers" in event:
                            active = event["active_workers"]
        except Exception:
            pass

    return stats


def validate_checkins(run_dir: Path) -> dict:
    """
    Validate worker checkins from agent logs.

    Prefer raw worker_start/checkin events so the result reflects the actual
    workers observed in the log, not the last periodic checkin_summary
    snapshot. Fall back to the summary counters only if raw events are absent.

    Returns a dict with validation results (included in summary.json).
    """
    results = {
        "agents_checked": 0,
        "agents_missing_summary": 0,
        "total_spawned": 0,
        "total_checkins": 0,
        "per_agent": {},
        "all_ok": True,
    }

    log_files = sorted(run_dir.glob("agent-*.jsonl"))
    if not log_files:
        # VM approach uses .log files with embedded JSONL
        log_files = sorted(run_dir.glob("agent-*.log"))

    for log_file in log_files:
        agent_id = log_file.stem  # e.g. "agent-0"
        started_workers = set()
        checked_in_workers = set()
        last_summary = None
        agent_start = None
        try:
            with open(log_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("event") == "worker_start" and event.get("worker_id"):
                        started_workers.add(event["worker_id"])
                    elif event.get("event") == "checkin" and event.get("worker_id"):
                        checked_in_workers.add(event["worker_id"])
                    elif event.get("event") == "checkin_summary":
                        last_summary = event
                    elif event.get("event") == "agent_start":
                        agent_start = event
        except Exception:
            pass

        if started_workers or checked_in_workers:
            spawned = len(started_workers)
            received = len(checked_in_workers)
            missing_checkins = sorted(started_workers - checked_in_workers)
            unexpected_checkins = sorted(checked_in_workers - started_workers)
            ok = not missing_checkins and not unexpected_checkins

            results["agents_checked"] += 1
            results["total_spawned"] += spawned
            results["total_checkins"] += received
            agent_result = {
                "spawned": spawned,
                "checkins": received,
                "ok": ok,
            }
            if missing_checkins:
                agent_result["missing_checkins"] = len(missing_checkins)
            if unexpected_checkins:
                agent_result["unexpected_checkins"] = len(unexpected_checkins)
            if last_summary:
                agent_result["summary_spawned"] = last_summary.get("workers_spawned", 0)
                agent_result["summary_checkins"] = last_summary.get("checkins_received", 0)
                agent_result["summary_ok"] = last_summary.get("checkins_ok", False)
            results["per_agent"][agent_id] = agent_result
            if not ok:
                results["all_ok"] = False
        elif last_summary:
            spawned = last_summary.get("workers_spawned", 0)
            received = last_summary.get("checkins_received", 0)
            ok = last_summary.get("checkins_ok", False)
            results["agents_checked"] += 1
            results["total_spawned"] += spawned
            results["total_checkins"] += received
            results["per_agent"][agent_id] = {
                "spawned": spawned,
                "checkins": received,
                "ok": ok,
            }
            if not ok:
                results["all_ok"] = False
        elif agent_start and agent_start.get("benchmark_mode") == "idle":
            results["agents_checked"] += 1
            results["per_agent"][agent_id] = {
                "spawned": 0,
                "checkins": 0,
                "ok": True,
            }
        else:
            results["agents_missing_summary"] += 1
            results["per_agent"][agent_id] = {"error": "no checkin_summary event"}
            results["all_ok"] = False

    return results


def validate_storage(run_dir: Path) -> dict:
    """
    Validate storage read/write results from agent logs.

    Each agent emits 'storage_summary' events with workers_tested, read_ok,
    and write_ok counts. We use the LAST summary per agent (same approach
    as validate_checkins).

    Returns a dict with validation results, or None if no storage events found.
    """
    results = {
        "agents_checked": 0,
        "total_tested": 0,
        "total_read_ok": 0,
        "total_write_ok": 0,
        "per_agent": {},
        "all_ok": True,
    }

    log_files = sorted(run_dir.glob("agent-*.jsonl"))
    if not log_files:
        log_files = sorted(run_dir.glob("agent-*.log"))

    found_any = False
    for log_file in log_files:
        agent_id = log_file.stem
        last_summary = None
        try:
            with open(log_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("event") == "storage_summary":
                        last_summary = event
        except Exception:
            pass

        if last_summary:
            found_any = True
            tested = last_summary.get("workers_tested", 0)
            read_ok = last_summary.get("read_ok", 0)
            write_ok = last_summary.get("write_ok", 0)
            results["agents_checked"] += 1
            results["total_tested"] += tested
            results["total_read_ok"] += read_ok
            results["total_write_ok"] += write_ok
            agent_ok = (read_ok == tested and write_ok == tested)
            results["per_agent"][agent_id] = {
                "tested": tested,
                "read_ok": read_ok,
                "write_ok": write_ok,
                "ok": agent_ok,
            }
            if not agent_ok:
                results["all_ok"] = False

    if not found_any:
        return None

    return results


def parse_spawn_latencies(run_dir: Path) -> dict:
    """Parse worker_spawn_timing and checkin events to compute spawn latency stats.

    Returns percentile stats for:
      - create: launch request -> create complete
      - start: create complete -> started
      - total: launch request -> started
      - cold_start: started -> first checkin
      - ready_total: launch request -> first checkin

    Returns None if no timing data.
    """
    create_vals = []
    start_vals = []
    total_vals = []
    cold_start_vals = []
    ready_total_vals = []

    log_files = sorted(run_dir.glob("agent-*.jsonl"))
    if not log_files:
        log_files = sorted(run_dir.glob("agent-*.log"))

    for log_file in log_files:
        spawn_total_by_worker = {}
        try:
            with open(log_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("event") == "worker_spawn_timing":
                        worker_id = event.get("worker_id")
                        if "create_ms" in event:
                            create_vals.append(event["create_ms"])
                        if "start_ms" in event:
                            start_vals.append(event["start_ms"])
                        if "total_ms" in event:
                            total_ms = event["total_ms"]
                            total_vals.append(total_ms)
                            if worker_id:
                                spawn_total_by_worker[worker_id] = total_ms
                    elif event.get("event") == "checkin":
                        if "cold_start_ms" in event:
                            cold_start_ms = event["cold_start_ms"]
                            cold_start_vals.append(cold_start_ms)
                            worker_id = event.get("worker_id")
                            total_ms = spawn_total_by_worker.get(worker_id)
                            if total_ms is not None:
                                ready_total_vals.append(total_ms + cold_start_ms)
        except Exception:
            pass

    if not total_vals:
        return None

    def _stats(vals):
        if not vals:
            return {}
        sorted_v = sorted(vals)
        n = len(sorted_v)
        return {
            "count": n,
            "min": round(sorted_v[0], 1),
            "mean": round(sum(sorted_v) / n, 1),
            "p50": round(sorted_v[int(0.50 * (n - 1))], 1),
            "p95": round(sorted_v[int(0.95 * (n - 1))], 1),
            "p99": round(sorted_v[int(0.99 * (n - 1))], 1),
            "max": round(sorted_v[-1], 1),
        }

    result = {"total": _stats(total_vals)}
    if create_vals:
        result["create"] = _stats(create_vals)
    if start_vals:
        result["start"] = _stats(start_vals)
    if cold_start_vals:
        result["cold_start"] = _stats(cold_start_vals)
    if ready_total_vals:
        result["ready_total"] = _stats(ready_total_vals)
    return result


def compute_percentiles(values: list, percentiles: list = None) -> dict:
    """Compute percentiles from a list of values."""
    if not values:
        return {}
    if percentiles is None:
        percentiles = [50, 95, 99]
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    result = {}
    for p in percentiles:
        idx = int(p / 100.0 * (n - 1))
        result[f"p{p}"] = sorted_vals[idx]
    return result


def compute_drift_slope(timestamps: list, values: list) -> float:
    """Compute linear slope (KiB/s) via simple linear regression."""
    n = len(timestamps)
    if n < 2:
        return 0.0
    mean_t = sum(timestamps) / n
    mean_v = sum(values) / n
    numerator = sum((t - mean_t) * (v - mean_v) for t, v in zip(timestamps, values))
    denominator = sum((t - mean_t) ** 2 for t in timestamps)
    if denominator == 0:
        return 0.0
    return numerator / denominator


def linear_regression(xs: list, ys: list) -> dict:
    """Fit y = intercept + slope * x and return slope/intercept/r2."""
    n = len(xs)
    if n < 2:
        return {}
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return {}
    slope = numerator / denominator
    intercept = mean_y - slope * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 if ss_tot == 0 else max(0.0, 1.0 - (ss_res / ss_tot))
    return {"slope": slope, "intercept": intercept, "r2": r2}


def build_plateau_summary(
    running_samples: list,
    baseline_kb: float,
    params: dict,
    num_agents: int,
) -> dict:
    """Compute per-plateau steady-state points and worker marginal costs."""
    config = params.get("config", {})
    targets = config.get("plateau_workers_per_agent") or []
    hold_s = config.get("plateau_hold_s", 0)
    settle_s = config.get("plateau_settle_s", 0)

    if not targets or hold_s <= 0:
        return None

    points = []
    plateau_steady_samples = []
    for index, target in enumerate(targets):
        stage_start = index * hold_s
        stage_end = stage_start + hold_s
        stage_samples = [
            s for s in running_samples
            if stage_start <= s["timestamp_s"] < stage_end
        ]
        if not stage_samples:
            points.append({
                "workers_per_agent": target,
                "error": "no samples",
            })
            continue
        steady_start = stage_start + min(settle_s, hold_s)
        stage_steady = [s for s in stage_samples if s["timestamp_s"] >= steady_start]
        if not stage_steady:
            stage_steady = stage_samples
        plateau_steady_samples.extend(stage_steady)

        consumed_values = [s["host_consumed_kb"] for s in stage_steady]
        worker_values = [s["active_workers"] for s in stage_steady]
        point = {
            "workers_per_agent": target,
            "samples": len(stage_samples),
            "steady_samples": len(stage_steady),
            "mean_net_mib": round(
                ((sum(consumed_values) / len(consumed_values)) - baseline_kb) / 1024, 3
            ),
            "peak_net_mib": round((max(consumed_values) - baseline_kb) / 1024, 3),
            "mean_active_workers": round(sum(worker_values) / len(worker_values), 3),
        }
        points.append(point)

    zero_point = next(
        (
            point for point in points
            if point.get("workers_per_agent") == 0 and "mean_net_mib" in point
        ),
        None,
    )

    first_worker_tax_mib = None
    steady_worker_mib = None
    worker_fit_r2 = None
    zero_point_per_agent_mib = None

    if zero_point:
        zero_workers = zero_point["mean_active_workers"]
        zero_net_mib = zero_point["mean_net_mib"]
        zero_point_per_agent_mib = zero_net_mib / max(num_agents, 1)
        delta_points = []
        for point in points:
            if "mean_net_mib" not in point:
                continue
            delta_workers = point["mean_active_workers"] - zero_workers
            delta_mib = point["mean_net_mib"] - zero_net_mib
            point["delta_active_workers"] = round(delta_workers, 3)
            point["delta_net_mib"] = round(delta_mib, 3)
            if delta_workers > 0:
                delta_points.append({
                    "workers_per_agent": point["workers_per_agent"],
                    "delta_active_workers": delta_workers,
                    "delta_net_mib": delta_mib,
                })

        if delta_points:
            first = delta_points[0]
            first_worker_tax_mib = first["delta_net_mib"] / first["delta_active_workers"]

        regression_points = [
            point for point in delta_points if point["workers_per_agent"] >= 2
        ]
        if len(regression_points) < 2:
            regression_points = delta_points
        if len(regression_points) >= 2:
            fit = linear_regression(
                [point["delta_active_workers"] for point in regression_points],
                [point["delta_net_mib"] for point in regression_points],
            )
            if fit:
                steady_worker_mib = fit["slope"]
                worker_fit_r2 = fit["r2"]

    return {
        "workers_per_agent": targets,
        "hold_s": hold_s,
        "settle_s": settle_s,
        "points": points,
        "zero_point_per_agent_mib": round(zero_point_per_agent_mib, 3)
        if zero_point_per_agent_mib is not None else None,
        "first_worker_tax_mib": round(first_worker_tax_mib, 3)
        if first_worker_tax_mib is not None else None,
        "steady_worker_mib": round(steady_worker_mib, 3)
        if steady_worker_mib is not None else None,
        "worker_fit_r2": round(worker_fit_r2, 4)
        if worker_fit_r2 is not None else None,
        "steady_samples": len(plateau_steady_samples),
    }


def run_benchmark(
    approach: Approach,
    num_agents: int,
    config: BenchmarkConfig,
    output_dir: Path,
    sample_interval_ms: int,
    mode: str,
) -> Path:
    """Execute a single benchmark run."""
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    run_dir = output_dir / f"{approach.name}-{mode}-n{num_agents}-{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Save parameters
    params = {
        "approach": approach.name,
        "mode": mode,
        "num_agents": num_agents,
        "config": {
            "benchmark_mode": config.benchmark_mode,
            "agent_memory_mb": config.agent_memory_mb,
            "agent_baseline_mb": config.agent_baseline_mb,
            "max_concurrent_workers": config.max_concurrent_workers,
            "spawn_interval_mean_s": config.spawn_interval_mean_s,
            "benchmark_duration_s": config.benchmark_duration_s,
            "worker_image": config.worker_image,
            "worker_memory_limit_mb": config.worker_memory_limit_mb,
            "worker_memory_mb": config.worker_memory_mb,
            "worker_duration_min_s": config.worker_duration_min_s,
            "worker_duration_max_s": config.worker_duration_max_s,
            "worker_lifetime_mode": config.worker_lifetime_mode,
            "plateau_workers_per_agent": config.plateau_workers_per_agent,
            "plateau_hold_s": config.plateau_hold_s,
            "plateau_settle_s": config.plateau_settle_s,
        },
        "run_id": config.run_id,
        "rng_seed": config.rng_seed,
        "sample_interval_ms": sample_interval_ms,
        "timestamp": timestamp,
    }
    with open(run_dir / "params.json", "w") as f:
        json.dump(params, f, indent=2)

    print(f"\n{'=' * 60}")
    print(f"Benchmark: {approach.name} with {num_agents} agents (mode={mode})")
    print(f"Run ID: {config.run_id}")
    print(f"RNG seed: {config.rng_seed}")
    print(f"Output: {run_dir}")
    print(f"Duration: {config.benchmark_duration_s}s + 20s baseline/settle")
    print(f"{'=' * 60}\n")

    # Pre-flight checks
    swap_status = check_swap_status()

    # Setup approach
    print("Setting up approach...")
    approach.setup(config)

    # Open timeseries output
    ts_file = open(run_dir / "timeseries.jsonl", "w")

    # Create collector
    collector = Collector(interval_ms=sample_interval_ms)
    agent_ids = []

    try:
        # Phase 1: Baseline (10s, no agents)
        print("Recording baseline memory (10s)...")
        baseline_collector = Collector(interval_ms=sample_interval_ms, phase="baseline")
        collector_thread = baseline_collector.run_in_thread(
            ts_file,
            get_agent_pids=lambda: {},
            get_daemon_pids=approach.get_daemon_pids,
            count_workers=lambda: 0,
        )
        time.sleep(10)

        # Phase 2: Start agents
        baseline_collector.stop()
        collector_thread.join(timeout=5)

        print(f"Starting {num_agents} agents...")
        agent_ids = approach.start_agents(num_agents, config)
        print(f"Agents started: {agent_ids}")

        # Start new collector with agent PIDs
        collector = Collector(interval_ms=sample_interval_ms, phase="running")
        collector_thread = collector.run_in_thread(
            ts_file,
            get_agent_pids=approach.get_agent_pids,
            get_daemon_pids=approach.get_daemon_pids,
            count_workers=approach.count_active_workers,
        )

        if mode in ("idle", "plateau"):
            print(f"Releasing {mode} benchmark start barrier...")
            approach.start_benchmark()

        # Phase 3: Run for benchmark duration
        duration = config.benchmark_duration_s
        print(f"Collecting data for {duration}s...")
        start = time.monotonic()
        while time.monotonic() - start < duration:
            elapsed = int(time.monotonic() - start)
            workers = approach.count_active_workers()
            workers_str = str(workers) if workers >= 0 else "N/A (inside VMs)"
            sys.stdout.write(
                f"\r  [{elapsed}/{duration}s] Active workers: {workers_str}    "
            )
            sys.stdout.flush()
            time.sleep(5)
        print()

        # Phase 4: Stop agents gracefully (SIGTERM allows cleanup + checkin_summary)
        print("Stopping agents...")
        approach.stop_agents()

        # Phase 5: Collect agent logs (from stopped but not yet removed containers)
        print("Collecting agent logs...")
        approach.collect_agent_logs(agent_ids, run_dir)

        # Phase 6: Remove containers and settle (10s)
        if hasattr(approach, "remove_containers"):
            approach.remove_containers()
        # Phase 7: Approach-specific cleanup (e.g. Podman user deletion, VM dir removal)
        approach.cleanup()
        print("Waiting for memory to settle (10s)...")
        time.sleep(10)

    finally:
        collector.stop()
        collector_thread.join(timeout=5)
        ts_file.close()

    # Post-flight: check swap activity
    swap_result = check_swap_activity(swap_status)

    # Generate summary
    summary = generate_summary(run_dir, swap_result)
    with open(run_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\nResults saved to {run_dir}")
    print(f"  Steady-state mean: {summary.get('steady_state_mean_mib', 0):.0f} MiB")
    print(f"  Peak: {summary.get('peak_mib', 0):.0f} MiB")
    print(f"  p95: {summary.get('p95_mib', 0):.0f} MiB")
    avg_w = summary.get('avg_workers', 0)
    print(f"  Avg workers: {avg_w:.1f}" if avg_w >= 0 else "  Avg workers: N/A")
    print(f"  Per-agent: {summary.get('per_agent_mean_mib', 0):.0f} MiB")
    plateau = summary.get("plateau")
    if plateau:
        zero = plateau.get("zero_point_per_agent_mib")
        first = plateau.get("first_worker_tax_mib")
        steady = plateau.get("steady_worker_mib")
        fit_r2 = plateau.get("worker_fit_r2")
        if zero is not None:
            print(f"  Plateau zero-point: {zero:.1f} MiB/agent")
        if first is not None:
            print(f"  First worker tax: {first:.1f} MiB/worker")
        if steady is not None:
            suffix = f" (r2={fit_r2:.3f})" if fit_r2 is not None else ""
            print(f"  Steady worker slope: {steady:.1f} MiB/worker{suffix}")
    drift = summary.get('drift_kb_per_s', 0)
    if abs(drift) > 10:
        print(f"  Drift: {drift:.1f} KiB/s (potential leak)")
    if 'host_cpu_pct' in summary:
        print(f"  Host CPU: {summary['host_cpu_pct']}%")
    if 'per_agent_cpu_s' in summary:
        print(f"  Per-agent CPU: {summary['per_agent_cpu_s']}s (avg)")
    for dname, dinfo in summary.get("daemon_overhead", {}).items():
        if "delta_pss_mib" in dinfo:
            print(f"  Daemon {dname}: {dinfo['mean_pss_mib']:.0f} MiB PSS "
                  f"(+{dinfo['delta_pss_mib']:.0f} MiB from baseline)")

    # Checkin validation results
    cv = summary.get("checkin_validation")
    if cv:
        total_s = cv["total_spawned"]
        total_c = cv["total_checkins"]
        if cv["all_ok"]:
            print(f"  Checkins: {total_c}/{total_s} OK")
        else:
            print(f"\n  CHECKIN VALIDATION FAILED: {total_c}/{total_s} workers checked in")
            for agent_id, info in sorted(cv["per_agent"].items()):
                if "error" in info:
                    print(f"    {agent_id}: {info['error']}")
                elif not info.get("ok"):
                    print(f"    {agent_id}: {info['checkins']}/{info['spawned']} checkins")

    # Storage validation results
    sv = summary.get("storage_validation")
    if sv:
        total_t = sv["total_tested"]
        total_r = sv["total_read_ok"]
        total_w = sv["total_write_ok"]
        if sv["all_ok"]:
            print(f"  Storage: {total_r}/{total_t} read OK, {total_w}/{total_t} write OK")
        else:
            print(f"\n  STORAGE VALIDATION FAILED: "
                  f"{total_r}/{total_t} read OK, {total_w}/{total_t} write OK")
            for agent_id, info in sorted(sv["per_agent"].items()):
                if not info.get("ok"):
                    print(f"    {agent_id}: {info['read_ok']}/{info['tested']} read, "
                          f"{info['write_ok']}/{info['tested']} write")

    # Spawn latency results
    sl = summary.get("spawn_latency")
    if sl:
        t = sl.get("total", {})
        print(f"  Spawn latency (total): p50={t.get('p50', 0):.0f}ms "
              f"p95={t.get('p95', 0):.0f}ms max={t.get('max', 0):.0f}ms")
        rt = sl.get("ready_total")
        if rt:
            print(f"  Ready latency (launch→checkin): p50={rt.get('p50', 0):.0f}ms "
                  f"p95={rt.get('p95', 0):.0f}ms max={rt.get('max', 0):.0f}ms")
        cs = sl.get("cold_start")
        if cs:
            print(f"  Post-start checkin: p50={cs.get('p50', 0):.0f}ms "
                  f"p95={cs.get('p95', 0):.0f}ms max={cs.get('max', 0):.0f}ms")

    return run_dir


def generate_summary(run_dir: Path, swap_result: dict = None) -> dict:
    """Generate summary statistics from a benchmark run."""
    with open(run_dir / "params.json") as f:
        params = json.load(f)

    samples = []
    with open(run_dir / "timeseries.jsonl") as f:
        for line in f:
            if line.strip():
                samples.append(json.loads(line))

    if not samples:
        return {"error": "No samples collected"}

    num_agents = params["num_agents"]

    # Compute baseline from samples explicitly tagged as "baseline" phase.
    # Fall back to timestamp heuristic for old data without phase tags.
    baseline_samples = [s for s in samples if s.get("phase") == "baseline"]
    if not baseline_samples:
        baseline_samples = [s for s in samples if s["timestamp_s"] < 10]
    baseline_kb = (
        sum(s["host_consumed_kb"] for s in baseline_samples) / len(baseline_samples)
        if baseline_samples
        else 0
    )

    # Running window: samples captured during the configured benchmark only.
    # Exclude teardown/settle samples that are still tagged as "running"
    # because the collector stays active through log collection and cleanup.
    benchmark_duration_s = params.get("config", {}).get("benchmark_duration_s")
    running_samples = [
        s for s in samples
        if s.get("phase", "running") == "running"
        and (
            benchmark_duration_s is None
            or s["timestamp_s"] <= benchmark_duration_s
        )
    ]
    if not running_samples:
        running_samples = [s for s in samples if s not in baseline_samples]

    mode = params.get("mode", "loaded")
    plateau = None
    if mode == "plateau":
        plateau = build_plateau_summary(running_samples, baseline_kb, params, num_agents)
        steady_samples = []
        config = params.get("config", {})
        hold_s = config.get("plateau_hold_s", 0)
        settle_s = config.get("plateau_settle_s", 0)
        targets = config.get("plateau_workers_per_agent") or []
        for index, _target in enumerate(targets):
            stage_start = index * hold_s
            stage_end = stage_start + hold_s
            steady_start = stage_start + min(settle_s, hold_s)
            steady_samples.extend(
                s for s in running_samples
                if steady_start <= s["timestamp_s"] < stage_end
            )
        if not steady_samples:
            steady_samples = running_samples
    else:
        warmup_s = 60
        steady_samples = [s for s in running_samples if s["timestamp_s"] >= warmup_s]
        if not steady_samples:
            steady_samples = running_samples

    consumed_values = [s["host_consumed_kb"] for s in steady_samples]
    worker_values = [s["active_workers"] for s in steady_samples]

    mean_consumed_kb = sum(consumed_values) / len(consumed_values)
    peak_consumed_kb = max(consumed_values)
    mean_workers = sum(worker_values) / len(worker_values)

    # Subtract baseline for net overhead
    net_consumed_kb = mean_consumed_kb - baseline_kb

    # Percentiles (baseline-subtracted)
    net_values = [v - baseline_kb for v in consumed_values]
    pcts = compute_percentiles(net_values, [50, 95, 99])

    # Drift detection (slope of consumed memory during steady state)
    timestamps = [s["timestamp_s"] for s in steady_samples]
    drift = compute_drift_slope(timestamps, consumed_values)

    # Daemon overhead: baseline (before agents) and steady-state (with agents)
    daemon_baseline = {}
    for s in baseline_samples:
        for daemon_name, daemon_data in s.get("daemons", {}).items():
            if daemon_name not in daemon_baseline:
                daemon_baseline[daemon_name] = {"rss_sum": 0, "pss_sum": 0, "count": 0}
            daemon_baseline[daemon_name]["rss_sum"] += daemon_data.get("rss_kb", 0)
            daemon_baseline[daemon_name]["pss_sum"] += daemon_data.get("pss_kb", 0)
            daemon_baseline[daemon_name]["count"] += 1

    daemon_overhead = {}
    for s in steady_samples:
        for daemon_name, daemon_data in s.get("daemons", {}).items():
            if daemon_name not in daemon_overhead:
                daemon_overhead[daemon_name] = {"rss_sum": 0, "pss_sum": 0, "count": 0}
            daemon_overhead[daemon_name]["rss_sum"] += daemon_data.get("rss_kb", 0)
            daemon_overhead[daemon_name]["pss_sum"] += daemon_data.get("pss_kb", 0)
            daemon_overhead[daemon_name]["count"] += 1

    daemon_summary = {}
    for name, data in daemon_overhead.items():
        if data["count"] > 0:
            mean_rss_kb = data["rss_sum"] / data["count"]
            mean_pss_kb = data["pss_sum"] / data["count"]
            entry = {
                "mean_rss_mib": mean_rss_kb / 1024,
                "mean_pss_mib": mean_pss_kb / 1024,
            }
            # Compute delta from baseline to isolate growth caused by agents
            bl = daemon_baseline.get(name)
            if bl and bl["count"] > 0:
                bl_rss_kb = bl["rss_sum"] / bl["count"]
                bl_pss_kb = bl["pss_sum"] / bl["count"]
                entry["baseline_rss_mib"] = bl_rss_kb / 1024
                entry["baseline_pss_mib"] = bl_pss_kb / 1024
                entry["delta_rss_mib"] = (mean_rss_kb - bl_rss_kb) / 1024
                entry["delta_pss_mib"] = (mean_pss_kb - bl_pss_kb) / 1024
            daemon_summary[name] = entry

    # --- CPU utilization ---
    clk_tck = os.sysconf("SC_CLK_TCK")  # typically 100

    # Host CPU %: delta of busy vs total jiffies across steady state
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

    # Per-agent CPU seconds: delta of (utime+stime) / CLK_TCK
    agent_cpu_seconds = []
    entity_cpu_samples = [s for s in steady_samples if s.get("entity_cpu")]
    if entity_cpu_samples:
        # For each agent, find first and last sample where it appears
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
                delta_u = last["utime"] - first["utime"]
                delta_s = last["stime"] - first["stime"]
                agent_cpu_seconds.append((delta_u + delta_s) / clk_tck)

    per_agent_cpu_s = None
    if agent_cpu_seconds:
        per_agent_cpu_s = round(sum(agent_cpu_seconds) / len(agent_cpu_seconds), 1)

    # Daemon CPU seconds
    daemon_cpu_summary = {}
    daemon_cpu_samples = [s for s in steady_samples if s.get("daemon_cpu")]
    if daemon_cpu_samples:
        all_daemons = set()
        for s in daemon_cpu_samples:
            all_daemons.update(s["daemon_cpu"].keys())
        for daemon_name in sorted(all_daemons):
            first = None
            last = None
            for s in daemon_cpu_samples:
                if daemon_name in s["daemon_cpu"]:
                    if first is None:
                        first = s["daemon_cpu"][daemon_name]
                    last = s["daemon_cpu"][daemon_name]
            if first is not None and last is not None:
                delta_u = last["utime"] - first["utime"]
                delta_s = last["stime"] - first["stime"]
                daemon_cpu_summary[daemon_name] = {
                    "cpu_s": round((delta_u + delta_s) / clk_tck, 1),
                }

    # Agent log stats
    agent_log_stats = parse_agent_logs(run_dir)

    # Worker checkin validation
    checkin_validation = validate_checkins(run_dir)

    summary = {
        "approach": params["approach"],
        "mode": mode,
        "num_agents": num_agents,
        "run_id": params.get("run_id", "unknown"),
        "rng_seed": params.get("rng_seed"),
        "baseline_mib": baseline_kb / 1024,
        "steady_state_mean_mib": net_consumed_kb / 1024,
        "peak_mib": (peak_consumed_kb - baseline_kb) / 1024,
        "p50_mib": pcts.get("p50", 0) / 1024,
        "p95_mib": pcts.get("p95", 0) / 1024,
        "p99_mib": pcts.get("p99", 0) / 1024,
        "avg_workers": mean_workers,
        "per_agent_mean_mib": (net_consumed_kb / 1024) / max(num_agents, 1),
        "drift_kb_per_s": round(drift, 2),
        "total_samples": len(samples),
        "steady_state_samples": len(steady_samples),
        "daemon_overhead": daemon_summary,
        **agent_log_stats,
    }

    # CPU stats
    if host_cpu_pct is not None:
        summary["host_cpu_pct"] = host_cpu_pct
    if per_agent_cpu_s is not None:
        summary["per_agent_cpu_s"] = per_agent_cpu_s
    if daemon_cpu_summary:
        summary["daemon_cpu"] = daemon_cpu_summary

    # Swap info
    if swap_result:
        summary["swap"] = swap_result

    # Checkin validation
    if checkin_validation["agents_checked"] > 0 or checkin_validation["agents_missing_summary"] > 0:
        summary["checkin_validation"] = checkin_validation

    # Storage validation
    storage_validation = validate_storage(run_dir)
    if storage_validation is not None:
        summary["storage_validation"] = storage_validation

    # Spawn latency stats
    spawn_latency = parse_spawn_latencies(run_dir)
    if spawn_latency is not None:
        summary["spawn_latency"] = spawn_latency

    if plateau is not None:
        summary["plateau"] = plateau

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Run a single isolation benchmark."
    )
    parser.add_argument(
        "--approach",
        required=True,
        help="Approach name (e.g., container-docker, vm-qemu)",
    )
    parser.add_argument(
        "--agents",
        type=int,
        default=None,
        help="Number of agents (default: from config.env)",
    )
    parser.add_argument(
        "--mode",
        choices=["loaded", "idle", "plateau"],
        default="loaded",
        help="Benchmark mode: 'loaded' (default), 'idle', or 'plateau'",
    )
    parser.add_argument(
        "--plateau-workers-per-agent",
        type=str,
        default=None,
        help="Comma-separated plateau worker targets per agent, e.g. 0,1,2,3,4,5",
    )
    parser.add_argument(
        "--plateau-hold-s",
        type=int,
        default=None,
        help="Seconds to hold each plateau target",
    )
    parser.add_argument(
        "--plateau-settle-s",
        type=int,
        default=None,
        help="Seconds to discard at the start of each plateau window",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Output directory (default: benchmarks/results/)",
    )
    parser.add_argument(
        "--sample-interval-ms",
        type=int,
        default=None,
        help="Collection interval in ms (default: from config.env or 500)",
    )
    args = parser.parse_args()

    # Load config
    config_env = load_config_env()
    # Apply config.env as defaults, then let BenchmarkConfig read from os.environ
    for k, v in config_env.items():
        os.environ.setdefault(k, v)

    config = BenchmarkConfig.from_env()
    num_agents = args.agents or int(config_env.get("DEFAULT_AGENTS", "5"))
    sample_interval = args.sample_interval_ms or int(
        config_env.get("SAMPLE_INTERVAL_MS", "500")
    )
    output_dir = Path(args.output_dir) if args.output_dir else BENCH_DIR / "results"

    config.benchmark_mode = args.mode
    if args.plateau_workers_per_agent is not None:
        config.plateau_workers_per_agent = [
            int(part.strip())
            for part in args.plateau_workers_per_agent.split(",")
            if part.strip()
        ]
    if args.plateau_hold_s is not None:
        config.plateau_hold_s = args.plateau_hold_s
    if args.plateau_settle_s is not None:
        config.plateau_settle_s = args.plateau_settle_s

    # Apply mode overrides
    if args.mode == "idle":
        config.max_concurrent_workers = 0
        # Shorter duration for idle mode — overhead stabilizes fast
        if config.benchmark_duration_s > 120:
            config.benchmark_duration_s = 120
            print(f"Idle mode: reduced duration to {config.benchmark_duration_s}s")
    elif args.mode == "plateau":
        if not config.plateau_workers_per_agent:
            config.plateau_workers_per_agent = list(
                range(config.max_concurrent_workers + 1)
            )
        if config.plateau_workers_per_agent[0] != 0:
            raise SystemExit("Plateau mode requires the first target to be 0 workers.")
        if any(target < 0 for target in config.plateau_workers_per_agent):
            raise SystemExit("Plateau mode does not allow negative worker targets.")
        if any(
            later < earlier
            for earlier, later in zip(
                config.plateau_workers_per_agent,
                config.plateau_workers_per_agent[1:],
            )
        ):
            raise SystemExit(
                "Plateau mode currently requires a non-decreasing worker schedule."
            )
        if config.plateau_hold_s <= 0:
            raise SystemExit("Plateau mode requires PLATEAU_HOLD_S > 0.")
        if config.plateau_settle_s < 0 or config.plateau_settle_s >= config.plateau_hold_s:
            raise SystemExit(
                "Plateau mode requires 0 <= PLATEAU_SETTLE_S < PLATEAU_HOLD_S."
            )
        config.max_concurrent_workers = max(config.plateau_workers_per_agent)
        config.worker_lifetime_mode = "hold"
        config.benchmark_duration_s = (
            len(config.plateau_workers_per_agent) * config.plateau_hold_s
        )
        print(
            "Plateau mode: "
            f"targets={config.plateau_workers_per_agent} "
            f"hold={config.plateau_hold_s}s settle={config.plateau_settle_s}s "
            f"duration={config.benchmark_duration_s}s"
        )

    # Discover and select approach
    discover_approaches()
    if args.approach not in APPROACHES:
        available = ", ".join(sorted(APPROACHES.keys())) or "(none found)"
        print(f"Error: unknown approach '{args.approach}'")
        print(f"Available: {available}")
        sys.exit(1)

    approach = APPROACHES[args.approach]

    # Run
    run_dir = run_benchmark(approach, num_agents, config, output_dir, sample_interval, args.mode)

    # Exit non-zero if checkin or storage validation failed
    summary_path = run_dir / "summary.json"
    if summary_path.exists():
        with open(summary_path) as f:
            summary = json.load(f)
        cv = summary.get("checkin_validation", {})
        if cv and not cv.get("all_ok", True):
            sys.exit(1)
        sv = summary.get("storage_validation", {})
        if sv and not sv.get("all_ok", True):
            sys.exit(1)


if __name__ == "__main__":
    main()
