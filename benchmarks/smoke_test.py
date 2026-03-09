#!/usr/bin/env python3
"""
Smoke test for real IronClaw benchmark approaches.

Runs a short host-driven benchmark for each approach and verifies that the
host-visible evidence proves the full worker lifecycle:
  - agent started
  - agent bootstrap storage write
  - native agent workspace write
  - job created
  - worker started
  - worker storage write logged
  - proof file verified
  - worker callback received
  - worker cleanup logged and host absence verified
  - agent exited and cleanup verified
"""

import argparse
import json
import subprocess
import sys
import time
import traceback
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR))

from approaches.base import discover_approaches


DEFAULT_AGENTS = 1
DEFAULT_TIMEOUT_S = 1800
DEFAULT_PORT_BASE = 52000
BENCHMARK_DURATION_S = 25
JOB_DURATION_S = 3


def build_benchmark_command(approach_name: str, num_agents: int, port_base: int, run_dir: Path) -> list[str]:
    return [
        sys.executable,
        str(BENCH_DIR / "ironclaw_benchmark.py"),
        "--approach",
        approach_name,
        "--agents",
        str(num_agents),
        "--mode",
        "loaded",
        "--benchmark-duration-s",
        str(BENCHMARK_DURATION_S),
        "--spawn-interval-mean-s",
        "0.1",
        "--max-concurrent-workers",
        "1",
        "--max-triggers-per-agent",
        "1",
        "--pre-trigger-settle-s",
        "1",
        "--control-interval-s",
        "0.5",
        "--sample-interval-ms",
        "500",
        "--job-dispatch",
        "worker-job",
        "--job-duration-min-s",
        str(JOB_DURATION_S),
        "--job-duration-max-s",
        str(JOB_DURATION_S),
        "--agent-memory-mb",
        "2048",
        "--orchestrator-base-port",
        str(port_base),
        "--output-dir",
        str(run_dir),
    ]


def load_summary(run_dir: Path) -> dict:
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        raise FileNotFoundError(f"summary.json not found in {run_dir}")
    return json.loads(summary_path.read_text())


def require_equal(failures: list[str], summary: dict, field: str, expected: int) -> None:
    actual = summary.get(field)
    if actual != expected:
        failures.append(f"{field}: expected {expected}, got {actual}")


def require_true(failures: list[str], record_id: str, field: str, value) -> None:
    if value is not True:
        failures.append(f"{record_id}: expected {field}=true, got {value!r}")


def require_present(failures: list[str], record_id: str, field: str, value) -> None:
    if value in (None, "", []):
        failures.append(f"{record_id}: missing {field}")


