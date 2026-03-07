#!/usr/bin/env python3
"""
Benchmark runner for real IronClaw approaches.

This is distinct from smoke_test.py:
  - smoke_test.py answers "does it work?"
  - this script answers "how does it behave under controlled load?"

Current focus is staggered one-shot spawning, which is useful for validating
startup fanout separately from steady-state memory.
"""

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR))

from approaches.base import BenchmarkConfig, discover_approaches
from approaches._ironclaw_helpers import trigger_worker_spawn
from runner.collect import Collector


def percentile(values, p):
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    values = sorted(values)
    idx = (len(values) - 1) * p
    lo = int(idx)
    hi = min(lo + 1, len(values) - 1)
    frac = idx - lo
    return float(values[lo] + (values[hi] - values[lo]) * frac)


def summarize_timeseries(run_dir: Path, target_workers: int, num_agents: int) -> dict:
    samples = []
    with open(run_dir / "timeseries.jsonl") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            samples.append(json.loads(line))

    host_consumed_kb = [s["host_consumed_kb"] for s in samples]
    full_load = [s for s in samples if s.get("active_workers", 0) == target_workers]
    baseline = [s for s in samples if s.get("active_workers", 0) == 0]
    if not baseline and full_load:
        first_full_t = full_load[0]["timestamp_s"]
        baseline = [s for s in samples if s["timestamp_s"] < first_full_t]
    full_load_consumed = [s["host_consumed_kb"] for s in full_load]

    result = {
        "samples": len(samples),
        "max_active_workers": max((s.get("active_workers", 0) for s in samples), default=0),
        "peak_host_mib": max(host_consumed_kb, default=0) / 1024,
        "mean_host_mib": statistics.mean(host_consumed_kb) / 1024 if host_consumed_kb else 0.0,
        "p95_host_mib": percentile(host_consumed_kb, 0.95) / 1024 if host_consumed_kb else 0.0,
        "baseline_samples": len(baseline),
        "full_load_samples": len(full_load),
    }

    def mean_entity_pss_mib(group):
        vals = []
        for sample in group:
            vals.extend(
                entity_data["pss_kb"]
                for entity_data in sample.get("entities", {}).values()
                if "pss_kb" in entity_data
            )
        return statistics.mean(vals) / 1024 if vals else 0.0

    if baseline:
        baseline_consumed = [s["host_consumed_kb"] for s in baseline]
        result.update({
            "baseline_mean_host_mib": statistics.mean(baseline_consumed) / 1024,
            "baseline_p95_host_mib": percentile(baseline_consumed, 0.95) / 1024,
            "baseline_entity_pss_mib_mean": mean_entity_pss_mib(baseline),
        })

    if full_load_consumed:
        result.update({
            "full_load_mean_host_mib": statistics.mean(full_load_consumed) / 1024,
            "full_load_p95_host_mib": percentile(full_load_consumed, 0.95) / 1024,
            "full_load_peak_host_mib": max(full_load_consumed) / 1024,
            "full_load_agent_plus_workers_per_agent_mib":
                (statistics.mean(full_load_consumed) / 1024) / num_agents,
        })

        entity_pss_kb = []
        daemon_pss = {}
        for sample in full_load:
            for entity_data in sample.get("entities", {}).values():
                if "pss_kb" in entity_data:
                    entity_pss_kb.append(entity_data["pss_kb"])
            for daemon_name, daemon_data in sample.get("daemons", {}).items():
                if "pss_kb" in daemon_data:
                    daemon_pss.setdefault(daemon_name, []).append(daemon_data["pss_kb"])

        if entity_pss_kb:
            result["full_load_entity_pss_mib_mean"] = statistics.mean(entity_pss_kb) / 1024
        if daemon_pss:
            result["full_load_daemon_pss_mib_mean"] = {
                name: statistics.mean(values) / 1024
                for name, values in daemon_pss.items()
            }
        if baseline:
            result["full_load_delta_host_mib"] = (
                result["full_load_mean_host_mib"] - result["baseline_mean_host_mib"]
            )
            result["full_load_delta_per_agent_mib"] = result["full_load_delta_host_mib"] / num_agents
            result["full_load_delta_entity_pss_mib_mean"] = (
                result["full_load_entity_pss_mib_mean"] - result["baseline_entity_pss_mib_mean"]
            )

    return result


