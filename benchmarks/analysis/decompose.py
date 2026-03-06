#!/usr/bin/env python3
"""
Fit per-agent idle cost and report plateau-mode worker costs.

Usage:
    python3 -m analysis.decompose [results-dir]
"""

import json
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent.parent


def linear_regression(xs: list, ys: list) -> dict:
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


def load_runs(results_dir: Path) -> list:
    runs = []
    for run_dir in sorted(results_dir.iterdir()):
        summary_file = run_dir / "summary.json"
        params_file = run_dir / "params.json"
        if not summary_file.exists() or not params_file.exists():
            continue
        with open(summary_file) as f:
            summary = json.load(f)
        with open(params_file) as f:
            params = json.load(f)
        runs.append({"run_dir": run_dir.name, "summary": summary, "params": params})
    return runs


def fmt(value, digits: int = 1) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def is_valid_run(run: dict) -> bool:
    summary = run["summary"]
    cv = summary.get("checkin_validation")
    if cv and not cv.get("all_ok", True):
        return False
    sv = summary.get("storage_validation")
    if sv and not sv.get("all_ok", True):
        return False
    return True


def print_idle_fits(runs: list) -> dict:
    by_approach = {}
    for run in runs:
        if not is_valid_run(run):
            continue
        summary = run["summary"]
        if summary.get("mode") != "idle":
            continue
        by_approach.setdefault(summary.get("approach", "?"), []).append(run)

    fits = {}
    if not by_approach:
        print("No idle runs found.\n")
        return fits

    print("Idle Fits")
    print("| Approach | Runs | Agent Counts | Agent Fixed MiB | Intercept MiB | R2 |")
    print("|----------|------|--------------|-----------------|---------------|----|")
    for approach in sorted(by_approach):
        approach_runs = sorted(
            by_approach[approach],
            key=lambda run: run["summary"].get("num_agents", 0),
        )
        xs = [run["summary"].get("num_agents", 0) for run in approach_runs]
        ys = [run["summary"].get("steady_state_mean_mib", 0) for run in approach_runs]
        fit = linear_regression(xs, ys)
        fits[approach] = fit
        counts = ",".join(str(x) for x in xs)
        slope = fit.get("slope")
        intercept = fit.get("intercept")
        r2 = fit.get("r2")
        slope_str = f"{slope:.1f}" if slope is not None else "-"
        intercept_str = f"{intercept:.1f}" if intercept is not None else "-"
        r2_str = f"{r2:.4f}" if r2 is not None else "-"
        print(
            f"| {approach} | {len(approach_runs)} | {counts} | "
            f"{slope_str} | {intercept_str} | {r2_str} |"
        )
    print()
    return fits


def print_plateau_runs(runs: list) -> None:
    plateau_runs = [
        run for run in runs
        if is_valid_run(run)
        and run["summary"].get("mode") == "plateau"
        and run["summary"].get("plateau")
    ]
    if not plateau_runs:
        print("No plateau runs found.\n")
        return

    print("Plateau Runs")
    print(
        "| Approach | Run | Agents | Worker MB | Schedule | Zero/Agent | "
        "First Worker | Steady Worker | R2 |"
    )
    print(
        "|----------|-----|--------|-----------|----------|------------|"
        "--------------|---------------|----|"
    )
    for run in sorted(
        plateau_runs,
        key=lambda item: (
            item["summary"].get("approach", ""),
            item["summary"].get("num_agents", 0),
            item["run_dir"],
        ),
    ):
        summary = run["summary"]
        params = run["params"]
        plateau = summary["plateau"]
        config = params.get("config", {})
        zero = plateau.get("zero_point_per_agent_mib")
        first = plateau.get("first_worker_tax_mib")
        steady = plateau.get("steady_worker_mib")
        r2 = plateau.get("worker_fit_r2")
        schedule = ",".join(str(v) for v in plateau.get("workers_per_agent", []))
        print(
            f"| {summary.get('approach', '?')} | {run['run_dir']} | "
            f"{summary.get('num_agents', 0)} | {config.get('worker_memory_mb', '-')} | "
            f"{schedule} | "
            f"{fmt(zero, 1)} | "
            f"{fmt(first, 1)} | "
            f"{fmt(steady, 1)} | "
            f"{fmt(r2, 4)} |"
        )
    print()

    paired = {}
    for run in plateau_runs:
        summary = run["summary"]
        params = run["params"]
        plateau = summary["plateau"]
        key = (
            summary.get("approach"),
            summary.get("num_agents"),
            tuple(plateau.get("workers_per_agent", [])),
        )
        paired.setdefault(key, {})[params.get("config", {}).get("worker_memory_mb")] = plateau

    split_rows = []
    for key, by_worker_mb in paired.items():
        if 0 not in by_worker_mb:
            continue
        runtime_tax = by_worker_mb[0].get("steady_worker_mib")
        for worker_mb, plateau in by_worker_mb.items():
            if worker_mb == 0:
                continue
            total_tax = plateau.get("steady_worker_mib")
            if runtime_tax is None or total_tax is None:
                continue
            split_rows.append((
                key[0],
                key[1],
                ",".join(str(v) for v in key[2]),
                worker_mb,
                runtime_tax,
                total_tax - runtime_tax,
                total_tax,
            ))

    if split_rows:
        print("Worker Split")
        print(
            "| Approach | Agents | Schedule | Payload MB | Runtime Tax | "
            "Payload Tax | Total Worker |"
        )
        print(
            "|----------|--------|----------|------------|-------------|"
            "-------------|--------------|"
        )
        for row in sorted(split_rows):
            print(
                f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | "
                f"{row[4]:.1f} | {row[5]:.1f} | {row[6]:.1f} |"
            )
        print()


def main():
    results_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else BENCH_DIR / "results"
    if not results_dir.exists():
        print(f"Results directory not found: {results_dir}")
        sys.exit(1)

    runs = load_runs(results_dir)
    if not runs:
        print("No benchmark results found.")
        sys.exit(1)

    print_idle_fits(runs)
    print_plateau_runs(runs)


if __name__ == "__main__":
    main()
