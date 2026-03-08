#!/usr/bin/env python3
import argparse
import base64
import http.client
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

BENCH_COMMAND_BEGIN = "<BENCH_COMMAND>"
BENCH_COMMAND_END = "</BENCH_COMMAND>"
GATEWAY_AUTH_TOKEN = os.environ.get("GATEWAY_AUTH_TOKEN", "bench-token")
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "3000"))
FC_KERNEL_PATH = os.environ.get("FC_KERNEL_PATH", "/opt/vmlinux")
FC_ROOTFS_PATH = os.environ.get("FC_ROOTFS_PATH", "/opt/ironclaw-worker-rootfs.ext4")
FC_VM_DIR = Path(os.environ.get("FC_VM_DIR", "/tmp/fc-vms"))
FC_VM_MEMORY_MB = int(os.environ.get("FC_VM_MEMORY_MB", "512"))
BENCH_EVIDENCE_DIR = Path(os.environ.get("BENCH_EVIDENCE_DIR", "/tmp/.ironclaw/bench-evidence"))


class UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout=5):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


def fc_api(socket_path, method, path, body=None):
    conn = UnixSocketHTTPConnection(socket_path)
    payload = json.dumps(body) if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    if resp.status >= 300:
        raise RuntimeError(f"Firecracker API {method} {path} returned {resp.status}: {data}")
    return data


def render_task(command: str) -> str:
    command = command.strip()
    if not command:
        command = "echo benchmark-worker-ok"
    return "\n".join([
        "Please create benchmark job.",
        BENCH_COMMAND_BEGIN,
        command,
        BENCH_COMMAND_END,
    ])


