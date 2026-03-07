#!/usr/bin/env python3
"""
Unified benchmark CLI.

Provides a single entry point for both synthetic and real IronClaw benchmarks.

Usage:
    python3 bench.py list                              # List all approaches
    python3 bench.py list --suite ironclaw             # List ironclaw approaches only
    python3 bench.py synthetic --approach container-docker --agents 5
    python3 bench.py ironclaw --approach ironclaw-docker --agents 2
    python3 bench.py ironclaw-benchmark --approach ironclaw-sysbox-dind --agents 50 -- --mode loaded
    python3 bench.py ironclaw                          # All ironclaw approaches

The 'synthetic' subcommand runs the memory-density orchestrator (runner/orchestrate.py).
The 'ironclaw' subcommand runs the real-agent smoke tests (smoke_test.py).
The 'ironclaw-benchmark' subcommand runs host-driven real-agent benchmarks
with idle/loaded/plateau modes and injected jobs (ironclaw_benchmark.py).
"""

import subprocess
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR))

from approaches.base import discover_approaches


def cmd_list(args):
    """List available approaches."""
    approaches = discover_approaches(suite=args.suite)
    if not approaches:
        suite_msg = f" for suite '{args.suite}'" if args.suite else ""
        print(f"No approaches found{suite_msg}.")
        return

    by_suite = {}
    for name, approach in sorted(approaches.items()):
        by_suite.setdefault(approach.suite, []).append(name)

    for suite in sorted(by_suite):
        print(f"\n  {suite}:")
        for name in sorted(by_suite[suite]):
            print(f"    {name}")
    print()


def cmd_synthetic(args):
    """Run synthetic memory-density benchmark via runner/orchestrate.py."""
    cmd = [sys.executable, "-m", "runner.orchestrate"]
    cmd += ["--approach", args.approach]
    if args.agents is not None:
        cmd += ["--agents", str(args.agents)]
    if args.mode:
        cmd += ["--mode", args.mode]
    # Pass through extra args
    cmd += args.extra
    sys.exit(subprocess.call(cmd, cwd=str(BENCH_DIR)))


def cmd_ironclaw(args):
    """Run real IronClaw smoke tests via smoke_test.py."""
    cmd = [sys.executable, str(BENCH_DIR / "smoke_test.py")]
    if args.approach:
        cmd += ["--approach", args.approach]
    if args.agents is not None:
        cmd += ["--agents", str(args.agents)]
    # Pass through extra args
    cmd += args.extra
    sys.exit(subprocess.call(cmd, cwd=str(BENCH_DIR)))


def cmd_ironclaw_benchmark(args):
    """Run host-driven real IronClaw benchmark via ironclaw_benchmark.py."""
    cmd = [sys.executable, str(BENCH_DIR / "ironclaw_benchmark.py")]
    cmd += ["--approach", args.approach]
    if args.agents is not None:
        cmd += ["--agents", str(args.agents)]
    cmd += args.extra
    sys.exit(subprocess.call(cmd, cwd=str(BENCH_DIR)))


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Unified benchmark CLI for synthetic and IronClaw suites.",
    )
    sub = parser.add_subparsers(dest="command")

    # --- list ---
    p_list = sub.add_parser("list", help="List available approaches")
    p_list.add_argument(
        "--suite", choices=["synthetic", "ironclaw"],
        help="Filter by suite",
    )
    p_list.set_defaults(func=cmd_list)

    # --- synthetic ---
    p_synth = sub.add_parser(
        "synthetic",
        help="Run synthetic memory-density benchmark",
    )
    p_synth.add_argument("--approach", required=True,
                         help="Approach name (e.g., container-docker)")
    p_synth.add_argument("--agents", type=int, default=None,
                         help="Number of agents")
    p_synth.add_argument("--mode", choices=["loaded", "idle", "plateau"],
                         default="loaded", help="Benchmark mode")
    p_synth.add_argument("extra", nargs=argparse.REMAINDER,
                         help="Extra args passed to runner/orchestrate.py")
    p_synth.set_defaults(func=cmd_synthetic)

    # --- ironclaw ---
    p_ic = sub.add_parser(
        "ironclaw",
        help="Run real IronClaw smoke tests",
    )
    p_ic.add_argument("--approach", default=None,
                      help="Run only this approach (default: all ironclaw)")
    p_ic.add_argument("--agents", type=int, default=None,
                      help="Number of agents per approach")
    p_ic.add_argument("extra", nargs=argparse.REMAINDER,
                      help="Extra args passed to smoke_test.py")
    p_ic.set_defaults(func=cmd_ironclaw)

    # --- ironclaw-benchmark ---
    p_ic_bench = sub.add_parser(
        "ironclaw-benchmark",
        help="Run host-driven real IronClaw benchmark",
    )
    p_ic_bench.add_argument("--approach", required=True,
                            help="Run this ironclaw approach")
    p_ic_bench.add_argument("--agents", type=int, default=None,
                            help="Number of agents")
    p_ic_bench.add_argument("extra", nargs=argparse.REMAINDER,
                            help="Extra args passed to ironclaw_benchmark.py")
    p_ic_bench.set_defaults(func=cmd_ironclaw_benchmark)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
