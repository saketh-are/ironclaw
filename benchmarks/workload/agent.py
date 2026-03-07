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
  BENCHMARK_MODE          Benchmark mode: loaded, idle, or plateau
  AGENT_BASELINE_MB       Agent process memory footprint (default: 50)
  SPAWN_INTERVAL_MEAN_S   Mean time between spawns, exponential dist (default: 30)
  SPAWN_RAMP_BATCH_SIZE   Loaded-mode agents released per ramp cohort (default: 0 = off)
  SPAWN_RAMP_INTERVAL_S   Delay between loaded-mode ramp cohorts in seconds (default: 0)
  MAX_CONCURRENT_WORKERS  Max workers running at once per agent (default: 5)
  CHECKIN_GRACE_S         Shutdown grace for in-flight worker checkins (default: 10)
  EVENT_LOG_PATH          Optional host-mounted JSONL path for direct event logging
  BENCHMARK_DURATION_S    Total benchmark duration (default: 300)
  WORKER_IMAGE            Worker container image (default: bench-worker:latest)
  WORKER_MEMORY_LIMIT_MB  Worker container memory limit (default: 2048)
  WORKER_MEMORY_MB        Memory each worker allocates (default: 500)
  WORKER_DURATION_MIN_S   Min worker lifetime (default: 30)
  WORKER_DURATION_MAX_S   Max worker lifetime (default: 120)
  WORKER_LIFETIME_MODE    timed (default) or hold
  PLATEAU_WORKERS_PER_AGENT  Non-decreasing comma-separated worker targets
  PLATEAU_HOLD_S          Seconds per plateau stage (default: 60)
  PLATEAU_SETTLE_S        Samples after this many seconds count as steady-state
  RNG_SEED                Base RNG seed for reproducibility (default: 42)
  BENCH_RUN_ID            Unique run identifier for container labels (default: unknown)
  BENCH_APPROACH          Approach name for container labels (default: unknown)
  WORKER_BACKEND          Backend for workers: "docker" (default) or "firecracker"
  FC_KERNEL_PATH          Firecracker kernel path (default: /opt/vmlinux)
  FC_ROOTFS_PATH          Firecracker rootfs path (default: /opt/worker-rootfs.ext4)
