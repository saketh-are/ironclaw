#!/usr/bin/env python3
"""
Agent simulator for isolation benchmarks.

Simulates an ironclaw agent by:
1. Allocating baseline memory (agent's own footprint)
2. Running a spawn loop that creates worker containers via the Docker API

Worker lifecycle matches ironclaw's ContainerJobManager:
  create → start → wait → remove

Emits structured JSONL events to stdout for host-side collection:
  {"t": ..., "event": "worker_start", "agent_id": ..., "worker_id": ...}
  {"t": ..., "event": "worker_end",   "agent_id": ..., "worker_id": ...}
  {"t": ..., "event": "status",       "agent_id": ..., "active_workers": N}

Environment variables:
  AGENT_ID                Unique agent identifier (required)
  AGENT_BASELINE_MB       Agent process memory footprint (default: 50)
  SPAWN_INTERVAL_MEAN_S   Mean time between spawns, exponential dist (default: 30)
  MAX_CONCURRENT_WORKERS  Max workers running at once per agent (default: 5)
  BENCHMARK_DURATION_S    Total benchmark duration (default: 300)
  WORKER_IMAGE            Worker container image (default: bench-worker:latest)
  WORKER_MEMORY_LIMIT_MB  Worker container memory limit (default: 2048)
  WORKER_MEMORY_MB        Memory each worker allocates (default: 500)
  WORKER_DURATION_MIN_S   Min worker lifetime (default: 30)
  WORKER_DURATION_MAX_S   Max worker lifetime (default: 120)
  RNG_SEED                Base RNG seed for reproducibility (default: 42)
  BENCH_RUN_ID            Unique run identifier for container labels (default: unknown)
  BENCH_APPROACH          Approach name for container labels (default: unknown)
"""

import hashlib
import json
import mmap
import os
import random
import signal
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

import docker


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AGENT_ID = os.environ.get("AGENT_ID", "agent-0")
AGENT_BASELINE_MB = int(os.environ.get("AGENT_BASELINE_MB", "50"))
SPAWN_INTERVAL_MEAN_S = float(os.environ.get("SPAWN_INTERVAL_MEAN_S", "30"))
MAX_CONCURRENT_WORKERS = int(os.environ.get("MAX_CONCURRENT_WORKERS", "5"))
BENCHMARK_DURATION_S = int(os.environ.get("BENCHMARK_DURATION_S", "300"))
WORKER_IMAGE = os.environ.get("WORKER_IMAGE", "bench-worker:latest")
WORKER_MEMORY_LIMIT_MB = int(os.environ.get("WORKER_MEMORY_LIMIT_MB", "2048"))
WORKER_MEMORY_MB = int(os.environ.get("WORKER_MEMORY_MB", "500"))
WORKER_DURATION_MIN_S = int(os.environ.get("WORKER_DURATION_MIN_S", "30"))
WORKER_DURATION_MAX_S = int(os.environ.get("WORKER_DURATION_MAX_S", "120"))
RNG_SEED_BASE = int(os.environ.get("RNG_SEED", "42"))
BENCH_RUN_ID = os.environ.get("BENCH_RUN_ID", "unknown")
BENCH_APPROACH = os.environ.get("BENCH_APPROACH", "unknown")
WORKER_RUNTIME = os.environ.get("WORKER_RUNTIME", "")  # e.g. "runsc" for gVisor
ORCHESTRATOR_PORT = os.environ.get("ORCHESTRATOR_PORT", "")  # HTTP server port (empty = disabled)
ORCHESTRATOR_HOST_PORT = os.environ.get("ORCHESTRATOR_HOST_PORT", "")  # Host-mapped port (container approaches)

# Derive per-agent seed: hash(base_seed + agent_id) for reproducibility
_seed_input = f"{RNG_SEED_BASE}:{AGENT_ID}"
AGENT_SEED = int(hashlib.sha256(_seed_input.encode()).hexdigest()[:8], 16)

WORKER_PREFIX = f"bench-worker-{AGENT_ID}"
STATUS_INTERVAL_S = 5  # How often to emit status events


# ---------------------------------------------------------------------------
# Structured event logging
# ---------------------------------------------------------------------------

_log_lock = threading.Lock()


def emit_event(event: str, **kwargs):
    """Emit a structured JSONL event to stdout."""
    record = {"t": time.time(), "event": event, "agent_id": AGENT_ID}
    record.update(kwargs)
    line = json.dumps(record)
    with _log_lock:
        print(line, flush=True)


def log(msg: str):
    """Emit a human-readable log line (also structured)."""
    emit_event("log", message=msg)


# ---------------------------------------------------------------------------
# Orchestrator HTTP server
# ---------------------------------------------------------------------------

DOCKER_BRIDGE_GATEWAY = "172.17.0.1"


