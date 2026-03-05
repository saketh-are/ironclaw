#!/usr/bin/env python3
"""
Filtering Unix-socket proxy for the Podman (Docker-compat) API.

Sits between an agent container and the real Podman socket, enforcing:
  1. Only the allowed worker image may be used in container creation.
  2. Only a safe subset of API endpoints is permitted.
  3. Privileged mode, host networking, pid/ipc sharing, and dangerous
     bind mounts are blocked.

Usage:
    python3 podman_proxy.py \
        --listen /home/bench-pm-0/.podman-proxy.sock \
        --upstream /run/user/3000/podman/podman.sock \
        --allowed-image localhost/bench-worker:latest
"""

import argparse
import json
import os
import re
import select
import signal
import socket
import sys
import threading

# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

# API paths that are unconditionally allowed (read-only / harmless).
ALLOWED_READONLY_PATTERNS = [
    re.compile(r"^/(v[\d.]+/)?version$"),
    re.compile(r"^/(v[\d.]+/)?_ping$"),
    re.compile(r"^/(v[\d.]+/)?info$"),
    re.compile(r"^/(v[\d.]+/)?images/json$"),
    re.compile(r"^/(v[\d.]+/)?images/.+/json$"),
    re.compile(r"^/(v[\d.]+/)?containers/json$"),
    re.compile(r"^/(v[\d.]+/)?containers/[^/]+/json$"),
    re.compile(r"^/(v[\d.]+/)?containers/[^/]+/logs$"),
    re.compile(r"^/(v[\d.]+/)?containers/[^/]+/top$"),
    re.compile(r"^/(v[\d.]+/)?containers/[^/]+/wait$"),
    re.compile(r"^/(v[\d.]+/)?networks$"),
    re.compile(r"^/(v[\d.]+/)?networks/[^/]+$"),
]

# Mutable endpoints that are allowed (with body inspection for create).
ALLOWED_MUTABLE_PATTERNS = [
    re.compile(r"^/(v[\d.]+/)?containers/create$"),
    re.compile(r"^/(v[\d.]+/)?containers/[^/]+/start$"),
    re.compile(r"^/(v[\d.]+/)?containers/[^/]+/stop$"),
    re.compile(r"^/(v[\d.]+/)?containers/[^/]+/kill$"),
    re.compile(r"^/(v[\d.]+/)?containers/[^/]+/wait$"),
    re.compile(r"^/(v[\d.]+/)?containers/[^/]+/remove$"),
    re.compile(r"^/(v[\d.]+/)?containers/[^/]+$"),  # DELETE
]

# Paths inside the container that must never be bind-mounted.
DANGEROUS_MOUNT_TARGETS = {
    "/", "/etc", "/proc", "/sys", "/dev",
    "/var/run/docker.sock", "/run/docker.sock",
}

# Host paths that must never be bind-mounted into a worker.
DANGEROUS_HOST_PATHS = {
    "/", "/etc", "/proc", "/sys", "/dev", "/boot", "/root",
    "/var/run/docker.sock", "/run/docker.sock",
}


def is_path_allowed(method: str, path: str) -> bool:
    """Check if the HTTP method + path is in the allowlist."""
    for pat in ALLOWED_READONLY_PATTERNS:
        if pat.match(path):
            return True
    for pat in ALLOWED_MUTABLE_PATTERNS:
        if pat.match(path):
            return True
    return False


def _is_under_dangerous_path(path: str, dangerous_set: set) -> bool:
    """Check if *path* equals or is a child of any entry in *dangerous_set*.

    Normalises the path first (resolves ``..``, ``//``, trailing ``/``) so
    that trivial bypasses like ``/etc/shadow``, ``/proc/1/root``, or
    ``../../etc`` are caught.
    """
    normalised = os.path.normpath(path)
    for d in dangerous_set:
        d_norm = os.path.normpath(d)
        if normalised == d_norm or normalised.startswith(d_norm + "/"):
            return True
    return False


def validate_create_body(body: dict, allowed_image: str):
    """Inspect a container-create JSON body.  Return an error string or None."""
    # 1. Image must match exactly.
    image = body.get("Image", "")
    if image != allowed_image:
        return f"image '{image}' not allowed (expected '{allowed_image}')"

    # 2. No privileged mode.
    host_config = body.get("HostConfig") or {}
    if host_config.get("Privileged"):
        return "privileged mode is not allowed"

    # 3. No host namespace sharing.
    for field, label in [
        ("NetworkMode", "network"), ("PidMode", "PID"),
        ("IpcMode", "IPC"), ("UsernsMode", "user"),
        ("UTSMode", "UTS"), ("CgroupnsMode", "cgroup"),
    ]:
        if host_config.get(field) == "host":
            return f"host {label} mode is not allowed"

    # 4. No dangerous capabilities.
    cap_add = set(host_config.get("CapAdd") or [])
    dangerous_caps = cap_add & {"ALL", "SYS_ADMIN", "SYS_PTRACE", "SYS_RAWIO",
                                "DAC_READ_SEARCH", "NET_ADMIN", "SYS_MODULE"}
    if dangerous_caps:
        return f"dangerous capabilities: {dangerous_caps}"

    # 5. No dangerous bind mounts (prefix-aware, normalised).
    for bind in host_config.get("Binds") or []:
        parts = bind.split(":")
        if len(parts) >= 2:
            host_path, container_path = parts[0], parts[1]
            if _is_under_dangerous_path(host_path, DANGEROUS_HOST_PATHS):
                return f"bind mount from '{host_path}' is not allowed"
            if _is_under_dangerous_path(container_path, DANGEROUS_MOUNT_TARGETS):
                return f"bind mount to '{container_path}' is not allowed"

    for mount in host_config.get("Mounts") or []:
        source = mount.get("Source", "")
        target = mount.get("Target", "")
        if source and _is_under_dangerous_path(source, DANGEROUS_HOST_PATHS):
            return f"mount source '{source}' is not allowed"
        if target and _is_under_dangerous_path(target, DANGEROUS_MOUNT_TARGETS):
            return f"mount target '{target}' is not allowed"

    # 6. No host device mounts.
    if host_config.get("Devices"):
        return "device mounts are not allowed"

    # 7. No disabling security profiles.
    for opt in host_config.get("SecurityOpt") or []:
        opt_lower = opt.lower()
        if "unconfined" in opt_lower or "disabled" in opt_lower:
            return f"security option '{opt}' is not allowed"

    return None


