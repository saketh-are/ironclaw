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
import shlex
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
from runner.collect import Collector, HOST_CPU_FIELDS
from runner.monitor import BenchmarkMonitor
from runner.orchestrate import build_plateau_summary, compute_drift_slope, compute_percentiles


LAUNCH_VISIBILITY_TIMEOUT_S = 30.0
BASELINE_DURATION_S = 10.0


def _detect_docker_host_ip() -> str:
    """Detect the IP address of the Docker bridge gateway on the host.

    This is the IP that containers can use to reach the host.  Falls back
    to 172.17.0.1 (the Docker default) if detection fails.
    """
    import subprocess as _sp
    try:
        out = _sp.run(
            ["ip", "-4", "addr", "show", "docker0"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            for line in out.stdout.splitlines():
                line = line.strip()
                if line.startswith("inet "):
                    # e.g. "inet 172.20.0.1/16 brd ..."
                    return line.split()[1].split("/")[0]
    except Exception:
        pass
    return "172.17.0.1"


def emit_monitor_event(agent_root: Path, agent_id: str, event_name: str, **kwargs) -> None:
    """Append a monitor-compatible JSONL event to the agent's evidence log."""
    log_path = agent_root / "evidence" / "agent-events.jsonl"
    payload = {"event": event_name, "agent_id": agent_id, "t": time.time()}
    payload.update(kwargs)
    try:
        with open(log_path, "a") as f:
            f.write(json.dumps(payload) + "\n")
    except OSError:
        pass



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
    checkin_url: str = "",
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

    # Emoji check-in: worker picks a random emoji and POSTs it through the
    # sandbox network proxy to the benchmark monitor, proving end-to-end
    # network connectivity between the sandbox container and the host.
    checkin_snippet = ""
    if checkin_url:
        # The heredoc is single-quoted ('CHECKIN') so the shell passes
        # content through verbatim.  The f-string injects agent_id and
        # checkin_url; double braces {{ }} become literal braces for Python.
        checkin_snippet = textwrap.dedent(f"""\
            python3 - <<'CHECKIN'
import json, random, uuid, urllib.request, os
EMOJIS = [chr(c) for c in [
    0x1f980, 0x1f40d, 0x1f40b, 0x1f525, 0x26a1, 0x1f31f, 0x1f48e,
    0x1f680, 0x1f3af, 0x1f6e1, 0x1f527, 0x2699, 0x1f9ea, 0x1f3b2,
    0x1f308, 0x1f419, 0x1f98a, 0x1f422, 0x1f985, 0x1f41d, 0x1f340,
    0x1f33b, 0x1f52e, 0x1f3b5, 0x1f3d4,
]]
emoji = random.choice(EMOJIS)
job_id = os.environ.get("IRONCLAW_JOB_ID") or str(uuid.uuid4())[:12]
payload = json.dumps({{
    "agent_id": "{agent_id}",
    "job_id": job_id,
    "emoji": emoji,
}}).encode()
req = urllib.request.Request(
    "{checkin_url}",
    data=payload,
    headers={{"Content-Type": "application/json"}},
)
try:
    urllib.request.urlopen(req, timeout=5)
except Exception:
    pass
CHECKIN
        """).strip()

    if custom_command:
        base_command = custom_command.format(**context)
    else:
        # Build command: setup → proof → checkin (early!) → workload → storage event
        # NOTE: heredocs (<<'TAG' ... TAG) require the delimiter on its own
        # line, so we join with newlines (\n) rather than ` && ` when a
        # heredoc is involved.
        parts = [
            "set -eu",
            f"mkdir -p {proof_dir}",
            f"echo proof-{agent_id}-{trigger_index} > {proof_file}",
        ]

        # Emit the emoji check-in right after proof file write, before the
        # long sleep/workload.  This gives early visibility on the dashboard.
        if checkin_snippet:
            parts.append(checkin_snippet)

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
        base_command = "\n".join(parts)

    emit_storage_event = textwrap.dedent(f"""\
        if [ -f {shlex.quote(proof_file)} ]; then
            mkdir -p /workspace/.bench-evidence
            python3 - "/workspace/.bench-evidence/worker-storage-written-${{IRONCLAW_JOB_ID:-unknown}}.json" "${{IRONCLAW_JOB_ID:-unknown}}" {shlex.quote(proof_file)} <<'PY'
        import json
        import sys
        import time

        output_path, job_id, proof_path = sys.argv[1:]
        payload = {{
            "event": "worker_storage_written",
            "job_id": job_id,
            "ts_unix_ms": int(time.time() * 1000),
            "path": proof_path,
        }}
        with open(output_path, "w") as f:
            json.dump(payload, f)
        PY
        fi
    """).strip()
    # For custom commands, inject checkin before the custom command if available.
    if custom_command and checkin_snippet:
        return f"{checkin_snippet}\n{base_command}\n{emit_storage_event}"
    return f"{base_command}\n{emit_storage_event}"


def proof_relpath(agent_id: str, trigger_index: int) -> str:
    return f"bench-test/output-{agent_id}-{trigger_index}.txt"


def parse_rfc3339_epoch(raw: str):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def summarize_latency_ms(job_records: List[dict], field: str) -> dict:
    values = []
    for record in job_records:
        target = record.get(field)
        if target is None:
            continue
        if isinstance(target, str):
            target = parse_rfc3339_epoch(target)
        trigger_epoch = record.get("triggered_at_epoch_s")
        if trigger_epoch is None or target is None:
            continue
        values.append(max(0.0, (target - trigger_epoch) * 1000.0))
    if not values:
        return {"count": 0}
    percentiles = compute_percentiles(values, [50, 95, 99])
    return {
        "count": len(values),
        "p50": round(percentiles.get("p50", 0.0), 1),
        "p95": round(percentiles.get("p95", 0.0), 1),
        "p99": round(percentiles.get("p99", 0.0), 1),
        "max": round(max(values), 1),
    }


def parse_unix_ms(raw):
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) / 1000.0
    return None


def read_json_file(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def read_jsonl(path: Path) -> List[dict]:
    events = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return []
    return events


def ensure_agent_record(agent_records: Dict[str, dict], agent_id: str) -> dict:
    return agent_records.setdefault(agent_id, {
        "agent_id": agent_id,
        "started_logged": False,
        "started_at_epoch_s": None,
        "storage_logged": False,
        "storage_at_epoch_s": None,
        "storage_path": None,
        "storage_verified": False,
        "workspace_write_logged": False,
        "workspace_write_at_epoch_s": None,
        "workspace_write_path": None,
        "workspace_write_backend": None,
        "workspace_write_files": 0,
        "workspace_write_size_bytes": None,
        "workspace_write_verified": False,
        "exited_logged": False,
        "exited_at_epoch_s": None,
        "absent_verified": None,
    })


def sync_host_evidence(
    approach,
    agent_roots: Dict[str, Path],
    states: Dict[str, "AgentState"],
    tracked_jobs: Dict[str, dict],
    agent_records: Dict[str, dict],
) -> None:
    now_epoch_s = time.time()

    for agent_id, agent_root in agent_roots.items():
        state = states[agent_id]
        agent_record = ensure_agent_record(agent_records, agent_id)
        evidence_dir = Path(agent_root) / "evidence"
        event_log = evidence_dir / "agent-events.jsonl"

        for event in read_jsonl(event_log):
            event_name = event.get("event")
            event_epoch_s = parse_unix_ms(event.get("ts_unix_ms"))
            if event_name == "agent_started":
                agent_record["started_logged"] = True
                agent_record["started_at_epoch_s"] = (
                    event_epoch_s or agent_record.get("started_at_epoch_s")
                )
            elif event_name == "agent_storage_written":
                agent_record["storage_logged"] = True
                agent_record["storage_at_epoch_s"] = (
                    event_epoch_s or agent_record.get("storage_at_epoch_s")
                )
                path_str = event.get("path")
                host_path = approach.translate_agent_path(agent_id, path_str)
                if host_path is not None:
                    agent_record["storage_path"] = str(host_path)
                elif path_str:
                    agent_record["storage_path"] = path_str
                if host_path:
                    proof_path = host_path
                    if proof_path.exists():
                        try:
                            content = proof_path.read_text().strip()
                        except Exception:
                            content = ""
                        agent_record["storage_verified"] = content.startswith(
                            f"agent-storage {agent_id}"
                        )
            elif event_name == "agent_exited":
                agent_record["exited_logged"] = True
                agent_record["exited_at_epoch_s"] = event_epoch_s or agent_record.get("exited_at_epoch_s")

        workspace_write_path = evidence_dir / "agent-workspace-written.json"
        if not agent_record.get("workspace_write_logged") and workspace_write_path.exists():
            payload = read_json_file(workspace_write_path)
            if payload:
                agent_record["workspace_write_logged"] = True
                agent_record["workspace_write_at_epoch_s"] = parse_unix_ms(
                    payload.get("ts_unix_ms")
                )
                agent_record["workspace_write_backend"] = payload.get("backend")
                agent_record["workspace_write_files"] = int(payload.get("files_written") or 0)
                agent_record["workspace_write_size_bytes"] = payload.get("size_bytes")
                path_str = payload.get("path")
                host_path = approach.translate_agent_path(agent_id, path_str)
                if host_path is not None:
                    agent_record["workspace_write_path"] = str(host_path)
                elif path_str:
                    agent_record["workspace_write_path"] = path_str
                if host_path and host_path.exists():
                    try:
                        size_bytes = host_path.stat().st_size
                    except OSError:
                        size_bytes = 0
                    agent_record["workspace_write_verified"] = size_bytes > 0
                elif payload.get("size_bytes"):
                    # Some approaches, notably the QEMU VM path, keep the
                    # SQLite database on guest-local storage while still
                    # surfacing benchmark evidence on a host-visible share.
                    agent_record["workspace_write_verified"] = (
                        int(payload.get("size_bytes") or 0) > 0
                    )

        for created_path in sorted(evidence_dir.glob("job-created-*.json")):
            payload = read_json_file(created_path)
            if not payload:
                continue
            job_id = payload.get("job_id")
            if not job_id or job_id in tracked_jobs:
                continue
            pending = state.pending_triggers.popleft() if state.pending_triggers else {
                "trigger_index": None,
                "command": "",
                "triggered_at_epoch_s": now_epoch_s,
                "proof_relpath": None,
            }
            tracked_jobs[job_id] = {
                "job_id": job_id,
                "agent_id": agent_id,
                "trigger_index": pending.get("trigger_index"),
                "command": pending.get("command"),
                "triggered_at_epoch_s": pending.get("triggered_at_epoch_s"),
                "discovered_at_epoch_s": parse_unix_ms(payload.get("ts_unix_ms")) or now_epoch_s,
                "proof_relpath": pending.get("proof_relpath"),
                "project_dir": (
                    str(approach.translate_agent_path(agent_id, payload.get("project_dir")))
                    if payload.get("project_dir")
                    else None
                ),
                "job_mode": payload.get("mode"),
                "job_created_at_epoch_s": parse_unix_ms(payload.get("ts_unix_ms")) or now_epoch_s,
                "worker_started_at_epoch_s": None,
                "worker_storage_at_epoch_s": None,
                "worker_storage_logged": False,
                "worker_storage_path": None,
                "callback_at_epoch_s": None,
                "worker_cleaned_at_epoch_s": None,
                "proof_verified": None,
                "proof_verified_at_epoch_s": None,
                "proof_content": None,
                "result_success": None,
                "failure_reason": None,
                "worker_absent_verified": None,
            }
            active = sum(
                1 for r in tracked_jobs.values()
                if r.get("agent_id") == agent_id
                and r.get("job_created_at_epoch_s") is not None
                and r.get("worker_cleaned_at_epoch_s") is None
            )
            emit_monitor_event(agent_root, agent_id, "worker_start",
                               worker_id=job_id, active_workers=active)

        for callback_path in sorted(evidence_dir.glob("worker-callback-*.json")):
            payload = read_json_file(callback_path)
            if not payload:
                continue
            job_id = payload.get("job_id")
            record = tracked_jobs.get(job_id)
            if not record:
                continue
            record["callback_at_epoch_s"] = (
                parse_unix_ms(payload.get("ts_unix_ms")) or record.get("callback_at_epoch_s")
            )
            record["completed_at"] = record.get("callback_at_epoch_s")
            record["result_success"] = payload.get("success")
            record["failure_reason"] = payload.get("message")
            record["state"] = "completed" if payload.get("success") else "failed"

        for cleaned_path in sorted(evidence_dir.glob("worker-cleaned-*.json")):
            payload = read_json_file(cleaned_path)
            if not payload:
                continue
            job_id = payload.get("job_id")
            record = tracked_jobs.get(job_id)
            if not record:
                continue
            already_cleaned = record.get("worker_cleaned_at_epoch_s") is not None
            record["worker_cleaned_at_epoch_s"] = (
                parse_unix_ms(payload.get("ts_unix_ms")) or record.get("worker_cleaned_at_epoch_s")
            )
            record["worker_cleanup_logged"] = True
            record["container_removed"] = payload.get("container_removed")
            if not already_cleaned:
                active = sum(
                    1 for r in tracked_jobs.values()
                    if r.get("agent_id") == agent_id
                    and r.get("job_created_at_epoch_s") is not None
                    and r.get("worker_cleaned_at_epoch_s") is None
                )
                emit_monitor_event(agent_root, agent_id, "worker_end",
                                   worker_id=job_id, active_workers=active)

    for job_id, record in tracked_jobs.items():
        project_dir = record.get("project_dir")
        if not project_dir:
            continue
        project_path = Path(project_dir)
        started_path = project_path / ".bench-evidence" / f"worker-started-{job_id}.json"
        if record.get("worker_started_at_epoch_s") is None and started_path.exists():
            payload = read_json_file(started_path)
            if payload:
                record["worker_started_at_epoch_s"] = parse_unix_ms(payload.get("ts_unix_ms"))
                record["started_at"] = record.get("worker_started_at_epoch_s")
                # Note: checkin events are now emitted by the worker itself
                # via HTTP POST through the sandbox network proxy, not here.

        evidence_dir = project_path / ".bench-evidence"
        worker_storage_candidates = [
            evidence_dir / f"worker-storage-written-{job_id}.json",
            evidence_dir / "worker-storage-written.json",
        ]
        worker_storage_candidates.extend(
            sorted(evidence_dir.glob("worker-storage-written-*.json"))
        )
        if record.get("worker_storage_logged") is not True:
            for worker_storage_path in worker_storage_candidates:
                if not worker_storage_path.exists():
                    continue
                payload = read_json_file(worker_storage_path)
                if not payload:
                    continue
                record["worker_storage_logged"] = True
                record["worker_storage_at_epoch_s"] = parse_unix_ms(
                    payload.get("ts_unix_ms")
                )
                record["worker_storage_path"] = payload.get("path")
                agent_id = record.get("agent_id")
                agent_root = agent_roots.get(agent_id) if agent_id else None
                if agent_root:
                    emit_monitor_event(agent_root, agent_id, "worker_storage_written",
                                       worker_id=job_id)
                break

        if record.get("proof_relpath") and record.get("proof_verified") is None:
            proof_path = project_path / record["proof_relpath"]
            if proof_path.exists():
                try:
                    content = proof_path.read_text().strip()
                except Exception:
                    content = ""
                record["proof_content"] = content
                expected_prefix = (
                    f"proof-{record['agent_id']}-{record['trigger_index']}"
                    if record.get("trigger_index") is not None
                    else "proof-"
                )
                record["proof_verified"] = content.startswith(expected_prefix)
                record["proof_verified_at_epoch_s"] = proof_path.stat().st_mtime

        if (
            record.get("worker_cleaned_at_epoch_s") is not None
            and record.get("worker_absent_verified") is None
        ):
            verdict = approach.verify_worker_absent(record["agent_id"], job_id)
            if verdict is not None:
                record["worker_absent_verified"] = verdict

    for state in states.values():
        state.expire_pending(now_epoch_s)


def evidence_active_counts(
    agent_ids: List[str],
    tracked_jobs: Dict[str, dict],
) -> Dict[str, int]:
    counts = {agent_id: 0 for agent_id in agent_ids}
    for record in tracked_jobs.values():
        agent_id = record.get("agent_id")
        if agent_id not in counts:
            continue
        if record.get("job_created_at_epoch_s") is None:
            continue
        if record.get("worker_cleaned_at_epoch_s") is not None:
            continue
        counts[agent_id] += 1
    return counts


def fetch_active_counts(
    approach,
    agent_ids: List[str],
    agent_roots: Dict[str, Path],
    states: Dict[str, "AgentState"],
    tracked_jobs: Dict[str, dict],
    agent_records: Dict[str, dict],
    previous_counts: Dict[str, int],
) -> (Dict[str, int], Dict[str, str]):
    counts = {agent_id: 0 for agent_id in agent_ids}
    errors = {}
    try:
        observed = approach.count_active_workers_per_agent()
        if observed:
            for agent_id in counts:
                counts[agent_id] = int(observed.get(agent_id, 0))
        else:
            counts = dict(previous_counts)
    except Exception as exc:  # pragma: no cover - defensive
        counts = dict(previous_counts)
        errors["count_active_workers"] = str(exc)

    sync_host_evidence(approach, agent_roots, states, tracked_jobs, agent_records)
    for agent_id, evidence_count in evidence_active_counts(agent_ids, tracked_jobs).items():
        counts[agent_id] = max(int(counts.get(agent_id, 0)), evidence_count)
    return counts, errors


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
        self.pending_triggers: Deque[dict] = deque()
        self.trigger_index = 0
        self.trigger_attempts = 0
        self.trigger_ok = 0
        self.trigger_failed = 0
        self.skipped_due_to_limit = 0
        self.skipped_due_to_global_bucket = 0
        self.pending_timeouts = 0

    def effective_active(self, observed_active: int) -> int:
        return observed_active + len(self.pending_triggers)

    def expire_pending(self, now_epoch_s: float) -> None:
        while self.pending_triggers and (
            now_epoch_s - self.pending_triggers[0]["triggered_at_epoch_s"]
        ) > LAUNCH_VISIBILITY_TIMEOUT_S:
            self.pending_triggers.popleft()
            self.pending_timeouts += 1


class GlobalLaunchBucket:
    def __init__(self, capacity: float, refill_rate_per_s: float):
        self.capacity = max(0.0, float(capacity))
        self.refill_rate_per_s = max(0.0, float(refill_rate_per_s))
        self.tokens = self.capacity
        self.last_refill_at = time.monotonic()

    @property
    def enabled(self) -> bool:
        return self.capacity > 0.0

    def refill(self, now: float) -> None:
        if not self.enabled:
            return
        elapsed = max(0.0, now - self.last_refill_at)
        if elapsed > 0 and self.refill_rate_per_s > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate_per_s)
        self.last_refill_at = now

    def try_take(self, now: float, amount: float = 1.0) -> bool:
        if not self.enabled:
            return True
        self.refill(now)
        if self.tokens + 1e-9 < amount:
            return False
        self.tokens = max(0.0, self.tokens - amount)
        return True


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
        "max_triggers_per_agent": args.max_triggers_per_agent,
        "global_launch_bucket_size": args.global_launch_bucket_size,
        "global_launch_refill_rate_per_s": args.global_launch_refill_rate_per_s,
        "pre_trigger_settle_s": args.pre_trigger_settle_s,
        "batch_size": args.batch_size,
        "batch_interval_s": args.batch_interval_s,
        "job_dispatch": args.job_dispatch,
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
            "job_dispatch": args.job_dispatch,
            "global_launch_bucket_size": args.global_launch_bucket_size,
            "global_launch_refill_rate_per_s": args.global_launch_refill_rate_per_s,
        },
    }