def verify_summary(summary: dict, expected_agents: int, expected_jobs: int) -> list[str]:
    failures: list[str] = []

    if summary.get("error"):
        failures.append(f"benchmark error: {summary['error']}")
    if summary.get("cleanup_error"):
        failures.append(f"cleanup error: {summary['cleanup_error']}")

    control_errors = summary.get("control_errors") or {}
    during_errors = control_errors.get("during_run") or {}
    final_errors = control_errors.get("final") or {}
    if during_errors:
        failures.append(f"control_errors.during_run: {during_errors}")
    if final_errors:
        failures.append(f"control_errors.final: {final_errors}")

    require_equal(failures, summary, "agents_started", expected_agents)
    require_equal(failures, summary, "agents_with_storage", expected_agents)
    require_equal(failures, summary, "agents_with_workspace_write", expected_agents)
    require_equal(failures, summary, "workers_spawned", expected_jobs)
    require_equal(failures, summary, "jobs_discovered", expected_jobs)
    require_equal(failures, summary, "jobs_started", expected_jobs)
    require_equal(failures, summary, "jobs_with_storage_event", expected_jobs)
    require_equal(failures, summary, "jobs_with_callback_event", expected_jobs)
    require_equal(failures, summary, "jobs_with_proof", expected_jobs)
    require_equal(failures, summary, "jobs_cleaned", expected_jobs)
    require_equal(failures, summary, "jobs_cleanup_verified", expected_jobs)
    require_equal(failures, summary, "jobs_completed", expected_jobs)
    require_equal(failures, summary, "jobs_succeeded", expected_jobs)
    require_equal(failures, summary, "jobs_failed", 0)
    require_equal(failures, summary, "agents_exited_logged", expected_agents)
    require_equal(failures, summary, "agents_cleanup_verified", expected_agents)

    agent_records = summary.get("agent_records") or []
    if len(agent_records) != expected_agents:
        failures.append(
            f"agent_records: expected {expected_agents}, got {len(agent_records)}"
        )
    for record in agent_records:
        agent_id = record.get("agent_id") or "<unknown-agent>"
        require_true(failures, agent_id, "started_logged", record.get("started_logged"))
        require_true(failures, agent_id, "storage_logged", record.get("storage_logged"))
        require_true(failures, agent_id, "storage_verified", record.get("storage_verified"))
        require_true(
            failures,
            agent_id,
            "workspace_write_logged",
            record.get("workspace_write_logged"),
        )
        require_true(
            failures,
            agent_id,
            "workspace_write_verified",
            record.get("workspace_write_verified"),
        )
        require_true(failures, agent_id, "exited_logged", record.get("exited_logged"))
        require_true(failures, agent_id, "absent_verified", record.get("absent_verified"))
        require_present(failures, agent_id, "storage_path", record.get("storage_path"))
        require_present(
            failures,
            agent_id,
            "workspace_write_path",
            record.get("workspace_write_path"),
        )

    job_records = summary.get("job_records") or []
    if len(job_records) != expected_jobs:
        failures.append(f"job_records: expected {expected_jobs}, got {len(job_records)}")
    for record in job_records:
        job_id = record.get("job_id") or "<unknown-job>"
        require_present(failures, job_id, "project_dir", record.get("project_dir"))
        require_present(failures, job_id, "job_created_at_epoch_s", record.get("job_created_at_epoch_s"))
        require_present(
            failures,
            job_id,
            "worker_started_at_epoch_s",
            record.get("worker_started_at_epoch_s"),
        )
        require_true(
            failures,
            job_id,
            "worker_storage_logged",
            record.get("worker_storage_logged"),
        )
        require_present(
            failures,
            job_id,
            "worker_storage_at_epoch_s",
            record.get("worker_storage_at_epoch_s"),
        )
        require_true(failures, job_id, "proof_verified", record.get("proof_verified"))
        require_present(
            failures,
            job_id,
            "proof_verified_at_epoch_s",
            record.get("proof_verified_at_epoch_s"),
        )
        require_present(
            failures,
            job_id,
            "callback_at_epoch_s",
            record.get("callback_at_epoch_s"),
        )
        require_true(failures, job_id, "result_success", record.get("result_success"))
        require_present(
            failures,
            job_id,
            "worker_cleaned_at_epoch_s",
            record.get("worker_cleaned_at_epoch_s"),
        )
        require_true(
            failures,
            job_id,
            "worker_absent_verified",
            record.get("worker_absent_verified"),
        )

    return failures