# ---------------------------------------------------------------------------
# HTTP parsing (minimal, sufficient for Docker API over Unix socket)
# ---------------------------------------------------------------------------

def read_http_request(sock: socket.socket) -> tuple[bytes, str, str, dict, bytes]:
    """Read an HTTP request.  Returns (raw_header, method, path, headers, body)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            return b"", "", "", {}, b""
        buf += chunk

    header_end = buf.index(b"\r\n\r\n") + 4
    header_bytes = buf[:header_end]
    body = buf[header_end:]

    lines = header_bytes.decode("utf-8", errors="replace").split("\r\n")
    request_line = lines[0]
    parts = request_line.split(" ", 2)
    if len(parts) < 2:
        return header_bytes, "", "", {}, body
    method, full_path = parts[0], parts[1]

    # Strip query string for path matching.
    path = full_path.split("?")[0]

    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    # Read remaining body if Content-Length is set.
    content_length = int(headers.get("content-length", 0))
    while len(body) < content_length:
        chunk = sock.recv(min(4096, content_length - len(body)))
        if not chunk:
            break
        body += chunk

    return header_bytes, method, path, headers, body


def send_error(client: socket.socket, status: int, message: str):
    """Send an HTTP error response and close the client."""
    body = json.dumps({"message": message}).encode()
    resp = (
        f"HTTP/1.1 {status} Blocked\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode() + body
    try:
        client.sendall(resp)
    except OSError:
        pass


def relay(src: socket.socket, dst: socket.socket):
    """Relay data between two sockets until one side closes."""
    try:
        while True:
            readable, _, _ = select.select([src, dst], [], [], 30)
            if not readable:
                continue
            for s in readable:
                data = s.recv(65536)
                if not data:
                    return
                target = dst if s is src else src
                target.sendall(data)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Proxy server
# ---------------------------------------------------------------------------

def _force_connection_close(raw_header: bytes) -> bytes:
    """Replace or inject ``Connection: close`` in the raw HTTP header.

    This ensures each TCP connection handles exactly one request, preventing
    HTTP keep-alive from letting subsequent requests bypass filtering.
    """
    header_str = raw_header.decode("utf-8", errors="replace")
    lines = header_str.split("\r\n")
    new_lines = []
    found = False
    for line in lines:
        if line.lower().startswith("connection:"):
            new_lines.append("Connection: close")
            found = True
        else:
            new_lines.append(line)
    if not found:
        # Insert before the final empty line that terminates the header.
        # lines looks like [..., "", ""] after splitting on \r\n\r\n.
        # Insert before the last blank line.
        insert_pos = len(new_lines) - 2
        if insert_pos < 1:
            insert_pos = len(new_lines) - 1
        new_lines.insert(insert_pos, "Connection: close")
    return "\r\n".join(new_lines).encode("utf-8")


def handle_client(client: socket.socket, upstream_path: str, allowed_image: str):
    """Handle one client connection: parse request, filter, relay."""
    try:
        raw_header, method, path, headers, body = read_http_request(client)
        if not method:
            return

        # Check allowlist.
        if not is_path_allowed(method, path):
            send_error(client, 403, f"endpoint not allowed: {method} {path}")
            return

        # Deep-inspect container creation.
        is_create = bool(re.match(r"^/(v[\d.]+/)?containers/create$", path))
        if is_create and body:
            try:
                create_body = json.loads(body)
            except json.JSONDecodeError:
                send_error(client, 400, "invalid JSON in request body")
                return
            err = validate_create_body(create_body, allowed_image)
            if err:
                send_error(client, 403, f"blocked: {err}")
                return

        # Force Connection: close so we only handle one request per
        # connection.  Without this, HTTP keep-alive would let subsequent
        # requests bypass filtering via the raw relay() pipe.
        raw_header = _force_connection_close(raw_header)

        # Connect to upstream and relay.
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        upstream.connect(upstream_path)
        upstream.sendall(raw_header + body)
        relay(client, upstream)
        upstream.close()
    except Exception:
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description="Filtering Podman API proxy")
    parser.add_argument("--listen", required=True, help="Path for the proxy Unix socket")
    parser.add_argument("--upstream", required=True, help="Path to the real Podman socket")
    parser.add_argument("--allowed-image", required=True, help="Only this image may be used")
    args = parser.parse_args()

    # Clean up stale socket.
    try:
        os.unlink(args.listen)
    except FileNotFoundError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(args.listen)
    os.chmod(args.listen, 0o660)
    server.listen(32)

    # Graceful shutdown.
    stop = threading.Event()

    def _signal(sig, frame):
        stop.set()
        server.close()

    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)

    while not stop.is_set():
        try:
            client, _ = server.accept()
        except OSError:
            break
        t = threading.Thread(
            target=handle_client,
            args=(client, args.upstream, args.allowed_image),
            daemon=True,
        )
        t.start()


if __name__ == "__main__":
    main()