def compute_orchestrator_url() -> str:
    """Compute the URL workers should use to reach this agent's HTTP server."""
    if ORCHESTRATOR_HOST_PORT:
        # Container approach: workers reach the agent via host-mapped port
        return f"http://{DOCKER_BRIDGE_GATEWAY}:{ORCHESTRATOR_HOST_PORT}"
    elif ORCHESTRATOR_PORT:
        # VM approach: agent is the host, workers use bridge gateway + agent port
        return f"http://{DOCKER_BRIDGE_GATEWAY}:{ORCHESTRATOR_PORT}"
    return ""


# Thread-safe set of worker IDs that have checked in
_checkins = set()
_checkins_lock = threading.Lock()


def record_checkin(worker_id: str) -> int:
    """Record a worker checkin. Returns the total number of unique checkins."""
    with _checkins_lock:
        _checkins.add(worker_id)
        return len(_checkins)


def get_checkin_count() -> int:
    with _checkins_lock:
        return len(_checkins)


class OrchestratorHandler(BaseHTTPRequestHandler):
    """Lightweight HTTP handler for agent ↔ worker communication."""

    def _read_json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b"{}"
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {}

    def _respond_ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_GET(self):
        if self.path == "/health":
            self._respond_ok()
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/heartbeat":
            data = self._read_json_body()
            worker_id = data.get("worker_id", "unknown")
            rss_kb = data.get("rss_kb", -1)
            emit_event("heartbeat", worker_id=worker_id, rss_kb=rss_kb)
            self._respond_ok()
        elif self.path == "/checkin":
            data = self._read_json_body()
            worker_id = data.get("worker_id", "unknown")
            total = record_checkin(worker_id)
            emit_event("checkin", worker_id=worker_id, total_checkins=total)
            self._respond_ok()
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        # Suppress default stderr logging; we emit structured events instead
        pass