"""

import hashlib
import heapq
import http.client
import json
import mmap
import os
import random
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WORKER_BACKEND = os.environ.get("WORKER_BACKEND", "docker")

if WORKER_BACKEND == "docker":
    import docker


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AGENT_ID = os.environ.get("AGENT_ID", "agent-0")
BENCHMARK_MODE = os.environ.get("BENCHMARK_MODE", "loaded")
AGENT_BASELINE_MB = int(os.environ.get("AGENT_BASELINE_MB", "50"))
SPAWN_INTERVAL_MEAN_S = float(os.environ.get("SPAWN_INTERVAL_MEAN_S", "30"))
SPAWN_RAMP_BATCH_SIZE = int(os.environ.get("SPAWN_RAMP_BATCH_SIZE", "0"))
SPAWN_RAMP_INTERVAL_S = float(os.environ.get("SPAWN_RAMP_INTERVAL_S", "0"))
MAX_CONCURRENT_WORKERS = int(os.environ.get("MAX_CONCURRENT_WORKERS", "5"))
CHECKIN_GRACE_S = float(os.environ.get("CHECKIN_GRACE_S", "10"))
EVENT_LOG_PATH = os.environ.get("EVENT_LOG_PATH", "")
BENCHMARK_DURATION_S = int(os.environ.get("BENCHMARK_DURATION_S", "300"))
WORKER_IMAGE = os.environ.get("WORKER_IMAGE", "bench-worker:latest")
WORKER_MEMORY_LIMIT_MB = int(os.environ.get("WORKER_MEMORY_LIMIT_MB", "2048"))
WORKER_MEMORY_MB = int(os.environ.get("WORKER_MEMORY_MB", "500"))
WORKER_DURATION_MIN_S = int(os.environ.get("WORKER_DURATION_MIN_S", "30"))
WORKER_DURATION_MAX_S = int(os.environ.get("WORKER_DURATION_MAX_S", "120"))
WORKER_LIFETIME_MODE = os.environ.get("WORKER_LIFETIME_MODE", "timed")
RNG_SEED_BASE = int(os.environ.get("RNG_SEED", "42"))
BENCH_RUN_ID = os.environ.get("BENCH_RUN_ID", "unknown")
BENCH_APPROACH = os.environ.get("BENCH_APPROACH", "unknown")
WORKER_RUNTIME = os.environ.get("WORKER_RUNTIME", "")  # e.g. "runsc" for gVisor
WORKER_NETWORK = os.environ.get("WORKER_NETWORK", "")  # e.g. "podman" for bridge networking
WORKER_NETWORK_MODE = os.environ.get("WORKER_NETWORK_MODE", "")  # e.g. "container:bench-agent-0"
ORCHESTRATOR_PORT = os.environ.get("ORCHESTRATOR_PORT", "")  # HTTP server port (empty = disabled)
ORCHESTRATOR_HOST_PORT = os.environ.get("ORCHESTRATOR_HOST_PORT", "")  # Host-mapped port (container approaches)
PLATEAU_WORKERS_PER_AGENT = [
    int(v.strip())
    for v in os.environ.get("PLATEAU_WORKERS_PER_AGENT", "").split(",")
    if v.strip()
]
PLATEAU_HOLD_S = int(os.environ.get("PLATEAU_HOLD_S", "60"))
PLATEAU_SETTLE_S = int(os.environ.get("PLATEAU_SETTLE_S", "20"))

# Storage validation: opt-in via STORAGE_VALIDATION=1
STORAGE_VALIDATION = os.environ.get("STORAGE_VALIDATION", "").lower() in ("1", "true", "yes")
WORKSPACE_BASE = os.environ.get("WORKSPACE_BASE", "/tmp/bench-workspaces")
WORKSPACE_HOST_BASE = os.environ.get("WORKSPACE_HOST_BASE", "")  # host-visible prefix for shared-daemon approaches

# Derive per-agent seed: hash(base_seed + agent_id) for reproducibility
_seed_input = f"{RNG_SEED_BASE}:{AGENT_ID}"
AGENT_SEED = int(hashlib.sha256(_seed_input.encode()).hexdigest()[:8], 16)

WORKER_PREFIX = f"bench-worker-{AGENT_ID}"
STATUS_INTERVAL_S = 5  # How often to emit status events

# Firecracker-specific configuration
FC_KERNEL_PATH = os.environ.get("FC_KERNEL_PATH", "/opt/vmlinux")
FC_ROOTFS_PATH = os.environ.get("FC_ROOTFS_PATH", "/opt/worker-rootfs.ext4")
FC_VM_DIR = "/tmp/fc-vms"
FC_MAX_NETWORK_SLOTS = 16384  # /30 subnets available inside 10.0.0.0/16


# ---------------------------------------------------------------------------
# Structured event logging
# ---------------------------------------------------------------------------

_log_lock = threading.Lock()
_benchmark_start = threading.Event()
_current_plateau_index = -1
_current_plateau_target = 0
_event_log_fp = None
_event_log_path_error = False

if EVENT_LOG_PATH:
    try:
        _event_log_fp = open(EVENT_LOG_PATH, "a", encoding="utf-8", buffering=1)
    except OSError as exc:
        _event_log_path_error = True
        print(
            f"WARNING: failed to open EVENT_LOG_PATH={EVENT_LOG_PATH}: {exc}",
            file=sys.stderr,
            flush=True,
        )


def emit_event(event: str, **kwargs):
    """Emit a structured JSONL event to stdout and an optional host log."""
    record = {"t": time.time(), "event": event, "agent_id": AGENT_ID}
    record.update(kwargs)
    line = json.dumps(record)
    global _event_log_fp, _event_log_path_error
    with _log_lock:
        print(line, flush=True)
        if _event_log_fp is not None:
            try:
                _event_log_fp.write(line + "\n")
                _event_log_fp.flush()
            except OSError as exc:
                if not _event_log_path_error:
                    print(
                        f"WARNING: failed to write EVENT_LOG_PATH={EVENT_LOG_PATH}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    _event_log_path_error = True
                try:
                    _event_log_fp.close()
                except OSError:
                    pass
                _event_log_fp = None


def log(msg: str):
    """Emit a human-readable log line (also structured)."""
    emit_event("log", message=msg)


def set_plateau_stage(index: int, target: int) -> None:
    global _current_plateau_index, _current_plateau_target
    _current_plateau_index = index
    _current_plateau_target = target


def get_plateau_state() -> dict:
    return {
        "plateau_index": _current_plateau_index,
        "plateau_target_workers": _current_plateau_target,
    }


def _agent_ordinal() -> int:
    """Best-effort stable ordinal for per-agent launch staggering."""
    try:
        return int(AGENT_ID.rsplit("-", 1)[-1])
    except (IndexError, ValueError):
        return AGENT_SEED


def loaded_spawn_ramp_delay_s() -> float:
    """Return the opt-in loaded-mode ramp delay for this agent."""
    if BENCHMARK_MODE != "loaded":
        return 0.0
    if SPAWN_RAMP_BATCH_SIZE <= 0 or SPAWN_RAMP_INTERVAL_S <= 0:
        return 0.0
    return (_agent_ordinal() // SPAWN_RAMP_BATCH_SIZE) * SPAWN_RAMP_INTERVAL_S


# ---------------------------------------------------------------------------
# Orchestrator HTTP server
# ---------------------------------------------------------------------------

def _detect_docker_bridge_gateway() -> str:
    """Detect the Docker bridge gateway IP dynamically."""
    # Explicit override takes precedence
    if os.environ.get("DOCKER_BRIDGE_GATEWAY"):
        return os.environ["DOCKER_BRIDGE_GATEWAY"]
    # Try reading from /proc/net/route (default gateway = docker bridge on host network)
    try:
        import struct
        with open("/proc/net/route") as f:
            for line in f:
                fields = line.strip().split()
                if len(fields) >= 3 and fields[1] == "00000000":
                    gw_hex = fields[2]
                    gw_bytes = struct.pack("<I", int(gw_hex, 16))
                    return ".".join(str(b) for b in gw_bytes)
    except Exception:
        pass
    return "172.17.0.1"


DOCKER_BRIDGE_GATEWAY = _detect_docker_bridge_gateway()


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


# ---------------------------------------------------------------------------
# Storage validation tracking
# ---------------------------------------------------------------------------

_storage_results = {}  # worker_name → {"read_ok": bool, "write_ok": bool}
_storage_lock = threading.Lock()

# Spawn → checkin cold-start tracking
_spawn_timestamps = {}   # worker_name → time.time() at worker_start emit
_spawn_ts_lock = threading.Lock()


def record_storage_read(worker_id: str, read_ok: bool):
    """Record storage read result from worker checkin."""
    with _storage_lock:
        if worker_id not in _storage_results:
            _storage_results[worker_id] = {"read_ok": False, "write_ok": False}
        _storage_results[worker_id]["read_ok"] = read_ok


def record_storage_write(worker_name: str, write_ok: bool):
    """Record storage write result (agent-side verification after container.wait)."""
    with _storage_lock:
        if worker_name not in _storage_results:
            _storage_results[worker_name] = {"read_ok": False, "write_ok": False}
        _storage_results[worker_name]["write_ok"] = write_ok


def get_storage_summary() -> dict:
    """Return aggregate storage validation results."""
    with _storage_lock:
        tested = len(_storage_results)
        read_ok = sum(1 for r in _storage_results.values() if r["read_ok"])
        write_ok = sum(1 for r in _storage_results.values() if r["write_ok"])
    return {"workers_tested": tested, "read_ok": read_ok, "write_ok": write_ok}


def prepare_worker_workspace(worker_name: str):
    """Create a per-worker workspace dir with a challenge file.

    Returns (local_path, host_path, token) or (None, None, None) if
    storage validation is disabled.
    """
    if not STORAGE_VALIDATION:
        return None, None, None

    import secrets as _secrets
    token = _secrets.token_hex(16)
    local_path = os.path.join(WORKSPACE_BASE, worker_name)
    os.makedirs(local_path, exist_ok=True)
    # Make writable by worker (runs as UID 1000)
    os.chmod(local_path, 0o777)
    with open(os.path.join(local_path, "challenge.txt"), "w") as f:
        f.write(token)

    # Compute host-visible path for shared-daemon approaches
    if WORKSPACE_HOST_BASE:
        host_path = os.path.join(WORKSPACE_HOST_BASE, worker_name)
    else:
        host_path = local_path

    return local_path, host_path, token


def check_worker_reply(local_path: str, token: str) -> bool:
    """Check if the worker wrote the correct reply. Returns True on match."""
    if not local_path:
        return False
    reply_file = os.path.join(local_path, "reply.txt")
    try:
        with open(reply_file) as f:
            return f.read().strip() == token
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Firecracker: TAP device and ext4 workspace helpers
# ---------------------------------------------------------------------------

_fc_network_lock = threading.Lock()
_fc_free_network_slots = list(range(1, FC_MAX_NETWORK_SLOTS + 1))
heapq.heapify(_fc_free_network_slots)
_fc_network_slots_in_use = set()


def _allocate_fc_network_slot() -> int:
    with _fc_network_lock:
        if not _fc_free_network_slots:
            raise RuntimeError("No Firecracker network slots available")
        slot = heapq.heappop(_fc_free_network_slots)
        _fc_network_slots_in_use.add(slot)
        return slot


def _release_fc_network_slot(slot: int | None) -> None:
    if slot is None:
        return
    with _fc_network_lock:
        if slot in _fc_network_slots_in_use:
            _fc_network_slots_in_use.remove(slot)
            heapq.heappush(_fc_free_network_slots, slot)


def _fc_network_config(slot: int):
    """Map a slot to a unique /30 subnet inside 10.0.0.0/16."""
    subnet_index = slot - 1
    third_octet = subnet_index // 64
    fourth_octet = (subnet_index % 64) * 4
    tap_name = f"tap{slot}"
    host_ip = f"10.0.{third_octet}.{fourth_octet + 1}"
    guest_ip = f"10.0.{third_octet}.{fourth_octet + 2}"
    return tap_name, host_ip, guest_ip


def _fc_guest_mac(slot: int, counter: int) -> str:
    octets = (
        0x02,
        0xFC,
        (slot >> 8) & 0xFF,
        slot & 0xFF,
        (counter >> 8) & 0xFF,
        counter & 0xFF,
    )
    return ":".join(f"{octet:02x}" for octet in octets)


def _setup_tap_device(slot: int):
    """Create a TAP device for a Firecracker VM and assign IPs.

    Uses a /30 subnet per VM inside 10.0.0.0/16.
    Returns (tap_name, host_ip, guest_ip) or raises on failure.
    """
    tap_name, host_ip, guest_ip = _fc_network_config(slot)

    subprocess.run(
        ["ip", "tuntap", "add", tap_name, "mode", "tap"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["ip", "addr", "add", f"{host_ip}/30", "dev", tap_name],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["ip", "link", "set", tap_name, "up"],
        check=True, capture_output=True,
    )
    return tap_name, host_ip, guest_ip


def _cleanup_tap_device(tap_name: str):
    """Delete a TAP device, ignoring errors."""
    try:
        subprocess.run(
            ["ip", "link", "del", tap_name],
            capture_output=True,
        )
    except Exception:
        pass


def _create_workspace_ext4(ws_dir: str, token: str) -> str:
    """Create a small ext4 image with challenge.txt written via debugfs.

    Returns the path to the ext4 image file.
    """
    img_path = os.path.join(ws_dir, "workspace.ext4")
    challenge_tmp = os.path.join(ws_dir, "challenge.txt")

    # Create 4 MB image
    subprocess.run(
        ["dd", "if=/dev/zero", f"of={img_path}", "bs=1M", "count=4"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["mkfs.ext4", "-F", "-q", img_path],
        check=True, capture_output=True,
    )

    # Write challenge.txt into the image via debugfs (no mount needed)
    with open(challenge_tmp, "w") as f:
        f.write(token)
    subprocess.run(
        ["debugfs", "-w", "-R", f"write {challenge_tmp} challenge.txt", img_path],
        check=True, capture_output=True,
    )
    os.unlink(challenge_tmp)

    return img_path


def _read_reply_from_ext4(img_path: str):
    """Read reply.txt from an ext4 image via debugfs. Returns content or None."""
    try:
        # Replay the ext4 journal first — killed VMs may leave uncommitted
        # journal entries that debugfs cannot see without recovery.
        subprocess.run(
            ["e2fsck", "-fy", img_path],
            capture_output=True,
        )
        result = subprocess.run(
            ["debugfs", "-R", "cat reply.txt", img_path],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout.strip()
    except Exception:
        pass
    return None


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
        elif self.path == "/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            body = json.dumps({
                "active_workers": get_active_workers(),
                "benchmark_mode": BENCHMARK_MODE,
                "benchmark_started": _benchmark_start.is_set(),
                **get_plateau_state(),
            })
            self.wfile.write(body.encode())
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
            checkin_extra = {}
            # Compute cold-start latency (spawn → checkin)
            with _spawn_ts_lock:
                spawn_ts = _spawn_timestamps.get(worker_id)
            if spawn_ts is not None:
                cold_start_ms = (time.time() - spawn_ts) * 1000
                checkin_extra["cold_start_ms"] = round(cold_start_ms, 1)
            emit_event("checkin", worker_id=worker_id, total_checkins=total,
                       **checkin_extra)
            # Record storage read result if present
            if "storage_read_ok" in data:
                record_storage_read(worker_id, bool(data["storage_read_ok"]))
            self._respond_ok()
        elif self.path == "/control/start":
            _benchmark_start.set()
            emit_event("benchmark_start_signal")
            self._respond_ok()
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        # Suppress default stderr logging; we emit structured events instead
        pass


class BenchmarkHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128


def start_orchestrator_server(port: int) -> ThreadingHTTPServer:
    """Start the orchestrator HTTP server in a background thread."""
    server = BenchmarkHTTPServer(("0.0.0.0", port), OrchestratorHandler)
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


def spawn_worker(client, rng: random.Random) -> bool:
    """
    Spawn a single worker container using the ironclaw lifecycle:
    create → start → wait → remove.

    Returns True if the container was created and started successfully.
    The wait/remove happens in a background thread.
    """
    counter, worker_name = next_worker_id()

    # Prepare workspace directory for storage validation
    ws_local, ws_host, ws_token = prepare_worker_workspace(worker_name)

    # Container config mirrors ironclaw sandbox defaults
    env = {
        "WORKER_NAME": worker_name,
        "WORKER_MEMORY_MB": str(WORKER_MEMORY_MB),
        "WORKER_DURATION_MIN_S": str(WORKER_DURATION_MIN_S),
        "WORKER_DURATION_MAX_S": str(WORKER_DURATION_MAX_S),
        "WORKER_LIFETIME_MODE": WORKER_LIFETIME_MODE,
    }
    orchestrator_url = compute_orchestrator_url()
    if orchestrator_url:
        env["ORCHESTRATOR_URL"] = orchestrator_url
    if ws_token:
        env["STORAGE_CHALLENGE"] = ws_token

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
    # Bind-mount workspace if storage validation is active
    if ws_host:
        create_kwargs["volumes"] = {ws_host: {"bind": "/workspace", "mode": "rw"}}
    if WORKER_RUNTIME:
        create_kwargs["runtime"] = WORKER_RUNTIME
    if WORKER_NETWORK:
        create_kwargs["network"] = WORKER_NETWORK
    if WORKER_NETWORK_MODE:
        create_kwargs["network_mode"] = WORKER_NETWORK_MODE

    try:
        t0 = time.monotonic()
        container = client.containers.create(**create_kwargs)
        t1 = time.monotonic()
    except docker.errors.APIError as e:
        log(f"Failed to create {worker_name}: {e}")
        return False

    try:
        container.start()
        t2 = time.monotonic()
    except docker.errors.APIError as e:
        log(f"Failed to start {worker_name}: {e}")
        try:
            container.remove(force=True)
        except Exception:
            pass
        return False

    create_ms = (t1 - t0) * 1000
    start_ms = (t2 - t1) * 1000
    total_ms = (t2 - t0) * 1000
    emit_event("worker_spawn_timing", worker_id=worker_name,
               create_ms=round(create_ms, 1),
               start_ms=round(start_ms, 1),
               total_ms=round(total_ms, 1))

    count = _inc_active()
    emit_event("worker_start", worker_id=worker_name, active_workers=count)

    # Record spawn timestamp for cold-start tracking
    with _spawn_ts_lock:
        _spawn_timestamps[worker_name] = time.time()

    # Wait and remove in background thread (matches ironclaw's async wait)
    def wait_and_remove():
        try:
            container.wait()
        except Exception:
            pass
        # Check storage reply after container exits (proves write persisted)
        if ws_local and ws_token:
            write_ok = check_worker_reply(ws_local, ws_token)
            record_storage_write(worker_name, write_ok)
        try:
            container.remove(force=True)
        except Exception:
            pass
        # Clean up workspace directory
        if ws_local:
            try:
                import shutil
                shutil.rmtree(ws_local, ignore_errors=True)
            except Exception:
                pass
        count = _dec_active()
        emit_event("worker_end", worker_id=worker_name, active_workers=count)

    t = threading.Thread(target=wait_and_remove, daemon=True)
    t.start()
    return True


# ---------------------------------------------------------------------------
# Firecracker: HTTP-over-Unix-socket helper
# ---------------------------------------------------------------------------

class _UnixSocketHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection subclass that connects via a Unix domain socket."""

    def __init__(self, socket_path, timeout=5):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