def control_loaded(
    args,
    approach,
    agent_ids: List[str],
    agent_roots: Dict[str, Path],
    states: Dict[str, AgentState],
    tracked_jobs: Dict[str, dict],
    agent_records: Dict[str, dict],
    benchmark_duration_s: float,
) -> dict:
    start_time = time.monotonic()
    end_time = time.monotonic() + benchmark_duration_s
    control_samples = []
    last_counts = {agent_id: 0 for agent_id in agent_ids}
    last_errors = {}
    launch_bucket = GlobalLaunchBucket(
        args.global_launch_bucket_size,
        args.global_launch_refill_rate_per_s,
    )
    bucket_deferred = 0
    agent_cursor = 0
    while time.monotonic() < end_time:
        now = time.monotonic()
        counts, errors = fetch_active_counts(
            approach, agent_ids, agent_roots, states, tracked_jobs, agent_records, last_counts
        )
        last_counts = counts
        last_errors = errors

        total_active = sum(counts.values())
        control_samples.append({
            "t_s": round(now - start_time, 3),
            "active_workers": total_active,
        })

        if launch_bucket.enabled and agent_ids:
            ordered_agent_ids = agent_ids[agent_cursor:] + agent_ids[:agent_cursor]
        else:
            ordered_agent_ids = agent_ids

        for agent_id in ordered_agent_ids:
            state = states[agent_id]
            if now < state.release_at or now < state.next_spawn_at:
                continue
            if (
                args.max_triggers_per_agent > 0
                and state.trigger_ok >= args.max_triggers_per_agent
            ):
                continue
            observed = counts[agent_id]
            if state.effective_active(observed) < args.max_concurrent_workers:
                if not launch_bucket.try_take(now):
                    state.skipped_due_to_global_bucket += 1
                    bucket_deferred += 1
                    state.next_spawn_at = now + exponential_delay_s(
                        state.rng, args.spawn_interval_mean_s
                    )
                    continue
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
                    checkin_url=getattr(args, "checkin_url", ""),
                )
                state.trigger_attempts += 1
                ok = approach.trigger_worker_spawn(
                    agent_id,
                    command=command,
                    dispatch_mode=args.job_dispatch,
                )
                if ok:
                    state.trigger_ok += 1
                    state.pending_triggers.append({
                        "trigger_index": state.trigger_index,
                        "command": command,
                        "triggered_at_epoch_s": time.time(),
                        "proof_relpath": proof_relpath(agent_id, state.trigger_index),
                    })
                    state.trigger_index += 1
                    if launch_bucket.enabled and agent_ids:
                        agent_cursor = (agent_ids.index(agent_id) + 1) % len(agent_ids)
                else:
                    state.trigger_failed += 1
            else:
                state.skipped_due_to_limit += 1
            state.next_spawn_at = now + exponential_delay_s(state.rng, args.spawn_interval_mean_s)

        time.sleep(args.control_interval_s)

    final_counts, final_errors = fetch_active_counts(
        approach, agent_ids, agent_roots, states, tracked_jobs, agent_records, last_counts
    )
    launch_bucket.refill(time.monotonic())
    return {
        "control_samples": control_samples,
        "last_errors": last_errors,
        "final_errors": final_errors,
        "final_counts": final_counts,
        "global_launch_bucket": {
            "enabled": launch_bucket.enabled,
            "capacity": launch_bucket.capacity,
            "refill_rate_per_s": launch_bucket.refill_rate_per_s,
            "tokens_remaining": round(launch_bucket.tokens, 3),
            "deferred": bucket_deferred,
        },
    }


