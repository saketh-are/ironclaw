"""
Memory and CPU time-series collector for isolation benchmarks.

Runs on the host, samples at configurable intervals:
  1. Host /proc/meminfo — full breakdown (consumed, cached, slab, swap, etc.)
  2. Per-entity RSS + PSS via /proc/<pid>/smaps_rollup
  3. Daemon (dockerd, containerd) RSS/PSS
  4. Active worker count from the approach plugin
  5. Optional: /proc/pressure/memory (PSI) and /proc/vmstat swap counters
  6. Host CPU counters from /proc/stat
  7. Per-process CPU counters from /proc/<pid>/stat

Writes JSONL to a file, one sample per line.
"""

import json
import threading
import time
from typing import Callable, Dict, Optional, TextIO


# Fields to extract from /proc/meminfo (all in KiB)
MEMINFO_FIELDS = {
    "MemTotal",
    "MemFree",
    "MemAvailable",
    "Buffers",
    "Cached",
    "SwapTotal",
    "SwapFree",
    "Slab",
    "SReclaimable",
    "SUnreclaim",
    "AnonPages",
    "Shmem",
}


def read_meminfo() -> Dict[str, int]:
    """Read /proc/meminfo, return selected fields in KiB."""
    result = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                key = parts[0].rstrip(":")
                if key in MEMINFO_FIELDS:
                    result[key] = int(parts[1])  # KiB
    except FileNotFoundError:
        pass
    return result


def read_smaps(pid: int) -> Optional[Dict[str, int]]:
    """
    Read RSS and PSS from /proc/<pid>/smaps_rollup, return KiB.
    PSS (Proportional Set Size) is more accurate than RSS when
    there are shared mappings (shared libs, etc.).
    Returns None on error.
    """
    result = {}
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                if line.startswith("Rss:"):
                    result["rss_kb"] = int(line.split()[1])
                elif line.startswith("Pss:"):
                    result["pss_kb"] = int(line.split()[1])
    except (FileNotFoundError, PermissionError):
        return None
    return result if result else None


def read_vmstat_swap() -> Dict[str, int]:
    """Read swap activity counters from /proc/vmstat."""
    result = {}
    try:
        with open("/proc/vmstat") as f:
            for line in f:
                if line.startswith("pswpin ") or line.startswith("pswpout "):
                    parts = line.split()
                    result[parts[0]] = int(parts[1])
    except FileNotFoundError:
        pass
    return result


def read_memory_pressure() -> Optional[str]:
    """Read /proc/pressure/memory (PSI) if available. Returns the 'some' line."""
    try:
        with open("/proc/pressure/memory") as f:
            return f.readline().strip()
    except (FileNotFoundError, PermissionError):
        return None


# Fields from the first line of /proc/stat (cumulative jiffies)
HOST_CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")


def read_host_cpu() -> Dict[str, int]:
    """Read /proc/stat aggregate CPU line. Returns cumulative jiffies per field."""
    try:
        with open("/proc/stat") as f:
            line = f.readline()  # first line: "cpu  <user> <nice> ..."
            parts = line.split()
            if parts[0] == "cpu":
                values = [int(v) for v in parts[1 : 1 + len(HOST_CPU_FIELDS)]]
                return dict(zip(HOST_CPU_FIELDS, values))
    except (FileNotFoundError, PermissionError, ValueError):
        pass
    return {}


def read_process_cpu(pid: int) -> Optional[Dict[str, int]]:
    """
    Read utime and stime from /proc/<pid>/stat (fields 14 and 15, 1-indexed).
    Returns {"utime": N, "stime": N} in jiffies, or None on error.
    """
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
        # Fields are space-separated, but comm (field 2) may contain spaces
        # and is enclosed in parens. Skip past the closing paren.
        close_paren = data.rfind(")")
        fields_after_comm = data[close_paren + 2 :].split()
        # fields_after_comm[0] = state (field 3), so utime = index 11, stime = index 12
        utime = int(fields_after_comm[11])
        stime = int(fields_after_comm[12])
        return {"utime": utime, "stime": stime}
    except (FileNotFoundError, PermissionError, ValueError, IndexError):
        return None