def _fc_api(socket_path, method, path, body=None):
    """Send an HTTP request to the Firecracker API over a Unix socket."""
    conn = _UnixSocketHTTPConnection(socket_path)
    headers = {"Content-Type": "application/json"} if body else {}
    payload = json.dumps(body) if body else None
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    if resp.status >= 300:
        raise RuntimeError(f"Firecracker API {method} {path} returned {resp.status}: {data}")
    return data


# ---------------------------------------------------------------------------
# Firecracker: worker spawn and lifecycle
# ---------------------------------------------------------------------------

def spawn_worker_firecracker(rng: random.Random) -> bool:
    """
    Spawn a single worker as a Firecracker microVM.

    1. Create a per-VM directory with a Unix socket
    2. Set up TAP device for networking (if orchestrator is configured)
    3. Create ext4 workspace image (if storage validation is enabled)
    4. Launch the firecracker VMM process
    5. Configure the VM via the Firecracker API (boot source, drives, network, machine config)
    6. Start the VM instance
    7. Background thread waits for the VMM process to exit, verifies storage, cleans up

    Returns True if the VM was started successfully.
    """
    counter, worker_name = next_worker_id()
    vm_dir = os.path.join(FC_VM_DIR, worker_name)
    socket_path = os.path.join(vm_dir, "fc.sock")

    try:
        os.makedirs(vm_dir, exist_ok=True)
    except OSError as e:
        log(f"Failed to create VM dir {vm_dir}: {e}")
        return False

    # Set up TAP device for networking
    tap_name = None
    host_ip = None
    guest_ip = None
    net_slot = None
    if ORCHESTRATOR_PORT:
        try:
            net_slot = _allocate_fc_network_slot()
            tap_name, host_ip, guest_ip = _setup_tap_device(net_slot)
        except Exception as e:
            log(f"Failed to set up TAP device for {worker_name}: {e}")
            _release_fc_network_slot(net_slot)
            try:
                import shutil
                shutil.rmtree(vm_dir, ignore_errors=True)
            except Exception:
                pass
            return False

    # Prepare workspace ext4 image for storage validation
    ws_token = None
    ws_img_path = None
    if STORAGE_VALIDATION:
        import secrets as _secrets
        ws_token = _secrets.token_hex(16)
        try:
            ws_img_path = _create_workspace_ext4(vm_dir, ws_token)
        except Exception as e:
            log(f"Failed to create workspace ext4 for {worker_name}: {e}")
            if tap_name:
                _cleanup_tap_device(tap_name)
            _release_fc_network_slot(net_slot)
            try:
                import shutil
                shutil.rmtree(vm_dir, ignore_errors=True)
            except Exception:
                pass
            return False

    # Launch the Firecracker VMM process
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            ["firecracker", "--api-sock", socket_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, FileNotFoundError) as e:
        log(f"Failed to launch firecracker for {worker_name}: {e}")
        if tap_name:
            _cleanup_tap_device(tap_name)
        _release_fc_network_slot(net_slot)
        return False

    # Wait for the API socket to appear
    for _ in range(50):  # 5 seconds max
        if os.path.exists(socket_path):
            break
        time.sleep(0.1)
    else:
        log(f"Firecracker socket never appeared for {worker_name}")
        proc.kill()
        if tap_name:
            _cleanup_tap_device(tap_name)
        _release_fc_network_slot(net_slot)
        return False

    # Compute worker memory: requested + 128MB headroom for guest kernel + Python runtime
    vm_mem_mib = WORKER_MEMORY_MB + 128

    # Build boot_args to pass worker config via /proc/cmdline
    boot_args = (
        f"console=ttyS0 reboot=k panic=1 pci=off "
        f"init=/sbin/init "
        f"worker_memory_mb={WORKER_MEMORY_MB} "
        f"worker_duration_min_s={WORKER_DURATION_MIN_S} "
        f"worker_duration_max_s={WORKER_DURATION_MAX_S} "
        f"worker_lifetime_mode={WORKER_LIFETIME_MODE}"
    )

    # Add networking config via kernel ip= parameter
    if guest_ip and host_ip:
        # ip=<client-ip>:<server-ip>:<gw-ip>:<netmask>:<hostname>:<device>:<autoconf>
        boot_args += f" ip={guest_ip}::{host_ip}:255.255.255.252::eth0:off"
        boot_args += f" orchestrator_url=http://{host_ip}:{ORCHESTRATOR_PORT}"
        boot_args += f" worker_name={worker_name}"

    # Add storage challenge token
    if ws_token:
        boot_args += f" storage_challenge={ws_token}"
        # Ensure worker_name is in boot_args even without networking
        if not guest_ip:
            boot_args += f" worker_name={worker_name}"

    try:
        # Configure boot source
        _fc_api(socket_path, "PUT", "/boot-source", {
            "kernel_image_path": FC_KERNEL_PATH,
            "boot_args": boot_args,
        })

        # Configure rootfs drive (read-only)
        _fc_api(socket_path, "PUT", "/drives/rootfs", {
            "drive_id": "rootfs",
            "path_on_host": FC_ROOTFS_PATH,
            "is_root_device": True,
            "is_read_only": True,
        })

        # Configure workspace drive (read-write) if storage validation is active
        if ws_img_path:
            _fc_api(socket_path, "PUT", "/drives/workspace", {
                "drive_id": "workspace",
                "path_on_host": ws_img_path,
                "is_root_device": False,
                "is_read_only": False,
            })

        # Configure network interface if TAP is set up
        if tap_name:
            _fc_api(socket_path, "PUT", "/network-interfaces/eth0", {
                "iface_id": "eth0",
                "host_dev_name": tap_name,
                "guest_mac": _fc_guest_mac(net_slot, counter),
            })

        # Configure machine resources
        _fc_api(socket_path, "PUT", "/machine-config", {
            "vcpu_count": 1,
            "mem_size_mib": vm_mem_mib,
        })

        # Start the VM instance
        _fc_api(socket_path, "PUT", "/actions", {
            "action_type": "InstanceStart",
        })
    except Exception as e:
        log(f"Failed to configure/start VM {worker_name}: {e}")
        proc.kill()
        if tap_name:
            _cleanup_tap_device(tap_name)
        _release_fc_network_slot(net_slot)
        try:
            import shutil
            shutil.rmtree(vm_dir, ignore_errors=True)
        except Exception:
            pass
        return False

    t1 = time.monotonic()
    total_ms = (t1 - t0) * 1000
    emit_event("worker_spawn_timing", worker_id=worker_name,
               total_ms=round(total_ms, 1))

    count = _inc_active()
    emit_event("worker_start", worker_id=worker_name, active_workers=count)

    # Record spawn timestamp for cold-start tracking
    with _spawn_ts_lock:
        _spawn_timestamps[worker_name] = time.time()

    # Background thread: wait for VMM process to exit, verify storage, clean up
    def wait_and_cleanup():
        try:
            proc.wait()
        except Exception:
            pass

        # Verify storage write after VM exits
        if ws_img_path and ws_token:
            reply = _read_reply_from_ext4(ws_img_path)
            write_ok = reply == ws_token
            record_storage_write(worker_name, write_ok)

        count = _dec_active()
        emit_event("worker_end", worker_id=worker_name, active_workers=count)

        # Clean up TAP device
        if tap_name:
            _cleanup_tap_device(tap_name)
        _release_fc_network_slot(net_slot)

        # Clean up VM directory
        try:
            import shutil
            shutil.rmtree(vm_dir, ignore_errors=True)
        except Exception:
            pass

    t = threading.Thread(target=wait_and_cleanup, daemon=True)
    t.start()
    return True


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup_workers_docker(client):
    """Remove all worker containers for this agent (Docker backend)."""
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