def control_plateau(
    args,
    approach,
    agent_ids: List[str],
    agent_roots: Dict[str, Path],
    states: Dict[str, AgentState],
    tracked_jobs: Dict[str, dict],
    agent_records: Dict[str, dict],
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

        counts, errors = fetch_active_counts(
            approach, agent_ids, agent_roots, states, tracked_jobs, agent_records, last_counts
        )
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
            if (
                args.max_triggers_per_agent > 0
                and state.trigger_ok >= args.max_triggers_per_agent
            ):
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
                    checkin_url=getattr(args, "checkin_url", ""),
                )
                state.trigger_attempts += 1
                ok = approach.trigger_worker_spawn(
                    agent_id,
                    command=command,
                    dispatch_mode=args.job_dispatch,
                )
                if ok:
                    state.trigger_ok += 1
                    state.pending_triggers.append({
                        "trigger_index": state.trigger_index,
                        "command": command,
                        "triggered_at_epoch_s": time.time(),
                        "proof_relpath": proof_relpath(agent_id, state.trigger_index),
                    })
                    state.trigger_index += 1
                else:
                    state.trigger_failed += 1
            elif state.effective_active(observed) > target:
                state.skipped_due_to_limit += 1

        time.sleep(args.control_interval_s)

    final_counts, final_errors = fetch_active_counts(
        approach, agent_ids, agent_roots, states, tracked_jobs, agent_records, last_counts
    )
    return {
        "control_samples": control_samples,
        "last_errors": last_errors,
        "final_errors": final_errors,
        "final_counts": final_counts,
        "plateau_start_offset_s": args.pre_trigger_settle_s,
    }


