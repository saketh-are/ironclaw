#!/usr/bin/env python3
"""
Compare benchmark results from multiple runs.

Reads summary.json files from all run directories in results/,
recursively, then prints a markdown comparison table with percentiles,
daemon overhead, and swap warnings.

Usage:
    python3 -m analysis.compare [results-dir]
"""

import json
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent.parent


def load_summaries(results_dir: Path) -> list:
    """Load all summary.json files from result directories recursively."""
    summaries = []
    for summary_file in sorted(results_dir.rglob("summary.json")):
        run_dir = summary_file.parent
        with open(summary_file) as f:
            summary = json.load(f)
            summary["run_dir"] = str(run_dir.relative_to(results_dir))
            summaries.append(summary)
    return summaries


def print_table(summaries: list) -> None:
    """Print a markdown comparison table."""
    if not summaries:
        print("No results found.")
        return

    # Check for any swap activity
    swap_runs = []
    for s in summaries:
        swap = s.get("swap", {})
        if swap.get("swap_occurred"):
            swap_runs.append(s.get("run_dir", "unknown"))

    if swap_runs:
        print("\nWARNING: Swap activity detected in the following runs:")
        for r in swap_runs:
            print(f"  - {r}")
        print("Results may not reflect true physical memory usage.\n")

    print()
    header = (
        "| Agents | Mode   | Approach         | Baseline | Abs Mean | Net Mean | "
        "Peak (net) | p95 (net) | Avg Workers | Per-Agent | Daemon Delta | Host CPU % | Per-Agent CPU (s) | Drift (KiB/s) |"
    )
    separator = (
        "|--------|--------|------------------|----------|----------|----------|"
        "-----------|-----------|-------------|-----------|--------------|------------|-------------------|---------------|"
    )
    print(header)
    print(separator)

    sort_key = lambda x: (
        x.get("mode", "loaded"),
        x.get("num_agents", 0),
        x.get("approach", ""),
    )

    for s in sorted(summaries, key=sort_key):
        approach = s.get("approach", "unknown")
        mode = s.get("mode", "loaded")
        agents = s.get("num_agents", 0)
        baseline = s.get("baseline_mib", 0)
        net_mean = s.get("steady_state_mean_mib", 0)
        abs_mean = baseline + net_mean
        peak = s.get("peak_mib", 0)
        p95 = s.get("p95_mib", 0)
        workers = s.get("avg_workers", 0)
        per_agent = s.get("per_agent_mean_mib", 0)
        drift = s.get("drift_kb_per_s", 0)

        host_cpu = s.get("host_cpu_pct")
        agent_cpu = s.get("per_agent_cpu_s")

        # Sum daemon PSS delta across all daemons (growth attributable to agents)
        daemon_delta_mib = 0.0
        has_daemon_delta = False
        for dinfo in s.get("daemon_overhead", {}).values():
            if "delta_pss_mib" in dinfo:
                daemon_delta_mib += dinfo["delta_pss_mib"]
                has_daemon_delta = True

        workers_str = f"{workers:.1f}" if workers >= 0 else "N/A"
        daemon_delta_str = f"{daemon_delta_mib:.0f}" if has_daemon_delta else "-"
        host_cpu_str = f"{host_cpu:.1f}" if host_cpu is not None else "-"
        agent_cpu_str = f"{agent_cpu:.1f}" if agent_cpu is not None else "-"

        # Flag drift
        drift_str = f"{drift:.1f}" if abs(drift) > 1 else "-"

        print(
            f"| {agents:>6} | {mode:<6} | {approach:<16} | {baseline:>8.0f} | "
            f"{abs_mean:>8.0f} | {net_mean:>8.0f} | "
            f"{peak:>9.0f} | {p95:>9.0f} | {workers_str:>11} | {per_agent:>9.0f} | "
            f"{daemon_delta_str:>12} | "
            f"{host_cpu_str:>10} | {agent_cpu_str:>17} | "
            f"{drift_str:>13} |"
        )

    print()

    # Print worker spawn stats if available
    has_worker_stats = any(s.get("total_workers_spawned", 0) > 0 for s in summaries)
    if has_worker_stats:
        print("Worker Statistics:")
        print("| Run | Total Spawned | Max Concurrent |")
        print("|-----|---------------|----------------|")
        for s in sorted(summaries, key=sort_key):
            total = s.get("total_workers_spawned", 0)
            max_c = s.get("max_concurrent_workers", 0)
            if total > 0:
                name = s.get("run_dir", "unknown")
                print(f"| {name:<30} | {total:>13} | {max_c:>14} |")
        print()

    plateau_summaries = [s for s in summaries if s.get("plateau")]
    if plateau_summaries:
        print("Plateau Decomposition:")
        print("| Approach | Run | Zero/Agent | First Worker | Steady Worker | R2 |")
        print("|----------|-----|------------|--------------|---------------|----|")
        for s in sorted(plateau_summaries, key=sort_key):
            plateau = s["plateau"]
            run_name = s.get("run_dir", "unknown")
            zero = plateau.get("zero_point_per_agent_mib")
            first = plateau.get("first_worker_tax_mib")
            steady = plateau.get("steady_worker_mib")
            r2 = plateau.get("worker_fit_r2")

            def _fmt(v, digits=1):
                return f"{v:.{digits}f}" if v is not None else "-"

            print(
                f"| {s.get('approach', 'unknown')} | {run_name} | "
                f"{_fmt(zero)} | {_fmt(first)} | {_fmt(steady)} | {_fmt(r2, 4)} |"
            )
        print()

    # Print spawn latency stats if available
    has_latency = any(s.get("spawn_latency") for s in summaries)
    if has_latency:
        ready_summaries = []
        for s in sorted(summaries, key=sort_key):
            sl = s.get("spawn_latency")
            if not sl:
                continue
            rt = sl.get("ready_total", {})
            if rt.get("count", 0) > 0:
                ready_summaries.append((s, rt))

        print("Ready Latency (launch -> first checkin, ms):")
        header = (
            "| Approach         | Ready p50 | Ready p95 |"
        )
        sep = (
            "|------------------|-----------|-----------|"
        )
        print(header)
        print(sep)
        for s, rt in ready_summaries:
            approach = s.get("approach", "unknown")

            def _fmt(v):
                return f"{v:.0f}" if v else "-"

            print(
                f"| {approach:<16} "
                f"| {_fmt(rt.get('p50')):>9} | {_fmt(rt.get('p95')):>9} |"
            )
        print()

        # Also print full stats table (min/mean/max)
        print("Spawn Latency Detail (ms):")
        print("| Approach         | Metric     | Count | Min    | Mean   | p50    | p95    | p99    | Max    |")
        print("|------------------|------------|-------|--------|--------|--------|--------|--------|--------|")
        for s in sorted(summaries, key=sort_key):
            sl = s.get("spawn_latency")
            if not sl:
                continue
            approach = s.get("approach", "unknown")
            for metric_name, metric_key in [
                ("create", "create"),
                ("start", "start"),
                ("spawn_total", "total"),
                ("post_start_checkin", "cold_start"),
                ("ready_total", "ready_total"),
            ]:
                data = sl.get(metric_key, {})
                if not data:
                    continue
                print(
                    f"| {approach:<16} | {metric_name:<10} "
                    f"| {data.get('count', 0):>5} "
                    f"| {data.get('min', 0):>6.0f} | {data.get('mean', 0):>6.0f} "
                    f"| {data.get('p50', 0):>6.0f} | {data.get('p95', 0):>6.0f} "
                    f"| {data.get('p99', 0):>6.0f} | {data.get('max', 0):>6.0f} |"
                )
        print()


def main():
    results_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else BENCH_DIR / "results"

    if not results_dir.exists():
        print(f"Results directory not found: {results_dir}")
        sys.exit(1)

    summaries = load_summaries(results_dir)
    print_table(summaries)


if __name__ == "__main__":
    main()