class Collector:
    """Collects memory time-series data."""

    def __init__(self, interval_ms: int = 500, phase: str = "running"):
        self.interval_s = interval_ms / 1000.0
        self.start_time = time.monotonic()
        self.phase = phase
        self._stop = threading.Event()

    def sample(
        self,
        get_agent_pids: Callable[[], Dict[str, int]],
        get_daemon_pids: Callable[[], Dict[str, int]],
        count_workers: Callable[[], int],
    ) -> dict:
        """Take one measurement sample."""
        meminfo = read_meminfo()

        # Host-level consumed memory (primary metric, backward-compatible)
        mem_total = meminfo.get("MemTotal", 0)
        mem_available = meminfo.get("MemAvailable", 0)
        host_consumed_kb = mem_total - mem_available

        sample = {
            "timestamp_s": round(time.monotonic() - self.start_time, 3),
            "phase": self.phase,
            "host_consumed_kb": host_consumed_kb,
            "meminfo": meminfo,
            "active_workers": count_workers(),
            "entities": {},
            "daemons": {},
        }

        # Swap activity counters
        vmstat = read_vmstat_swap()
        if vmstat:
            sample["vmstat_swap"] = vmstat

        # Memory pressure (PSI)
        psi = read_memory_pressure()
        if psi:
            sample["memory_pressure"] = psi

        # Host CPU counters (cumulative jiffies)
        host_cpu = read_host_cpu()
        if host_cpu:
            sample["host_cpu"] = host_cpu

        entity_cpu = {}
        daemon_cpu = {}

        # Per-agent entity RSS/PSS + CPU
        try:
            pids = get_agent_pids()
            for entity_id, pid in pids.items():
                smaps = read_smaps(pid)
                if smaps is not None:
                    sample["entities"][entity_id] = smaps
                cpu = read_process_cpu(pid)
                if cpu is not None:
                    entity_cpu[entity_id] = cpu
        except Exception:
            pass  # Don't let measurement errors crash the collector

        # Daemon RSS/PSS + CPU (dockerd, containerd, etc.)
        try:
            daemon_pids = get_daemon_pids()
            for daemon_name, pid in daemon_pids.items():
                smaps = read_smaps(pid)
                if smaps is not None:
                    sample["daemons"][daemon_name] = smaps
                cpu = read_process_cpu(pid)
                if cpu is not None:
                    daemon_cpu[daemon_name] = cpu
        except Exception:
            pass

        if entity_cpu:
            sample["entity_cpu"] = entity_cpu
        if daemon_cpu:
            sample["daemon_cpu"] = daemon_cpu

        return sample

    def run(
        self,
        output: TextIO,
        get_agent_pids: Callable[[], Dict[str, int]],
        get_daemon_pids: Callable[[], Dict[str, int]],
        count_workers: Callable[[], int],
    ) -> None:
        """
        Continuous collection loop. Runs until stop() is called.
        Writes JSONL to the output file.
        """
        while not self._stop.is_set():
            sample = self.sample(get_agent_pids, get_daemon_pids, count_workers)
            try:
                output.write(json.dumps(sample) + "\n")
                output.flush()
            except (ValueError, OSError):
                break
            self._stop.wait(timeout=self.interval_s)

    def stop(self) -> None:
        """Signal the collector to stop."""
        self._stop.set()

    def run_in_thread(
        self,
        output: TextIO,
        get_agent_pids: Callable[[], Dict[str, int]],
        get_daemon_pids: Callable[[], Dict[str, int]],
        count_workers: Callable[[], int],
    ) -> threading.Thread:
        """Start collection in a background thread. Returns the thread."""
        t = threading.Thread(
            target=self.run,
            args=(output, get_agent_pids, get_daemon_pids, count_workers),
            daemon=True,
        )
        t.start()
        return t