def control_idle(
    args,
    approach,
    agent_ids: List[str],
    agent_roots: Dict[str, Path],
    states: Dict[str, AgentState],
    tracked_jobs: Dict[str, dict],
    agent_records: Dict[str, dict],
) -> dict:
    start_time = time.monotonic()
    end_time = time.monotonic() + args.benchmark_duration_s
    control_samples = []
    last_counts = {agent_id: 0 for agent_id in agent_ids}
    last_errors = {}
    while time.monotonic() < end_time:
        counts, errors = fetch_active_counts(
            approach, agent_ids, agent_roots, states, tracked_jobs, agent_records, last_counts
        )
        last_counts = counts
        last_errors = errors
        control_samples.append({
            "t_s": round(time.monotonic() - start_time, 3),
            "active_workers": sum(counts.values()),
        })
        time.sleep(args.control_interval_s)
    final_counts, final_errors = fetch_active_counts(
        approach, agent_ids, agent_roots, states, tracked_jobs, agent_records, last_counts
    )
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
    parser.add_argument("--job-dispatch", choices=["worker-job", "shell"], default="worker-job",
                        help="Dispatch jobs through real worker-mode sandbox jobs or direct shell")
    parser.add_argument("--pre-trigger-settle-s", type=float, default=10.0,
                        help="Collect zero-worker samples after agents start before control begins")
    parser.add_argument("--control-interval-s", type=float, default=1.0,
                        help="Host control-loop polling interval")
    parser.add_argument("--max-triggers-per-agent", type=int, default=0,
                        help="Maximum total job triggers per agent (0 = unlimited)")
    parser.add_argument("--global-launch-bucket-size", type=float, default=0.0,
                        help="Shared loaded-mode launch burst budget across all agents (0 = disabled)")
    parser.add_argument("--global-launch-refill-rate-per-s", type=float, default=0.0,
                        help="Shared loaded-mode launch token refill rate across all agents")
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
    parser.add_argument("--monitor-port", type=int, default=0,
                        help="Port for live monitor dashboard (0 = disabled)")
    parser.add_argument("--monitor-host", default="0.0.0.0",
                        help="Host for monitor dashboard")
    args = parser.parse_args()

    if args.job_profile == "custom" and not args.job_command:
        parser.error("--job-profile custom requires --job-command")
    if args.global_launch_bucket_size < 0 or args.global_launch_refill_rate_per_s < 0:
        parser.error("--global-launch-bucket-size and --global-launch-refill-rate-per-s must be >= 0")
    if args.mode != "loaded" and (
        args.global_launch_bucket_size > 0 or args.global_launch_refill_rate_per_s > 0
    ):
        parser.error("--global-launch-bucket-* only applies to loaded mode")
    if args.global_launch_refill_rate_per_s > 0 and args.global_launch_bucket_size <= 0:
        parser.error("--global-launch-bucket-size must be > 0 when --global-launch-refill-rate-per-s is set")
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
    run_dir = run_dir.resolve()
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
        run_dir=str(run_dir),
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
    agent_roots: Dict[str, Path] = {}
    agent_records: Dict[str, dict] = {}
    tracked_jobs = {}
    summary = dict(params)
    run_error = None
    cleanup_error = None
    monitor = None

    if args.monitor_port:
        monitor = BenchmarkMonitor(
            host=args.monitor_host, port=args.monitor_port,
            approach=args.approach, mode=args.mode, run_id=run_label,
            expected_agents=[f"agent-{i}" for i in range(args.agents)],
            duration_s=int(args.benchmark_duration_s),
            max_worker_slots=args.max_concurrent_workers,
        )
        monitor.start()
        print(f"[ironclaw-benchmark] monitor: {monitor.url}", flush=True)
        monitor.set_phase("setup", f"Setting up {args.approach}")
        # Build a checkin URL that sandbox workers can reach through the
        # network proxy.  Detect the Docker bridge gateway IP so this works
        # regardless of Docker network configuration.
        _, monitor_port = monitor.address
        docker_host_ip = _detect_docker_host_ip()
        args.checkin_url = f"http://{docker_host_ip}:{monitor_port}/api/checkin"
        # Allow sandbox workers to reach the monitor host through the proxy.
        config.sandbox_extra_domains = docker_host_ip
    else:
        args.checkin_url = ""

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

        if monitor:
            monitor.set_phase("starting_agents", f"Launching {args.agents} agents")
        print(f"[ironclaw-benchmark] starting {args.agents} agents", flush=True)
        agent_ids = approach.start_agents(args.agents, config)
        summary["agent_ids"] = agent_ids
        summary["agent_start_elapsed_s"] = round(time.time() - start_ts, 1)
        agent_roots = approach.get_agent_roots()

        if monitor:
            log_paths = approach.live_event_log_paths(agent_ids, run_dir)
            monitor.attach_agents(agent_ids, log_paths)
            gateways = approach.get_agent_gateways()
            if gateways:
                monitor.state.set_agent_gateways(gateways)

        if set(agent_roots) != set(agent_ids):
            raise RuntimeError("Approach did not expose host-visible roots for all agents")

        states = {}
        control_start_ref = time.monotonic() + args.pre_trigger_settle_s
        for agent_id in agent_ids:
            rng = per_agent_rng(args.rng_seed, agent_id)
            release_at = control_start_ref + ramp_delay_s(agent_id, args.batch_size, args.batch_interval_s)
            next_spawn_at = release_at + exponential_delay_s(rng, args.spawn_interval_mean_s)
            states[agent_id] = AgentState(rng, release_at, next_spawn_at)

        def collector_active_count():
            try:
                observed_total = int(approach.count_active_workers())
            except Exception:
                observed_total = 0
            evidence_total = sum(evidence_active_counts(agent_ids, tracked_jobs).values())
            return max(observed_total, evidence_total)

        running_collector = Collector(interval_ms=args.sample_interval_ms, phase="running")
        running_thread = running_collector.run_in_thread(
            output=ts_file,
            get_agent_pids=approach.get_agent_pids,
            get_daemon_pids=approach.get_daemon_pids,
            count_workers=collector_active_count,
        )

        if args.pre_trigger_settle_s > 0:
            print(
                f"[ironclaw-benchmark] collecting zero-worker settle window for {args.pre_trigger_settle_s:.1f}s",
                flush=True,
            )
            time.sleep(args.pre_trigger_settle_s)

        sync_host_evidence(approach, agent_roots, states, tracked_jobs, agent_records)

        if monitor:
            monitor.set_phase("running", f"{args.mode} control loop active")

        if args.mode == "idle":
            control_result = control_idle(
                args, approach, agent_ids, agent_roots, states, tracked_jobs, agent_records
            )
        elif args.mode == "loaded":
            control_result = control_loaded(
                args,
                approach,
                agent_ids,
                agent_roots,
                states,
                tracked_jobs,
                agent_records,
                args.benchmark_duration_s,
            )
        else:
            control_result = control_plateau(
                args, approach, agent_ids, agent_roots, states, tracked_jobs, agent_records
            )
            params["plateau_start_offset_s"] = control_result.get("plateau_start_offset_s", 0.0)
            summary["plateau_start_offset_s"] = params["plateau_start_offset_s"]
            with open(run_dir / "params.json", "w") as f:
                json.dump(params, f, indent=2)

        if monitor:
            monitor.set_phase("cooldown", "Collecting final evidence")
        running_collector.stop()
        running_thread.join(timeout=30)
        ts_file.close()
        ts_file = None
        sync_host_evidence(approach, agent_roots, states, tracked_jobs, agent_records)

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
        summary["global_launch_bucket_deferred"] = sum(
            state.skipped_due_to_global_bucket for state in states.values()
        )
        summary["per_agent_triggers_ok"] = {agent_id: state.trigger_ok for agent_id, state in states.items()}
        summary["per_agent_trigger_attempts"] = {agent_id: state.trigger_attempts for agent_id, state in states.items()}
        summary["per_agent_pending_timeouts"] = {agent_id: state.pending_timeouts for agent_id, state in states.items()}
        summary["per_agent_global_launch_bucket_deferred"] = {
            agent_id: state.skipped_due_to_global_bucket for agent_id, state in states.items()
        }
        if "global_launch_bucket" in control_result:
            summary["global_launch_bucket"] = control_result["global_launch_bucket"]
        job_records = sorted(
            tracked_jobs.values(),
            key=lambda record: (
                record.get("agent_id", ""),
                record.get("trigger_index")
                if record.get("trigger_index") is not None
                else 10**9,
                record.get("job_id", ""),
            ),
        )
        summary["agent_records"] = [agent_records[agent_id] for agent_id in sorted(agent_records)]
        summary["job_records"] = job_records
        summary["agents_started"] = sum(
            1 for record in agent_records.values() if record.get("started_logged")
        )
        summary["agents_with_storage"] = sum(
            1
            for record in agent_records.values()
            if record.get("storage_logged") and record.get("storage_verified")
        )
        summary["agents_with_workspace_write"] = sum(
            1
            for record in agent_records.values()
            if record.get("workspace_write_logged") and record.get("workspace_write_verified")
        )
        summary["jobs_discovered"] = len(job_records)
        summary["jobs_started"] = sum(
            1 for record in job_records if record.get("worker_started_at_epoch_s")
        )
        summary["jobs_with_storage_event"] = sum(
            1 for record in job_records if record.get("worker_storage_logged") is True
        )
        summary["jobs_with_callback_event"] = sum(
            1 for record in job_records if record.get("callback_at_epoch_s")
        )
        summary["jobs_with_proof"] = sum(1 for record in job_records if record.get("proof_verified") is True)
        summary["jobs_cleaned"] = sum(
            1 for record in job_records if record.get("worker_cleaned_at_epoch_s")
        )
        summary["jobs_cleanup_verified"] = sum(
            1 for record in job_records if record.get("worker_absent_verified") is True
        )
        summary["jobs_completed"] = sum(1 for record in job_records if record.get("completed_at"))
        summary["jobs_succeeded"] = sum(
            1
            for record in job_records
            if record.get("result_success") is True
            and record.get("proof_verified") is True
        )
        summary["jobs_failed"] = sum(
            1
            for record in job_records
            if record.get("result_success") is False
        )
        summary["job_latency_ms"] = {
            "trigger_to_job_created": summarize_latency_ms(job_records, "job_created_at_epoch_s"),
            "trigger_to_started": summarize_latency_ms(job_records, "worker_started_at_epoch_s"),
            "trigger_to_worker_storage": summarize_latency_ms(job_records, "worker_storage_at_epoch_s"),
            "trigger_to_proof": summarize_latency_ms(job_records, "proof_verified_at_epoch_s"),
            "trigger_to_callback": summarize_latency_ms(job_records, "callback_at_epoch_s"),
            "trigger_to_cleanup": summarize_latency_ms(job_records, "worker_cleaned_at_epoch_s"),
        }
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

    print("[ironclaw-benchmark] stopping agents...", flush=True)
    try:
        approach.stop_agents()
    except Exception as exc:
        cleanup_error = exc
        print(f"[ironclaw-benchmark] stop error: {exc}", flush=True)

    if "states" in locals():
        try:
            sync_host_evidence(approach, agent_roots, states, tracked_jobs, agent_records)
        except Exception as exc:
            if cleanup_error is None:
                cleanup_error = exc
            print(f"[ironclaw-benchmark] post-stop evidence sync error: {exc}", flush=True)

    if monitor:
        monitor.set_phase("cleanup", "Removing containers")
    print("[ironclaw-benchmark] cleaning up...", flush=True)
    try:
        approach.cleanup()
    except Exception as exc:
        if cleanup_error is None:
            cleanup_error = exc
        print(f"[ironclaw-benchmark] cleanup error: {exc}", flush=True)

    if "states" in locals():
        try:
            sync_host_evidence(approach, agent_roots, states, tracked_jobs, agent_records)
        except Exception as exc:
            if cleanup_error is None:
                cleanup_error = exc
            print(f"[ironclaw-benchmark] post-cleanup evidence sync error: {exc}", flush=True)

    for agent_id in agent_ids:
        agent_record = ensure_agent_record(agent_records, agent_id)
        try:
            verdict = approach.verify_agent_absent(agent_id)
        except Exception as exc:
            if cleanup_error is None:
                cleanup_error = exc
            print(f"[ironclaw-benchmark] cleanup verification error for {agent_id}: {exc}", flush=True)
            verdict = None
        if verdict is not None:
            agent_record["absent_verified"] = verdict

    summary["agent_records"] = [agent_records[agent_id] for agent_id in sorted(agent_records)]
    summary["agents_exited_logged"] = sum(
        1 for record in agent_records.values() if record.get("exited_logged")
    )
    summary["agents_cleanup_verified"] = sum(
        1 for record in agent_records.values() if record.get("absent_verified") is True
    )
    if cleanup_error is not None:
        summary["cleanup_error"] = str(cleanup_error)

    if (run_dir / "timeseries.jsonl").exists():
        summary.update(summarize_timeseries(run_dir, params, args.agents))

    with open(run_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps({
        "run_dir": str(run_dir),
        "approach": args.approach,
        "mode": args.mode,
        "workers_spawned": summary.get("workers_spawned", 0),
        "jobs_with_proof": summary.get("jobs_with_proof", 0),
        "jobs_succeeded": summary.get("jobs_succeeded", 0),
        "trigger_to_callback_p50_ms": (
            summary.get("job_latency_ms", {})
            .get("trigger_to_callback", {})
            .get("p50")
        ),
        "avg_workers": round(summary.get("avg_workers", 0.0), 3),
        "per_agent_mean_mib": round(summary.get("per_agent_mean_mib", 0.0), 3),
        "final_active_workers": summary.get("final_active_workers", 0),
        "peak_mib": round(summary.get("peak_mib", 0.0), 3),
        "p95_mib": round(summary.get("p95_mib", 0.0), 3),
        "agents_started": summary.get("agents_started", 0),
        "agents_cleanup_verified": summary.get("agents_cleanup_verified", 0),
        "jobs_cleanup_verified": summary.get("jobs_cleanup_verified", 0),
    }, indent=2), flush=True)

    if monitor:
        monitor.set_phase("done", "Benchmark complete")
        monitor.stop()

    if cleanup_error is not None:
        raise cleanup_error
    if run_error is not None:
        raise run_error


if __name__ == "__main__":
    main()