def main():
    parser = argparse.ArgumentParser(description="Run a real IronClaw benchmark.")
    parser.add_argument("--approach", required=True, help="IronClaw approach name")
    parser.add_argument("--agents", type=int, required=True, help="Number of agents")
    parser.add_argument("--batch-size", type=int, default=10,
                        help="Agents to trigger per batch")
    parser.add_argument("--batch-interval-s", type=float, default=5.0,
                        help="Delay between trigger batches")
    parser.add_argument("--stability-window-s", type=float, default=30.0,
                        help="Require target worker count for this long")
    parser.add_argument("--poll-interval-s", type=float, default=5.0,
                        help="Polling interval for active worker count")
    parser.add_argument("--active-timeout-s", type=float, default=240.0,
                        help="Max time to wait for target active workers")
    parser.add_argument("--pre-trigger-settle-s", type=float, default=10.0,
                        help="Collect baseline samples before triggering workers")
    parser.add_argument("--sample-interval-ms", type=int, default=1000,
                        help="Collector sampling interval")
    parser.add_argument("--agent-memory-mb", type=int, default=2048,
                        help="Outer agent memory limit in MB")
    parser.add_argument("--orchestrator-base-port", type=int, default=56000,
                        help="Base host port for agent gateways")
    parser.add_argument("--worker-command", default="",
                        help="Override MOCK_WORKER_COMMAND inside agents")
    parser.add_argument("--output-dir", default="",
                        help="Optional explicit results directory")
    args = parser.parse_args()

    approaches = discover_approaches(suite="ironclaw")
    if args.approach not in approaches:
        print(f"Unknown approach '{args.approach}'. Available:")
        for name in sorted(approaches):
            print(f"  {name}")
        sys.exit(1)

    if args.worker_command:
        os.environ["MOCK_WORKER_COMMAND"] = args.worker_command

    approach = approaches[args.approach]
    run_label = (
        Path(args.output_dir).name if args.output_dir else
        f"{args.approach}-staggered-n{args.agents}-"
        f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
    )
    run_dir = (
        Path(args.output_dir) if args.output_dir else
        (BENCH_DIR / "results" / run_label)
    )
    run_dir.mkdir(parents=True, exist_ok=True)

    config = BenchmarkConfig(
        benchmark_mode="loaded",
        agent_memory_mb=args.agent_memory_mb,
        orchestrator_base_port=args.orchestrator_base_port,
    )

    summary = {
        "run_label": run_label,
        "run_dir": str(run_dir),
        "approach": args.approach,
        "agents": args.agents,
        "batch_size": args.batch_size,
        "batch_interval_s": args.batch_interval_s,
        "stability_window_s": args.stability_window_s,
        "poll_interval_s": args.poll_interval_s,
        "active_timeout_s": args.active_timeout_s,
        "pre_trigger_settle_s": args.pre_trigger_settle_s,
        "worker_command": os.environ.get("MOCK_WORKER_COMMAND", ""),
    }

    start_ts = time.time()
    collector = None
    collector_thread = None
    ts_file = None
    agent_ids = []
    cleanup_error = None
    run_error = None

    try:
        print(f"[ironclaw-benchmark] setup {args.approach}", flush=True)
        approach.setup(config)
        print(f"[ironclaw-benchmark] starting {args.agents} agents", flush=True)
        agent_ids = approach.start_agents(args.agents, config)
        summary["agent_ids"] = agent_ids
        summary["agent_start_elapsed_s"] = round(time.time() - start_ts, 1)

        ts_file = open(run_dir / "timeseries.jsonl", "w")
        collector = Collector(interval_ms=args.sample_interval_ms, phase="running")
        collector_thread = collector.run_in_thread(
            output=ts_file,
            get_agent_pids=approach.get_agent_pids,
            get_daemon_pids=approach.get_daemon_pids,
            count_workers=approach.count_active_workers,
        )

        if args.pre_trigger_settle_s > 0:
            print(
                f"[ironclaw-benchmark] collecting baseline for "
                f"{args.pre_trigger_settle_s:.1f}s",
                flush=True,
            )
            time.sleep(args.pre_trigger_settle_s)

        send_results = {}
        batch_times = []
        for batch_idx, start in enumerate(range(0, len(agent_ids), args.batch_size)):
            batch = agent_ids[start : start + args.batch_size]
            batch_times.append({
                "batch": batch_idx,
                "agents": batch,
                "t_s": round(time.time() - start_ts, 1),
            })
            print(
                f"[ironclaw-benchmark] triggering batch {batch_idx} "
                f"agents {start}-{start + len(batch) - 1}",
                flush=True,
            )
            for agent_id in batch:
                ok = trigger_worker_spawn(approach._host_ports[agent_id])
                send_results[agent_id] = ok
            if start + args.batch_size < len(agent_ids):
                time.sleep(args.batch_interval_s)
        summary["send_results"] = send_results
        summary["trigger_ok"] = sum(1 for ok in send_results.values() if ok)
        summary["batch_times"] = batch_times

        samples = []
        stable_samples_needed = max(1, int(round(args.stability_window_s / args.poll_interval_s)))
        stable_target_samples = 0
        deadline = time.time() + args.active_timeout_s
        while time.time() < deadline:
            active = approach.count_active_workers()
            sample = {
                "t_s": round(time.time() - start_ts, 1),
                "active_workers": active,
            }
            samples.append(sample)
            print(f"[ironclaw-benchmark] sample t={sample['t_s']} active={active}", flush=True)
            if active == args.agents:
                stable_target_samples += 1
            else:
                stable_target_samples = 0
            if stable_target_samples >= stable_samples_needed:
                break
            time.sleep(args.poll_interval_s)

        summary["samples"] = samples
        summary["stable_target_for_window"] = stable_target_samples >= stable_samples_needed

        per_agent_worker_counts = {}
        import subprocess
        for i, agent_id in enumerate(agent_ids):
            container_name = f"bench-ic-agent-{i}"
            result = subprocess.run(
                ["docker", "exec", container_name, "docker", "ps", "-q", "--filter", "name=sandbox-"],
                capture_output=True, text=True, timeout=10,
            )
            count = 0
            if result.returncode == 0 and result.stdout.strip():
                count = len([line for line in result.stdout.splitlines() if line.strip()])
            per_agent_worker_counts[agent_id] = count
        summary["per_agent_worker_counts"] = per_agent_worker_counts
        summary["agents_with_one_worker"] = sum(1 for v in per_agent_worker_counts.values() if v == 1)
        summary["agents_with_zero_workers"] = sum(1 for v in per_agent_worker_counts.values() if v == 0)

        summary["elapsed_s"] = round(time.time() - start_ts, 1)

    except Exception as exc:
        summary["error"] = str(exc)
        run_error = exc

    finally:
        if collector is not None:
            collector.stop()
        if collector_thread is not None:
            collector_thread.join(timeout=60)
        if ts_file is not None and (collector_thread is None or not collector_thread.is_alive()):
            try:
                ts_file.close()
            except Exception:
                pass
        elif collector_thread is not None and collector_thread.is_alive():
            print("[ironclaw-benchmark] WARNING: collector thread still alive at shutdown", flush=True)

    if (run_dir / "timeseries.jsonl").exists():
        summary["memory_summary"] = summarize_timeseries(run_dir, args.agents, args.agents)

    with open(run_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps({
        "run_dir": str(run_dir),
        "approach": args.approach,
        "trigger_ok": summary.get("trigger_ok", 0),
        "stable_target_for_window": summary.get("stable_target_for_window", False),
        "agents_with_one_worker": summary.get("agents_with_one_worker", 0),
        "agents_with_zero_workers": summary.get("agents_with_zero_workers", 0),
        "memory_summary": summary.get("memory_summary", {}),
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