def run_smoke_test(approach_name: str, num_agents: int, port_base: int, timeout_s: int) -> tuple[bool, dict]:
    timestamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    run_dir = (BENCH_DIR / "results" / f"smoke-{approach_name}-{timestamp}").resolve()
    cmd = build_benchmark_command(approach_name, num_agents, port_base, run_dir)

    print(f"\n{'=' * 72}")
    print(f"SMOKE TEST: {approach_name} (agents={num_agents})")
    print(f"{'=' * 72}\n")
    print(f"[smoke] Running: {' '.join(cmd)}")

    details = {
        "approach": approach_name,
        "agents": num_agents,
        "run_dir": str(run_dir),
        "command": cmd,
    }
    start = time.monotonic()

    try:
        result = subprocess.run(
            cmd,
            cwd=BENCH_DIR,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        details["returncode"] = result.returncode

        run_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = run_dir / "smoke.stdout.log"
        stderr_path = run_dir / "smoke.stderr.log"
        stdout_path.write_text(result.stdout)
        stderr_path.write_text(result.stderr)
        details["stdout_path"] = str(stdout_path)
        details["stderr_path"] = str(stderr_path)

        summary = load_summary(run_dir)
        details["summary_path"] = str(run_dir / "summary.json")
        details["summary_counts"] = {
            "agents_started": summary.get("agents_started"),
            "agents_with_storage": summary.get("agents_with_storage"),
            "agents_with_workspace_write": summary.get("agents_with_workspace_write"),
            "workers_spawned": summary.get("workers_spawned"),
            "jobs_discovered": summary.get("jobs_discovered"),
            "jobs_with_storage_event": summary.get("jobs_with_storage_event"),
            "jobs_with_proof": summary.get("jobs_with_proof"),
            "jobs_with_callback_event": summary.get("jobs_with_callback_event"),
            "jobs_cleaned": summary.get("jobs_cleaned"),
            "jobs_cleanup_verified": summary.get("jobs_cleanup_verified"),
            "agents_exited_logged": summary.get("agents_exited_logged"),
            "agents_cleanup_verified": summary.get("agents_cleanup_verified"),
        }

        failures = []
        if result.returncode != 0:
            failures.append(f"benchmark exited with status {result.returncode}")
        failures.extend(verify_summary(summary, expected_agents=num_agents, expected_jobs=num_agents))

        elapsed = time.monotonic() - start
        details["elapsed_s"] = round(elapsed, 1)
        if failures:
            details["failures"] = failures
            print(f"[smoke] FAIL: {approach_name}")
            for failure in failures:
                print(f"[smoke]   - {failure}")
            return False, details

        details["passed"] = True
        print(
            "[smoke] PASS: "
            f"agents_started={summary.get('agents_started')}/{num_agents}, "
            f"jobs={summary.get('jobs_succeeded')}/{num_agents}, "
            f"workspace_writes={summary.get('agents_with_workspace_write')}/{num_agents}"
        )
        return True, details

    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - start
        details["elapsed_s"] = round(elapsed, 1)
        details["error"] = f"timed out after {timeout_s}s"
        details["stdout"] = exc.stdout
        details["stderr"] = exc.stderr
        print(f"[smoke] FAIL: {approach_name} timed out after {timeout_s}s")
        return False, details
    except Exception as exc:
        elapsed = time.monotonic() - start
        details["elapsed_s"] = round(elapsed, 1)
        details["error"] = str(exc)
        details["traceback"] = traceback.format_exc()
        print(f"[smoke] FAIL: {approach_name}: {exc}")
        return False, details


def main() -> None:
    parser = argparse.ArgumentParser(description="Full-lifecycle smoke tests for real IronClaw benchmarks")
    parser.add_argument("--approach", help="Run only this approach")
    parser.add_argument("--list", action="store_true", help="List available approaches")
    parser.add_argument(
        "--agents",
        type=int,
        default=DEFAULT_AGENTS,
        help=f"Number of agents to run per approach (default: {DEFAULT_AGENTS})",
    )
    parser.add_argument(
        "--timeout-s",
        type=int,
        default=DEFAULT_TIMEOUT_S,
        help=f"Per-approach timeout in seconds (default: {DEFAULT_TIMEOUT_S})",
    )
    parser.add_argument(
        "--port-base",
        type=int,
        default=DEFAULT_PORT_BASE,
        help=f"Base host port for the first approach (default: {DEFAULT_PORT_BASE})",
    )
    parser.add_argument(
        "--output",
        default="smoke-results.json",
        help="Output file for aggregate results",
    )
    args = parser.parse_args()

    approaches = discover_approaches(suite="ironclaw")
    if args.list:
        for name in sorted(approaches):
            print(f"  {name}")
        return

    if args.approach:
        if args.approach not in approaches:
            print(f"Unknown approach '{args.approach}'. Available:")
            for name in sorted(approaches):
                print(f"  {name}")
            sys.exit(1)
        approaches = {args.approach: approaches[args.approach]}

    if not approaches:
        print("No ironclaw approaches found.")
        sys.exit(1)

    results = {}
    all_passed = True

    for index, name in enumerate(sorted(approaches)):
        port_base = args.port_base + (index * 100)
        passed, details = run_smoke_test(name, args.agents, port_base, args.timeout_s)
        results[name] = details
        if not passed:
            all_passed = False

    print(f"\n{'=' * 72}")
    print("SMOKE TEST SUMMARY")
    print(f"{'=' * 72}")
    total = len(results)
    passed_count = sum(1 for details in results.values() if details.get("passed"))
    print(f"  {passed_count}/{total} approaches passed")
    for name, details in sorted(results.items()):
        status = "PASS" if details.get("passed") else "FAIL"
        elapsed = details.get("elapsed_s", "?")
        print(f"  [{status}] {name} ({elapsed}s)")
        for failure in details.get("failures", []):
            print(f"         {failure}")
        if details.get("error"):
            print(f"         {details['error']}")

    output_path = BENCH_DIR / args.output
    output_path.write_text(json.dumps(results, indent=2))
    print(f"\nResults written to {output_path}")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