def cleanup_workers_firecracker():
    """Kill any remaining Firecracker VMM child processes."""
    log("Cleaning up Firecracker VMs...")
    killed = 0
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(os.getpid()), "firecracker"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            for pid_str in result.stdout.strip().split("\n"):
                try:
                    os.kill(int(pid_str), signal.SIGKILL)
                    killed += 1
                except (ProcessLookupError, ValueError):
                    pass
    except Exception as e:
        log(f"Cleanup error: {e}")
    if killed:
        log(f"Killed {killed} Firecracker processes.")
        # Wait for wait_and_cleanup threads to read storage results from
        # workspace images before removing VM directories.
        time.sleep(2)
    # Clean up VM directories
    try:
        import shutil
        if os.path.isdir(FC_VM_DIR):
            shutil.rmtree(FC_VM_DIR, ignore_errors=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Status emitter (periodic)
# ---------------------------------------------------------------------------

def status_emitter(stop_event: threading.Event):
    """Periodically emit status events with active worker count."""
    while not stop_event.is_set():
        emit_event("status", active_workers=get_active_workers(), **get_plateau_state())
        # Emit periodic checkin_summary so approaches that can't deliver
        # SIGTERM (rootless Podman) still have recent validation data.
        if ORCHESTRATOR_PORT and _worker_counter > 0:
            checkins = get_checkin_count()
            emit_event("checkin_summary",
                       workers_spawned=_worker_counter,
                       checkins_received=checkins,
                       checkins_ok=checkins == _worker_counter)
        # Emit periodic storage_summary when storage validation is active
        if STORAGE_VALIDATION and _worker_counter > 0:
            emit_event("storage_summary", **get_storage_summary())
        stop_event.wait(timeout=STATUS_INTERVAL_S)


# ---------------------------------------------------------------------------
# Main spawn loop
# ---------------------------------------------------------------------------

def validate_config() -> None:
    if BENCHMARK_MODE not in ("loaded", "idle", "plateau"):
        log(f"Unknown BENCHMARK_MODE={BENCHMARK_MODE}, exiting.")
        sys.exit(1)
    if SPAWN_RAMP_BATCH_SIZE < 0:
        log("SPAWN_RAMP_BATCH_SIZE must be >= 0.")
        sys.exit(1)
    if SPAWN_RAMP_INTERVAL_S < 0:
        log("SPAWN_RAMP_INTERVAL_S must be >= 0.")
        sys.exit(1)
    if (SPAWN_RAMP_BATCH_SIZE == 0) != (SPAWN_RAMP_INTERVAL_S == 0):
        log("SPAWN_RAMP_BATCH_SIZE and SPAWN_RAMP_INTERVAL_S must be set together.")
        sys.exit(1)
    if CHECKIN_GRACE_S < 0:
        log("CHECKIN_GRACE_S must be >= 0.")
        sys.exit(1)
    if WORKER_LIFETIME_MODE not in ("timed", "hold"):
        log(f"Unknown WORKER_LIFETIME_MODE={WORKER_LIFETIME_MODE}, exiting.")
        sys.exit(1)
    if BENCHMARK_MODE == "plateau":
        if not ORCHESTRATOR_PORT:
            log("Plateau mode requires ORCHESTRATOR_PORT for synchronized start.")
            sys.exit(1)
        if not PLATEAU_WORKERS_PER_AGENT:
            log("Plateau mode requires PLATEAU_WORKERS_PER_AGENT.")
            sys.exit(1)
        if PLATEAU_WORKERS_PER_AGENT[0] != 0:
            log("Plateau mode requires the first target to be 0 workers.")
            sys.exit(1)
        if any(target < 0 for target in PLATEAU_WORKERS_PER_AGENT):
            log("Plateau mode does not allow negative worker targets.")
            sys.exit(1)
        if any(
            later < earlier
            for earlier, later in zip(
                PLATEAU_WORKERS_PER_AGENT, PLATEAU_WORKERS_PER_AGENT[1:]
            )
        ):
            log("Plateau mode currently requires a non-decreasing worker schedule.")
            sys.exit(1)
        if max(PLATEAU_WORKERS_PER_AGENT) > MAX_CONCURRENT_WORKERS:
            log("Plateau schedule exceeds MAX_CONCURRENT_WORKERS.")
            sys.exit(1)
        if PLATEAU_HOLD_S <= 0:
            log("PLATEAU_HOLD_S must be > 0.")
            sys.exit(1)
        if PLATEAU_SETTLE_S < 0 or PLATEAU_SETTLE_S >= PLATEAU_HOLD_S:
            log("PLATEAU_SETTLE_S must be >= 0 and < PLATEAU_HOLD_S.")
            sys.exit(1)
        if WORKER_LIFETIME_MODE != "hold":
            log("Plateau mode requires WORKER_LIFETIME_MODE=hold.")
            sys.exit(1)


def maybe_spawn_worker(client, rng: random.Random) -> bool:
    if WORKER_BACKEND == "firecracker":
        return spawn_worker_firecracker(rng)
    return spawn_worker(client, rng)


def wait_for_benchmark_start(stop: threading.Event) -> bool:
    # Synchronize all modes so large-N startup time does not overlap the
    # measurement window differently for early and late agents.
    log(f"{BENCHMARK_MODE.capitalize()} mode armed. Waiting for orchestrator start signal.")
    while not stop.is_set():
        if _benchmark_start.wait(timeout=0.5):
            return True
    return False


def run_loaded_loop(stop: threading.Event, client, rng: random.Random) -> None:
    start_time = time.monotonic()
    ramp_delay_s = loaded_spawn_ramp_delay_s()
    if ramp_delay_s > 0:
        emit_event(
            "spawn_ramp_delay",
            ramp_delay_s=round(ramp_delay_s, 3),
            ramp_batch_size=SPAWN_RAMP_BATCH_SIZE,
            ramp_interval_s=SPAWN_RAMP_INTERVAL_S,
            agent_ordinal=_agent_ordinal(),
        )
        log(
            "Applying loaded-mode spawn ramp delay "
            f"({ramp_delay_s:.1f}s, batch={SPAWN_RAMP_BATCH_SIZE}, "
            f"interval={SPAWN_RAMP_INTERVAL_S}s)."
        )
        if stop.wait(timeout=ramp_delay_s):
            return
    log("Entering stochastic spawn loop.")

    while not stop.is_set():
        elapsed = time.monotonic() - start_time
        if elapsed >= BENCHMARK_DURATION_S:
            log(f"Benchmark duration reached ({elapsed:.0f}s). Stopping.")
            break

        delay = rng.expovariate(1.0 / SPAWN_INTERVAL_MEAN_S)
        if stop.wait(timeout=delay):
            break

        active = get_active_workers()
        if active >= MAX_CONCURRENT_WORKERS:
            log(f"At capacity ({active}/{MAX_CONCURRENT_WORKERS}). Skipping spawn.")
            continue

        log(f"Spawning worker (active: {active}/{MAX_CONCURRENT_WORKERS})")
        maybe_spawn_worker(client, rng)


def run_plateau_loop(stop: threading.Event, client, rng: random.Random) -> None:
    log(
        "Entering plateau loop: "
        f"targets={PLATEAU_WORKERS_PER_AGENT} hold_s={PLATEAU_HOLD_S} "
        f"settle_s={PLATEAU_SETTLE_S}"
    )
    benchmark_start = time.monotonic()

    for stage_index, target in enumerate(PLATEAU_WORKERS_PER_AGENT):
        if stop.is_set():
            break

        set_plateau_stage(stage_index, target)
        emit_event(
            "plateau_stage_start",
            plateau_index=stage_index,
            target_workers=target,
            hold_s=PLATEAU_HOLD_S,
            settle_s=PLATEAU_SETTLE_S,
        )
        stage_deadline = time.monotonic() + PLATEAU_HOLD_S

        while not stop.is_set() and time.monotonic() < stage_deadline:
            active = get_active_workers()
            if active < target:
                log(f"Plateau {stage_index}: filling to target ({active}/{target})")
                if not maybe_spawn_worker(client, rng):
                    remaining = max(0.0, stage_deadline - time.monotonic())
                    stop.wait(timeout=min(1.0, remaining))
                    continue
                remaining = max(0.0, stage_deadline - time.monotonic())
                stop.wait(timeout=min(0.5, remaining))
                continue

            remaining = max(0.0, stage_deadline - time.monotonic())
            stop.wait(timeout=min(1.0, remaining))

        emit_event(
            "plateau_stage_end",
            plateau_index=stage_index,
            target_workers=target,
            active_workers=get_active_workers(),
        )

    elapsed = time.monotonic() - benchmark_start
    log(f"Plateau schedule complete ({elapsed:.0f}s). Stopping.")


def main():
    # Resolve DOCKER_BRIDGE_GATEWAY=auto to this container's own IP.
    # Used with Podman CNI bridge where containers share a bridge network
    # and workers reach the agent via its container IP, not the host.
    global DOCKER_BRIDGE_GATEWAY
    if DOCKER_BRIDGE_GATEWAY == "auto":
        try:
            DOCKER_BRIDGE_GATEWAY = socket.gethostbyname(socket.gethostname())
        except socket.gaierror:
            DOCKER_BRIDGE_GATEWAY = "172.17.0.1"

    # Set up reproducible RNG
    rng = random.Random(AGENT_SEED)
    validate_config()

    emit_event("agent_start",
               benchmark_mode=BENCHMARK_MODE,
               baseline_mb=AGENT_BASELINE_MB,
               spawn_interval_mean_s=SPAWN_INTERVAL_MEAN_S,
               max_concurrent_workers=MAX_CONCURRENT_WORKERS,
               duration_s=BENCHMARK_DURATION_S,
               rng_seed=AGENT_SEED,
               worker_runtime=WORKER_RUNTIME or "default",
               worker_backend=WORKER_BACKEND,
               worker_lifetime_mode=WORKER_LIFETIME_MODE,
               plateau_workers_per_agent=PLATEAU_WORKERS_PER_AGENT,
               plateau_hold_s=PLATEAU_HOLD_S,
               plateau_settle_s=PLATEAU_SETTLE_S,
               orchestrator_port=ORCHESTRATOR_PORT or "disabled",
               orchestrator_url=compute_orchestrator_url() or "disabled")

    # Allocate baseline memory
    log(f"Allocating {AGENT_BASELINE_MB} MB baseline memory...")
    _baseline_mem = allocate_baseline(AGENT_BASELINE_MB)
    log(f"Baseline memory allocated.")

    # Backend-specific init
    client = None
    if WORKER_BACKEND == "docker":
        client = docker.from_env()
    elif WORKER_BACKEND == "firecracker":
        os.makedirs(FC_VM_DIR, exist_ok=True)
        log(f"Firecracker backend: kernel={FC_KERNEL_PATH}, rootfs={FC_ROOTFS_PATH}")
    else:
        log(f"Unknown WORKER_BACKEND={WORKER_BACKEND}, exiting.")
        sys.exit(1)

    # Create workspace base directory for storage validation
    if STORAGE_VALIDATION:
        os.makedirs(WORKSPACE_BASE, exist_ok=True)
        log(f"Storage validation enabled: base={WORKSPACE_BASE}, "
            f"host_base={WORKSPACE_HOST_BASE or '(same)'}")

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

    if not wait_for_benchmark_start(stop):
        log("Stopped before benchmark start signal arrived.")
    elif BENCHMARK_MODE == "plateau":
        run_plateau_loop(stop, client, rng)
    else:
        run_loaded_loop(stop, client, rng)

    # Cleanup
    stop.set()

    # Grace period: let in-flight workers finish their checkin before killing them.
    # Workers allocate memory (~1-2s) then immediately POST /checkin.
    if ORCHESTRATOR_PORT and get_active_workers() > 0:
        grace_s = int(CHECKIN_GRACE_S) if CHECKIN_GRACE_S.is_integer() else CHECKIN_GRACE_S
        log(f"Waiting {grace_s}s for {get_active_workers()} in-flight workers to check in...")
        deadline = time.monotonic() + CHECKIN_GRACE_S
        while time.monotonic() < deadline and get_checkin_count() < _worker_counter:
            time.sleep(0.5)

    # Emit final checkin/storage summaries BEFORE cleanup so they're flushed
    # even if the process is killed during slow container removal.
    if ORCHESTRATOR_PORT:
        checkins = get_checkin_count()
        emit_event("checkin_summary",
                   workers_spawned=_worker_counter,
                   checkins_received=checkins,
                   checkins_ok=checkins == _worker_counter)

    if STORAGE_VALIDATION and _worker_counter > 0:
        emit_event("storage_summary", **get_storage_summary())

    if WORKER_BACKEND == "firecracker":
        cleanup_workers_firecracker()
    else:
        cleanup_workers_docker(client)

    emit_event("agent_stop", total_workers_spawned=_worker_counter)
    log("Agent stopped.")


if __name__ == "__main__":
    main()