def create_external_job(task: str) -> dict:
    payload = json.dumps({"task": task}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{GATEWAY_PORT}/api/benchmark/external-worker",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GATEWAY_AUTH_TOKEN}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def report_failure(orchestrator_url: str, job_id: str, worker_token: str, message: str):
    body = json.dumps({
        "success": False,
        "message": message,
        "iterations": 0,
    }).encode()
    req = urllib.request.Request(
        f"{orchestrator_url}/worker/{job_id}/complete",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {worker_token}",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def write_worker_cleaned(job_id: str, removed: bool):
    BENCH_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "event": "worker_cleaned",
        "job_id": job_id,
        "container_id": None,
        "container_removed": removed,
        "ts_unix_ms": int(time.time() * 1000),
    }
    (BENCH_EVIDENCE_DIR / f"worker-cleaned-{job_id}.json").write_text(json.dumps(payload))


def next_slot(counter_path: Path) -> int:
    counter_path.parent.mkdir(parents=True, exist_ok=True)
    value = 0
    if counter_path.exists():
        try:
            value = int(counter_path.read_text().strip())
        except Exception:
            value = 0
    counter_path.write_text(str(value + 1))
    return value


def network_for_slot(slot: int):
    third = (slot // 64) % 250
    base = (slot % 64) * 4
    host_ip = f"172.29.{third}.{base + 1}"
    guest_ip = f"172.29.{third}.{base + 2}"
    tap_name = f"fc{slot % 10000}"
    mac = f"02:FC:{(slot >> 16) & 0xff:02x}:{(slot >> 8) & 0xff:02x}:{slot & 0xff:02x}:01"
    return tap_name, host_ip, guest_ip, mac


def setup_tap(tap_name: str, host_ip: str):
    subprocess.run(["ip", "tuntap", "add", tap_name, "mode", "tap"], check=True)
    subprocess.run(["ip", "addr", "add", f"{host_ip}/30", "dev", tap_name], check=True)
    subprocess.run(["ip", "link", "set", tap_name, "up"], check=True)


def cleanup_tap(tap_name: str):
    subprocess.run(["ip", "link", "del", tap_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def create_workspace_image(image_path: Path, seed_dir: Path):
    subprocess.run(["dd", "if=/dev/zero", f"of={image_path}", "bs=1M", "count=128", "status=none"], check=True)
    subprocess.run(["mkfs.ext4", "-F", "-q", "-d", str(seed_dir), str(image_path)], check=True)


def sync_workspace_image(image_path: Path, project_dir: Path):
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["debugfs", "-R", f"rdump / {project_dir}", str(image_path)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--command-b64", required=True)
    args = parser.parse_args()

    command = base64.b64decode(args.command_b64).decode()
    job = create_external_job(render_task(command))
    job_id = job["job_id"]
    worker_token = job["worker_token"]
    local_orchestrator_url = job["orchestrator_url"]
    project_dir = Path(job["project_dir"])

    vm_dir = FC_VM_DIR / job_id
    seed_dir = vm_dir / "workspace-seed"
    socket_path = Path(f"/tmp/fc-{job_id[:12]}.sock")
    workspace_image = vm_dir / "workspace.ext4"
    pid_path = vm_dir / "firecracker.pid"
    log_path = BENCH_EVIDENCE_DIR / f"worker-vm-{job_id}.log"
    counter_path = FC_VM_DIR / ".slot-counter"
    vm_dir.mkdir(parents=True, exist_ok=True)
    seed_dir.mkdir(parents=True, exist_ok=True)
    BENCH_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (seed_dir / ".bench-evidence").mkdir(parents=True, exist_ok=True)

    slot = next_slot(counter_path)
    tap_name, host_ip, guest_ip, guest_mac = network_for_slot(slot)
    proc = None

    try:
        setup_tap(tap_name, host_ip)
        create_workspace_image(workspace_image, seed_dir)

        with open(log_path, "ab", buffering=0) as fc_log:
            proc = subprocess.Popen(
                ["firecracker", "--api-sock", str(socket_path)],
                stdout=fc_log,
                stderr=subprocess.STDOUT,
            )
        del fc_log
        pid_path.write_text(str(proc.pid))

        deadline = time.time() + 5
        while time.time() < deadline:
            if socket_path.exists():
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Firecracker API socket did not appear")

        guest_orchestrator_url = f"http://{host_ip}:50051"
        boot_args = " ".join([
            "console=ttyS0",
            "reboot=k",
            "panic=1",
            "pci=off",
            "init=/sbin/init",
            f"ip={guest_ip}::{host_ip}:255.255.255.252::eth0:off",
            f"job_id={job_id}",
            f"worker_token={worker_token}",
            f"orchestrator_url={guest_orchestrator_url}",
        ])

        fc_api(str(socket_path), "PUT", "/boot-source", {
            "kernel_image_path": FC_KERNEL_PATH,
            "boot_args": boot_args,
        })
        fc_api(str(socket_path), "PUT", "/drives/rootfs", {
            "drive_id": "rootfs",
            "path_on_host": FC_ROOTFS_PATH,
            "is_root_device": True,
            "is_read_only": True,
        })
        fc_api(str(socket_path), "PUT", "/drives/workspace", {
            "drive_id": "workspace",
            "path_on_host": str(workspace_image),
            "is_root_device": False,
            "is_read_only": False,
        })
        fc_api(str(socket_path), "PUT", "/network-interfaces/eth0", {
            "iface_id": "eth0",
            "host_dev_name": tap_name,
            "guest_mac": guest_mac,
        })
        fc_api(str(socket_path), "PUT", "/machine-config", {
            "vcpu_count": 1,
            "mem_size_mib": max(256, FC_VM_MEMORY_MB + 128),
        })
        fc_api(str(socket_path), "PUT", "/actions", {"action_type": "InstanceStart"})

        proc.wait(timeout=None)
        sync_workspace_image(workspace_image, project_dir)
    except Exception as exc:
        report_failure(local_orchestrator_url, job_id, worker_token, f"Firecracker launch failed: {exc}")
        raise
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        try:
            socket_path.unlink(missing_ok=True)
        except Exception:
            pass
        cleanup_tap(tap_name)
        try:
            shutil.rmtree(vm_dir, ignore_errors=True)
        except Exception:
            pass
        write_worker_cleaned(job_id, not vm_dir.exists())


if __name__ == "__main__":
    main()
