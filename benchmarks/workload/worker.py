#!/usr/bin/env python3
"""
Worker memory simulator for isolation benchmarks.

Simulates a oneshot worker task: allocates a configurable amount of memory,
touches every page to ensure physical allocation, holds for a random duration,
then exits. Designed to run inside a Docker container with --rm.

Environment variables:
    WORKER_MEMORY_MB       Memory to allocate and touch (default: 500)
    WORKER_DURATION_MIN_S  Minimum hold duration in seconds (default: 30)
    WORKER_DURATION_MAX_S  Maximum hold duration in seconds (default: 120)
"""

import json
import mmap
import os
import random
import socket
import sys
import threading
import time
import urllib.request

PAGE_SIZE = os.sysconf("SC_PAGESIZE")  # Usually 4096


def get_rss_kb() -> int:
    """Read RSS from /proc/self/statm in KiB."""
    try:
        with open("/proc/self/statm") as f:
            parts = f.read().split()
            rss_pages = int(parts[1])
            return rss_pages * PAGE_SIZE // 1024
    except (FileNotFoundError, IndexError):
        return -1


HEARTBEAT_INTERVAL_S = 5
ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "")
WORKER_NAME = os.environ.get("WORKER_NAME", "")


def heartbeat_loop(stop_event: threading.Event):
    """Send periodic heartbeats to the orchestrator. Best-effort: failures are logged, not fatal."""
    worker_id = WORKER_NAME or socket.gethostname()
    url = f"{ORCHESTRATOR_URL}/heartbeat"
    while not stop_event.is_set():
        try:
            payload = json.dumps({
                "worker_id": worker_id,
                "rss_kb": get_rss_kb(),
            }).encode()
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            print(f"[worker] heartbeat failed: {e}", flush=True)
        stop_event.wait(timeout=HEARTBEAT_INTERVAL_S)


CHECKIN_MAX_RETRIES = 3
CHECKIN_RETRY_DELAY_S = 2

# Storage validation: if STORAGE_CHALLENGE is set, verify workspace bind-mount
STORAGE_CHALLENGE = os.environ.get("STORAGE_CHALLENGE", "")


def do_storage_validation() -> bool:
    """Validate workspace bind-mount by reading challenge and writing reply.

    Returns True if the challenge file was read and matched STORAGE_CHALLENGE.
    When storage validation is not active (no STORAGE_CHALLENGE), returns False.
    """
    if not STORAGE_CHALLENGE:
        return False

    read_ok = False
    try:
        with open("/workspace/challenge.txt") as f:
            token = f.read().strip()
        if token == STORAGE_CHALLENGE:
            read_ok = True
            print("[worker] Storage read OK: challenge matched", flush=True)
        else:
            print(f"[worker] Storage read MISMATCH: expected {STORAGE_CHALLENGE!r}, "
                  f"got {token!r}", flush=True)
    except Exception as e:
        print(f"[worker] Storage read FAILED: {e}", flush=True)

    # Write reply regardless — proves write capability even if read mismatched.
    # fsync ensures data reaches the block device (important for Firecracker VMs
    # where the host reads the ext4 image via debugfs after the VM is killed).
    try:
        with open("/workspace/reply.txt", "w") as f:
            f.write(STORAGE_CHALLENGE)
            f.flush()
            os.fsync(f.fileno())
        print("[worker] Storage write OK: reply.txt written", flush=True)
    except Exception as e:
        print(f"[worker] Storage write FAILED: {e}", flush=True)

    return read_ok


def do_checkin(storage_read_ok: bool = False):
    """Send a single mandatory checkin to the orchestrator. Retries on failure."""
    worker_id = WORKER_NAME or socket.gethostname()
    url = f"{ORCHESTRATOR_URL}/checkin"
    payload_dict = {"worker_id": worker_id}
    if STORAGE_CHALLENGE:
        payload_dict["storage_read_ok"] = storage_read_ok
    payload = json.dumps(payload_dict).encode()
    for attempt in range(1, CHECKIN_MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
            print(f"[worker] Checked in with orchestrator (attempt {attempt})", flush=True)
            return
        except Exception as e:
            print(f"[worker] Checkin attempt {attempt}/{CHECKIN_MAX_RETRIES} failed: {e}", flush=True)
            if attempt < CHECKIN_MAX_RETRIES:
                time.sleep(CHECKIN_RETRY_DELAY_S)
    print("[worker] WARNING: All checkin attempts failed", flush=True)


def allocate_and_touch(size_mb: int) -> mmap.mmap:
    """Allocate anonymous memory and touch every page to force physical allocation."""
    size_bytes = size_mb * 1024 * 1024
    # MAP_ANONYMOUS + MAP_PRIVATE = anonymous private mapping
    mm = mmap.mmap(-1, size_bytes, mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
    # Touch every page to ensure RSS reflects real physical allocation
    for offset in range(0, size_bytes, PAGE_SIZE):
        mm[offset] = offset & 0xFF
    return mm


def main():
    memory_mb = int(os.environ.get("WORKER_MEMORY_MB", "500"))
    duration_min = int(os.environ.get("WORKER_DURATION_MIN_S", "30"))
    duration_max = int(os.environ.get("WORKER_DURATION_MAX_S", "120"))

    duration = random.uniform(duration_min, duration_max)

    # Start heartbeat thread if orchestrator URL is configured
    stop_heartbeat = threading.Event()
    if ORCHESTRATOR_URL:
        print(f"[worker] Heartbeat enabled → {ORCHESTRATOR_URL}", flush=True)
        hb_thread = threading.Thread(
            target=heartbeat_loop, args=(stop_heartbeat,), daemon=True
        )
        hb_thread.start()

    print(f"[worker] Allocating {memory_mb} MB...", flush=True)
    mem = allocate_and_touch(memory_mb)
    rss = get_rss_kb()
    print(
        f"[worker] Allocated. RSS={rss} KiB ({rss // 1024} MiB). "
        f"Holding for {duration:.0f}s.",
        flush=True,
    )

    # Storage validation: proves workspace bind-mount works (read + write)
    storage_read_ok = do_storage_validation()

    # Mandatory checkin: proves the network path to the orchestrator works
    if ORCHESTRATOR_URL:
        do_checkin(storage_read_ok=storage_read_ok)

    time.sleep(duration)

    stop_heartbeat.set()
    mem.close()
    print("[worker] Done, exiting.", flush=True)


if __name__ == "__main__":
    main()
