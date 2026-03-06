#!/usr/bin/env python3
"""
Smoke test for real-ironclaw benchmark approaches.

Runs 2 IronClaw agents per approach, sends each a message that triggers
a shell tool call via the mock LLM, and verifies that each agent
successfully processed the message and executed the command.

Usage:
    python3 smoke_test.py                          # All ironclaw approaches
    python3 smoke_test.py --approach ironclaw-docker  # Single approach
    python3 smoke_test.py --list                      # List available approaches

Exit code:
    0  All tested approaches passed
    1  At least one approach failed
"""

import argparse
import importlib
import json
import subprocess
import sys
import time
import traceback
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR))

from approaches.base import Approach, BenchmarkConfig
from approaches._ironclaw_helpers import (
    GATEWAY_AUTH_TOKEN,
    trigger_worker_spawn,
    wait_for_gateway,
)

# Smoke test parameters
NUM_AGENTS = 2
COMMAND_WAIT_S = 15        # Time to wait for shell commands to execute
TOTAL_TIMEOUT_S = 300      # Max total time per approach

# ---------------------------------------------------------------------------
# Approach discovery (same as runner/orchestrate.py)
# ---------------------------------------------------------------------------

def discover_ironclaw_approaches():
    """Find all ironclaw-* approaches."""
    approaches = {}
    approaches_dir = BENCH_DIR / "approaches"
    for py_file in approaches_dir.glob("ironclaw_*.py"):
        if py_file.name.startswith("_"):
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
                    approaches[instance.name] = instance
        except Exception as e:
            print(f"Warning: could not load {py_file.name}: {e}")
    return approaches


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------

