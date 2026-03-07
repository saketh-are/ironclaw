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
    """Check agent container logs for evidence of sandboxed shell tool execution.

    Parses the structured tool-result JSON from ironclaw's debug logs to verify
    the shell command actually ran inside a sandbox container and exited cleanly.

    Returns (executed: bool, details: str).
    """
    container_name = f"bench-ic-agent-{agent_index}"
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", "200", container_name],
            capture_output=True, text=True, timeout=10,
        )
        combined = result.stdout + result.stderr

        indicators = {
            "message_received": "Received message from" in combined,
            "llm_call": "LLM call used" in combined,
            "tool_started": "Tool call started" in combined and "shell" in combined,
            "sandbox_init": "Sandbox initialized" in combined,
            "tool_succeeded": False,
            "sandboxed": False,
            "exit_code_0": False,
        }

        # Parse the structured tool result from the "Tool call succeeded" log line.
        # Format: Tool call succeeded tool=shell elapsed_ms=220 result={"exit_code":0,...}
        # Note: logs contain ANSI escape codes, so strip them first.
        import re
        ansi_re = re.compile(r'\x1b\[[0-9;]*m')
        combined = ansi_re.sub('', combined)
        for line in combined.split("\n"):
            m = re.search(r'Tool call succeeded tool=shell.*result=(\{.*\})', line)
            if m:
                indicators["tool_succeeded"] = True
                try:
                    result_json = json.loads(m.group(1))
                    indicators["sandboxed"] = result_json.get("sandboxed") is True
                    indicators["exit_code_0"] = result_json.get("exit_code") == 0
                except (json.JSONDecodeError, AttributeError):
                    pass

        # Pass requires: LLM called, tool dispatched, tool succeeded in sandbox
        # with exit code 0.
        executed = (
            indicators["tool_succeeded"]
            and indicators["sandboxed"]
            and indicators["exit_code_0"]
        )

        detail_parts = [f"{k}={'yes' if v else 'no'}" for k, v in indicators.items()]
        return executed, ", ".join(detail_parts)
    except (subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
        return False, f"log check failed: {e}"


def check_tool_execution_via_api(port, auth_token=GATEWAY_AUTH_TOKEN):
    """Check tool execution via the gateway's chat history API.

    Used for VM-based approaches where docker logs aren't available.
    Queries /api/chat/history and checks for a successful shell tool call.

    Returns (executed: bool, details: str).
    """
    import urllib.request
    url = f"http://127.0.0.1:{port}/api/chat/history"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {auth_token}"}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
    except Exception as e:
        return False, f"API query failed: {e}"

    turns = data.get("turns", [])
    for turn in turns:
        for tc in turn.get("tool_calls", []):
            if tc.get("name") == "shell" and tc.get("has_result") and not tc.get("has_error"):
                # Parse the result preview
                try:
                    result_json = json.loads(tc.get("result_preview", "{}"))
                    sandboxed = result_json.get("sandboxed") is True
                    exit_ok = result_json.get("exit_code") == 0
                    if sandboxed and exit_ok:
                        return True, f"sandboxed=yes, exit_code=0"
                    return False, f"sandboxed={sandboxed}, exit_code={result_json.get('exit_code')}"
                except (json.JSONDecodeError, AttributeError):
                    return False, "result parse error"
    return False, "no shell tool call found in chat history"


def check_storage_write(agent_index, host_workspace_dir=None):
    """Verify that the sandbox container wrote a proof file to the workspace.

    The mock LLM command writes to /workspace/bench-test/output.txt inside the
    sandbox container.  Because /workspace is bind-mounted from the agent's
    workspace directory, we can read the proof file either:
      - directly on the host (if host_workspace_dir is given), or
      - via `docker exec` on the agent container.

    Returns (written: bool, content: str).
    """
    proof_relpath = "bench-test/output.txt"

    # If we know the host workspace path, read directly (shared-daemon case)
    if host_workspace_dir is not None:
        proof_path = Path(host_workspace_dir) / proof_relpath
        try:
            if proof_path.exists():
                content = proof_path.read_text().strip()
                if content.startswith("proof-"):
                    return True, content
                return False, f"content={content!r}"
            return False, f"file not found: {proof_path}"
        except Exception as e:
            return False, f"read failed: {e}"

    # Fallback: read via docker exec (DinD case — workspace inside container)
    container_name = f"bench-ic-agent-{agent_index}"
    try:
        result = subprocess.run(
            ["docker", "exec", container_name,
             "cat", f"/tmp/workspace/{proof_relpath}"],
            capture_output=True, text=True, timeout=5,
        )
        content = result.stdout.strip()
        if result.returncode == 0 and content.startswith("proof-"):
            return True, content
        return False, f"rc={result.returncode} content={content!r} stderr={result.stderr.strip()!r}"
    except (subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
        return False, f"exec failed: {e}"


# ---------------------------------------------------------------------------
# Smoke test runner
# ---------------------------------------------------------------------------

def run_smoke_test(approach, approach_name, num_agents=NUM_AGENTS):
    """Run the smoke test for a single approach. Returns (passed, details)."""
    print(f"\n{'='*60}")
    print(f"SMOKE TEST: {approach_name}  (N={num_agents})")
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

    details = {"approach": approach_name, "agents": num_agents}
    start_time = time.monotonic()

    try:
        # 1. Setup
        print(f"[smoke] Setting up {approach_name}...")
        approach.setup(config)

        # 2. Start agents
        print(f"[smoke] Starting {num_agents} agents...")
        agent_ids = approach.start_agents(num_agents, config)
        details["agent_ids"] = agent_ids

        if len(agent_ids) < num_agents:
            details["error"] = f"Only {len(agent_ids)}/{num_agents} agents started"
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

        # 6. Verify execution via logs (or API for VM approaches)
        use_api = hasattr(approach, '_config') and not hasattr(approach, '_agent_ids')
        # Heuristic: VM approaches don't use docker containers
        use_api = approach.name.startswith("ironclaw-vm")
        print(f"[smoke] Checking tool execution ({'API' if use_api else 'logs'})...")
        agents_executed = 0
        for i, agent_id in enumerate(agent_ids):
            if use_api:
                port = approach._host_ports.get(agent_id)
                executed, log_detail = check_tool_execution_via_api(port) if port else (False, "no port")
            else:
                executed, log_detail = check_tool_execution_in_logs(i)
            details[f"{agent_id}_log"] = log_detail
            if executed:
                agents_executed += 1
                print(f"[smoke]   {agent_id}: EXECUTED ({log_detail})")
            else:
                print(f"[smoke]   {agent_id}: NOT EXECUTED ({log_detail})")

        details["agents_executed"] = agents_executed

        if agents_executed < num_agents:
            details["error"] = (
                f"Only {agents_executed}/{num_agents} agents executed commands"
            )
            return False, details

        # 7. Verify storage writes (sandbox → workspace bind mount)
        # VM approaches can't verify storage from outside the VM, so
        # we trust the tool execution check (sandboxed=true, exit_code=0).
        if use_api:
            print("[smoke] Storage write check skipped (VM approach — "
                  "verified via API tool result)")
            agents_wrote = agents_executed  # trust the API check
            details["storage_check"] = "skipped-vm"
        else:
            print("[smoke] Checking storage writes from sandbox containers...")
            agents_wrote = 0
            for i, agent_id in enumerate(agent_ids):
                ws_dir = getattr(approach, '_workspace_dirs', {}).get(agent_id)
                written, write_detail = check_storage_write(i, host_workspace_dir=ws_dir)
                details[f"{agent_id}_storage"] = write_detail
                if written:
                    agents_wrote += 1
                    print(f"[smoke]   {agent_id}: WRITTEN ({write_detail})")
                else:
                    print(f"[smoke]   {agent_id}: NOT WRITTEN ({write_detail})")

        details["agents_wrote_storage"] = agents_wrote

        if agents_wrote >= num_agents:
            print(f"[smoke] PASS: {agents_executed}/{num_agents} executed, "
                  f"{agents_wrote}/{num_agents} wrote to storage")
            details["passed"] = True
            return True, details
        else:
            details["error"] = (
                f"Only {agents_wrote}/{num_agents} agents wrote to storage"
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
    parser.add_argument("--agents", type=int, default=NUM_AGENTS,
                        help=f"Number of agents to run (default: {NUM_AGENTS})")
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
        passed, details = run_smoke_test(approach, name, num_agents=args.agents)
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