def start_orchestrator_server(port: int) -> HTTPServer:
    """Start the orchestrator HTTP server in a background thread."""
    server = HTTPServer(("0.0.0.0", port), OrchestratorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log(f"Orchestrator HTTP server listening on 0.0.0.0:{port}")
    return server


# ---------------------------------------------------------------------------
# Baseline memory allocation
# ---------------------------------------------------------------------------

def allocate_baseline(size_mb: int) -> mmap.mmap:
    """Allocate anonymous memory and touch every page."""
    size_bytes = size_mb * 1024 * 1024
    mm = mmap.mmap(-1, size_bytes, mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
    page_size = os.sysconf("SC_PAGESIZE")
    for offset in range(0, size_bytes, page_size):
        mm[offset] = offset & 0xFF
    return mm


# ---------------------------------------------------------------------------
# Worker lifecycle: create → start → wait → remove
# ---------------------------------------------------------------------------

# Thread-safe active worker count
_active_workers = 0
_active_lock = threading.Lock()
_worker_counter = 0
_counter_lock = threading.Lock()


def get_active_workers() -> int:
    with _active_lock:
        return _active_workers


def _inc_active():
    global _active_workers
    with _active_lock:
        _active_workers += 1
        return _active_workers


def _dec_active():
    global _active_workers
    with _active_lock:
        _active_workers -= 1
        return _active_workers


def next_worker_id() -> tuple:
    """Return (counter, worker_name)."""
    global _worker_counter
    with _counter_lock:
        _worker_counter += 1
        c = _worker_counter
    return c, f"{WORKER_PREFIX}-{c}"


def spawn_worker(client: docker.DockerClient, rng: random.Random) -> bool:
    """
    Spawn a single worker container using the ironclaw lifecycle:
    create → start → wait → remove.

    Returns True if the container was created and started successfully.
    The wait/remove happens in a background thread.
    """
    counter, worker_name = next_worker_id()

    # Container config mirrors ironclaw sandbox defaults
    env = {
        "WORKER_MEMORY_MB": str(WORKER_MEMORY_MB),
        "WORKER_DURATION_MIN_S": str(WORKER_DURATION_MIN_S),
        "WORKER_DURATION_MAX_S": str(WORKER_DURATION_MAX_S),
    }
    orchestrator_url = compute_orchestrator_url()
    if orchestrator_url:
        env["ORCHESTRATOR_URL"] = orchestrator_url

    create_kwargs = dict(
        image=WORKER_IMAGE,
        name=worker_name,
        mem_limit=f"{WORKER_MEMORY_LIMIT_MB}m",
        cap_drop=["ALL"],
        cap_add=["CHOWN"],
        security_opt=["no-new-privileges"],
        tmpfs={"/tmp": "size=512m"},
        user="1000:1000",
        environment=env,
        labels={
            "bench_run_id": BENCH_RUN_ID,
            "bench_role": "worker",
            "bench_agent_id": AGENT_ID,
            "bench_approach": BENCH_APPROACH,
        },
        detach=True,
    )
    if WORKER_RUNTIME:
        create_kwargs["runtime"] = WORKER_RUNTIME

    try:
        container = client.containers.create(**create_kwargs)
    except docker.errors.APIError as e:
        log(f"Failed to create {worker_name}: {e}")
        return False

    try:
        container.start()
    except docker.errors.APIError as e:
        log(f"Failed to start {worker_name}: {e}")
        try:
            container.remove(force=True)
        except Exception:
            pass
        return False

    count = _inc_active()
    emit_event("worker_start", worker_id=worker_name, active_workers=count)

    # Wait and remove in background thread (matches ironclaw's async wait)
    def wait_and_remove():
        try:
            container.wait()
        except Exception:
            pass
        try:
            container.remove(force=True)
        except Exception:
            pass
        count = _dec_active()
        emit_event("worker_end", worker_id=worker_name, active_workers=count)

    t = threading.Thread(target=wait_and_remove, daemon=True)
    t.start()
    return True


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup_workers(client: docker.DockerClient):
    """Remove all worker containers for this agent."""
    log("Cleaning up workers...")
    try:
        containers = client.containers.list(
            all=True,
            filters={"label": [f"bench_agent_id={AGENT_ID}", "bench_role=worker"]},
        )
        for c in containers:
            try:
                c.remove(force=True)
            except Exception:
                pass
        if containers:
            log(f"Removed {len(containers)} worker containers.")
    except Exception as e:
        log(f"Cleanup error: {e}")


# ---------------------------------------------------------------------------
# Status emitter (periodic)
# ---------------------------------------------------------------------------

def status_emitter(stop_event: threading.Event):
    """Periodically emit status events with active worker count."""
    while not stop_event.is_set():
        emit_event("status", active_workers=get_active_workers())
        stop_event.wait(timeout=STATUS_INTERVAL_S)


# ---------------------------------------------------------------------------
# Main spawn loop
# ---------------------------------------------------------------------------

def main():
    # Set up reproducible RNG
    rng = random.Random(AGENT_SEED)

    emit_event("agent_start",
               baseline_mb=AGENT_BASELINE_MB,
               spawn_interval_mean_s=SPAWN_INTERVAL_MEAN_S,
               max_concurrent_workers=MAX_CONCURRENT_WORKERS,
               duration_s=BENCHMARK_DURATION_S,
               rng_seed=AGENT_SEED,
               worker_runtime=WORKER_RUNTIME or "default",
               orchestrator_port=ORCHESTRATOR_PORT or "disabled",
               orchestrator_url=compute_orchestrator_url() or "disabled")

    # Allocate baseline memory
    log(f"Allocating {AGENT_BASELINE_MB} MB baseline memory...")
    _baseline_mem = allocate_baseline(AGENT_BASELINE_MB)
    log(f"Baseline memory allocated.")

    # Connect to Docker
    client = docker.from_env()

    # Start orchestrator HTTP server if configured
    _server = None
    if ORCHESTRATOR_PORT:
        _server = start_orchestrator_server(int(ORCHESTRATOR_PORT))

    # Set up signal handler for clean shutdown
    stop = threading.Event()

    def handle_signal(sig, frame):
        log(f"Received signal {sig}, shutting down...")
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Start status emitter
    status_thread = threading.Thread(
        target=status_emitter, args=(stop,), daemon=True
    )
    status_thread.start()

    # Spawn loop
    start_time = time.monotonic()
    log("Entering spawn loop.")

    while not stop.is_set():
        # Check duration
        elapsed = time.monotonic() - start_time
        if elapsed >= BENCHMARK_DURATION_S:
            log(f"Benchmark duration reached ({elapsed:.0f}s). Stopping.")
            break

        # Sleep for random interval (exponential distribution)
        delay = rng.expovariate(1.0 / SPAWN_INTERVAL_MEAN_S)
        if stop.wait(timeout=delay):
            break  # Signalled to stop during sleep

        # Check capacity
        active = get_active_workers()
        if active >= MAX_CONCURRENT_WORKERS:
            log(f"At capacity ({active}/{MAX_CONCURRENT_WORKERS}). Skipping spawn.")
            continue

        # Spawn
        log(f"Spawning worker (active: {active}/{MAX_CONCURRENT_WORKERS})")
        spawn_worker(client, rng)

    # Cleanup
    stop.set()

    # Grace period: let in-flight workers finish their checkin before killing them.
    # Workers allocate memory (~1-2s) then immediately POST /checkin.
    if ORCHESTRATOR_PORT and get_active_workers() > 0:
        grace_s = 10
        log(f"Waiting {grace_s}s for {get_active_workers()} in-flight workers to check in...")
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline and get_checkin_count() < _worker_counter:
            time.sleep(0.5)

    cleanup_workers(client)

    # Emit checkin summary for post-run validation
    if ORCHESTRATOR_PORT:
        checkins = get_checkin_count()
        emit_event("checkin_summary",
                   workers_spawned=_worker_counter,
                   checkins_received=checkins,
                   checkins_ok=checkins == _worker_counter)

    emit_event("agent_stop", total_workers_spawned=_worker_counter)
    log("Agent stopped.")


if __name__ == "__main__":
    main()
