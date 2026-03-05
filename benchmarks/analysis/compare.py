#!/usr/bin/env python3
"""
Compare benchmark results from multiple runs.

Reads summary.json files from all run directories in results/,
prints a markdown comparison table with percentiles, daemon overhead,
and swap warnings.

Usage:
    python3 -m analysis.compare [results-dir]
"""

import json
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent.parent


def load_summaries(results_dir: Path) -> list:
    """Load all summary.json files from result directories."""
    summaries = []
    for run_dir in sorted(results_dir.iterdir()):
        summary_file = run_dir / "summary.json"
        if summary_file.exists():
            with open(summary_file) as f:
                summary = json.load(f)
                summary["run_dir"] = str(run_dir.name)
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
        "Peak (net) | p95 (net) | Avg Workers | Per-Agent | Drift (KiB/s) |"
    )
    separator = (
        "|--------|--------|------------------|----------|----------|----------|"
        "-----------|-----------|-------------|-----------|---------------|"
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

        workers_str = f"{workers:.1f}" if workers >= 0 else "N/A"

        # Flag drift
        drift_str = f"{drift:.1f}" if abs(drift) > 1 else "-"

        print(
            f"| {agents:>6} | {mode:<6} | {approach:<16} | {baseline:>8.0f} | "
            f"{abs_mean:>8.0f} | {net_mean:>8.0f} | "
            f"{peak:>9.0f} | {p95:>9.0f} | {workers_str:>11} | {per_agent:>9.0f} | "
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
                name = f"{s.get('approach', '?')}-{s.get('mode', '?')}-n{s.get('num_agents', '?')}"
                print(f"| {name:<30} | {total:>13} | {max_c:>14} |")
        print()

    # Print spawn latency stats if available
    has_latency = any(s.get("spawn_latency") for s in summaries)
    if has_latency:
        print("Spawn Latency (ms):")
        header = (
            "| Approach         | Create p50 | Create p95 | Start p50 | Start p95 "
            "| Total p50 | Total p95 | Cold-Start p50 | Cold-Start p95 |"
        )
        sep = (
            "|------------------|------------|------------|-----------|-----------|"
            "-----------|-----------|----------------|----------------|"
        )
        print(header)
        print(sep)
        for s in sorted(summaries, key=sort_key):
            sl = s.get("spawn_latency")
            if not sl:
                continue
            approach = s.get("approach", "unknown")
            cr = sl.get("create", {})
            st = sl.get("start", {})
            to = sl.get("total", {})
            cs = sl.get("cold_start", {})

            def _fmt(v):
                return f"{v:.0f}" if v else "-"

            print(
                f"| {approach:<16} "
                f"| {_fmt(cr.get('p50')):>10} | {_fmt(cr.get('p95')):>10} "
                f"| {_fmt(st.get('p50')):>9} | {_fmt(st.get('p95')):>9} "
                f"| {_fmt(to.get('p50')):>9} | {_fmt(to.get('p95')):>9} "
                f"| {_fmt(cs.get('p50')):>14} | {_fmt(cs.get('p95')):>14} |"
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
            for metric_name, metric_key in [("create", "create"), ("start", "start"),
                                             ("total", "total"), ("cold_start", "cold_start")]:
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