def check_tool_execution_in_logs(agent_index):
    """Check agent container logs for evidence of shell tool execution.

    Returns (executed: bool, details: str).
    """
    container_name = f"bench-ic-agent-{agent_index}"
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", "200", container_name],
            capture_output=True, text=True, timeout=10,
        )
        combined = result.stdout + result.stderr

        # Look for evidence that ironclaw processed a message and ran a tool.
        # Log patterns observed (with RUST_LOG=ironclaw=debug):
        #   "Received message from benchmark on gateway"
        #   "LLM call used 10 input + 20 output tokens"
        #   "Tool call started tool=shell"
        #   "Tool call completed tool=shell"
        #   "Sandbox initialized"  (sandbox container path)
        indicators = {
            "message_received": "Received message from" in combined,
            "llm_call": "LLM call used" in combined,
            "tool_started": "Tool call started" in combined and "shell" in combined,
            "tool_completed": "Tool call completed" in combined or "Tool call succeeded" in combined or "Tool call failed" in combined,
            "sandbox_init": "Sandbox initialized" in combined,
            "benchmark_ok": "benchmark-worker-ok" in combined,
        }

        # Pass if we see the LLM was called and the shell tool was invoked
        executed = indicators["llm_call"] and indicators["tool_started"]

        detail_parts = [f"{k}={'yes' if v else 'no'}" for k, v in indicators.items()]
        return executed, ", ".join(detail_parts)
    except (subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
        return False, f"log check failed: {e}"


# ---------------------------------------------------------------------------
# Smoke test runner
# ---------------------------------------------------------------------------

def run_smoke_test(approach, approach_name):
    """Run the smoke test for a single approach. Returns (passed, details)."""
    print(f"\n{'='*60}")
    print(f"SMOKE TEST: {approach_name}")
    print(f"{'='*60}\n")

    config = BenchmarkConfig(
        benchmark_mode="loaded",
        agent_memory_mb=2048,
        agent_baseline_mb=0,
        max_concurrent_workers=2,
        spawn_interval_mean_s=10,
        benchmark_duration_s=60,
        worker_memory_mb=50,
        worker_duration_min_s=30,
        worker_duration_max_s=30,
        worker_lifetime_mode="timed",
        orchestrator_base_port=51000,
    )

    details = {"approach": approach_name, "agents": NUM_AGENTS}
    start_time = time.monotonic()

    try:
        # 1. Setup
        print(f"[smoke] Setting up {approach_name}...")
        approach.setup(config)

        # 2. Start agents
        print(f"[smoke] Starting {NUM_AGENTS} agents...")
        agent_ids = approach.start_agents(NUM_AGENTS, config)
        details["agent_ids"] = agent_ids

        if len(agent_ids) < NUM_AGENTS:
            details["error"] = f"Only {len(agent_ids)}/{NUM_AGENTS} agents started"
            return False, details

        # 3. Verify health
        print("[smoke] Verifying agent health...")
        for agent_id in agent_ids:
            port = approach._host_ports.get(agent_id)
            if port and not wait_for_gateway(port, timeout_s=60, label=agent_id):
                details["error"] = f"{agent_id} failed health check"
                return False, details

        print("[smoke] All agents healthy.")

        # 4. Send messages to trigger shell tool calls
        print("[smoke] Sending messages to trigger shell commands...")
        send_results = {}
        for agent_id in agent_ids:
            port = approach._host_ports.get(agent_id)
            if port:
                ok = trigger_worker_spawn(port)
                send_results[agent_id] = ok
                if ok:
                    print(f"[smoke]   {agent_id}: message accepted")
                else:
                    print(f"[smoke]   {agent_id}: message FAILED")

        if not all(send_results.values()):
            failed = [a for a, ok in send_results.items() if not ok]
            details["error"] = f"Failed to send messages to: {failed}"
            return False, details

        # 5. Wait for command execution
        print(f"[smoke] Waiting {COMMAND_WAIT_S}s for shell commands to execute...")
        time.sleep(COMMAND_WAIT_S)

        # 6. Verify execution via logs
        print("[smoke] Checking agent logs for tool execution...")
        agents_executed = 0
        for i, agent_id in enumerate(agent_ids):
            executed, log_detail = check_tool_execution_in_logs(i)
            details[f"{agent_id}_log"] = log_detail
            if executed:
                agents_executed += 1
                print(f"[smoke]   {agent_id}: EXECUTED ({log_detail})")
            else:
                print(f"[smoke]   {agent_id}: NOT EXECUTED ({log_detail})")

        details["agents_executed"] = agents_executed

        if agents_executed >= NUM_AGENTS:
            print(f"[smoke] PASS: {agents_executed}/{NUM_AGENTS} agents "
                  "executed shell commands")
            details["passed"] = True
            return True, details
        else:
            details["error"] = (
                f"Only {agents_executed}/{NUM_AGENTS} agents executed commands"
            )
            return False, details

    except Exception as e:
        details["error"] = str(e)
        details["traceback"] = traceback.format_exc()
        print(f"[smoke] ERROR: {e}")
        return False, details

    finally:
        elapsed = time.monotonic() - start_time
        details["elapsed_s"] = round(elapsed, 1)

        # Always clean up
        print(f"[smoke] Cleaning up {approach_name}...")
        try:
            approach.cleanup()
        except Exception as cleanup_err:
            print(f"[smoke] Cleanup error: {cleanup_err}")
        print(f"[smoke] {approach_name} done in {elapsed:.1f}s\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Ironclaw benchmark smoke tests")
    parser.add_argument("--approach", help="Run only this approach")
    parser.add_argument("--list", action="store_true", help="List available approaches")
    parser.add_argument("--output", default="smoke-results.json",
                        help="Output file for results")
    args = parser.parse_args()

    approaches = discover_ironclaw_approaches()

    if args.list:
        for name in sorted(approaches):
            print(f"  {name}")
        return

    if not approaches:
        print("No ironclaw approaches found. Check approaches/ directory.")
        sys.exit(1)

    # Filter to requested approach
    if args.approach:
        if args.approach not in approaches:
            print(f"Unknown approach '{args.approach}'. Available:")
            for name in sorted(approaches):
                print(f"  {name}")
            sys.exit(1)
        approaches = {args.approach: approaches[args.approach]}

    # Run tests
    results = {}
    all_passed = True

    for name, approach in sorted(approaches.items()):
        passed, details = run_smoke_test(approach, name)
        results[name] = details
        if not passed:
            all_passed = False
            print(f"  FAIL: {name} - {details.get('error', 'unknown')}")
        else:
            print(f"  PASS: {name}")

    # Summary
    print(f"\n{'='*60}")
    print("SMOKE TEST SUMMARY")
    print(f"{'='*60}")
    total = len(results)
    passed_count = sum(1 for d in results.values() if d.get("passed"))
    print(f"  {passed_count}/{total} approaches passed")
    for name, details in sorted(results.items()):
        status = "PASS" if details.get("passed") else "FAIL"
        elapsed = details.get("elapsed_s", "?")
        print(f"  [{status}] {name} ({elapsed}s)")
        if not details.get("passed") and "error" in details:
            print(f"         {details['error']}")

    # Write results
    output_path = BENCH_DIR / args.output
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults written to {output_path}")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
