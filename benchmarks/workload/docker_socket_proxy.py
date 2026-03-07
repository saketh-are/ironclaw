#!/usr/bin/env python3
"""
Thin Docker-API socket proxy for benchmark isolation.

Forwards Docker Engine API requests from a listen Unix socket to an upstream
daemon socket (Podman Docker-compat, inner dockerd, etc.). Optionally rewrites
container-create requests to add a ``localhost/`` prefix to image names, which
is required for Podman to resolve locally-loaded images.

Usage:
    python3 docker_socket_proxy.py \\
        --listen /var/run/docker.sock \\
        --upstream /run/user/4000/podman/podman.sock

    # With image-name rewriting for Podman:
    python3 docker_socket_proxy.py \\
        --listen /var/run/docker.sock \\
        --upstream /run/user/4000/podman/podman.sock \\
        --rewrite-images
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

BUFFER_SIZE = 65536
CONTAINER_CREATE_RE = re.compile(
    rb"^POST\s+/(?:v[\d.]+/)?containers/create[\s?]",
    re.IGNORECASE,
)


def _decode_chunked(data):
    """Decode HTTP chunked transfer encoding. Returns decoded bytes or None."""
    result = b""
    pos = 0
    while pos < len(data):
        # Find the chunk size line
        crlf = data.find(b"\r\n", pos)
        if crlf < 0:
            break
        size_str = data[pos:crlf].strip()
        try:
            chunk_size = int(size_str, 16)
        except ValueError:
            return None
        if chunk_size == 0:
            break
        start = crlf + 2
        end = start + chunk_size
        if end > len(data):
            # Incomplete chunk — return what we have
            result += data[start:]
            break
        result += data[start:end]
        pos = end + 2  # skip trailing \r\n
    return result


def forward(src, dst, label=None):
    """Forward data from src to dst until EOF."""
    first = True
    try:
        while True:
            data = src.recv(BUFFER_SIZE)
            if not data:
                break
            if first and label:
                preview = data[:200].decode("latin-1", errors="replace").replace("\r\n", " | ")
                print(f"[docker-proxy] {label}: {preview[:120]}", flush=True)
                first = False
            dst.sendall(data)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def rewrite_container_create(header_bytes, body_bytes):
    """Rewrite container-create requests for Podman compatibility.

    Applies the following transformations:
      1. Add ``localhost/`` prefix to the Image field (Podman requires it
         for locally-loaded images).
      2. Fix SecurityOpt entries: Podman doesn't accept ``no-new-privileges:true``,
         only ``no-new-privileges``.

    Returns (possibly modified header_bytes, possibly modified body_bytes).
    """
    if not CONTAINER_CREATE_RE.match(header_bytes):
        return header_bytes, body_bytes

    if not body_bytes:
        return header_bytes, body_bytes

    # Handle chunked Transfer-Encoding: decode chunks first
    chunked = b"transfer-encoding" in header_bytes.lower() and b"chunked" in header_bytes.lower()
    raw_body = body_bytes
    if chunked:
        raw_body = _decode_chunked(body_bytes)
        if raw_body is None:
            print(f"[docker-proxy] WARNING: failed to decode chunked body", flush=True)
            return header_bytes, body_bytes

    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"[docker-proxy] WARNING: JSON parse failed: {e}", flush=True)
        print(f"[docker-proxy]   body preview: {raw_body[:200]!r}", flush=True)
        return header_bytes, body_bytes

    modified = False

    # 1. Rewrite image name
    image = body.get("Image", "")
    if image and not image.startswith("localhost/") and "/" not in image.split(":")[0]:
        body["Image"] = f"localhost/{image}"
        modified = True

    # 2. Fix SecurityOpt for Podman
    host_config = body.get("HostConfig", {})
    sec_opts = host_config.get("SecurityOpt", [])
    if sec_opts:
        new_opts = []
        for opt in sec_opts:
            # Podman rejects "no-new-privileges:true", wants "no-new-privileges"
            if opt == "no-new-privileges:true":
                new_opts.append("no-new-privileges")
                modified = True
            elif opt == "no-new-privileges:false":
                # Just drop it — Podman default is to allow privilege escalation
                modified = True
            else:
                new_opts.append(opt)
        host_config["SecurityOpt"] = new_opts

    # 3. Remove resource limits that Podman rootless can't set (cgroup delegation)
    for field in ("Memory", "MemorySwap", "CpuShares", "CpuPeriod", "CpuQuota",
                  "NanoCpus", "CpusetCpus", "CpusetMems",
                  "BlkioWeight", "PidsLimit"):
        if field in host_config:
            del host_config[field]
            modified = True

    # 4. Disable AutoRemove — Podman's wait endpoint races with auto-removal,
    #    causing bollard to get an error. Ironclaw cleans up via DELETE anyway.
    if host_config.get("AutoRemove"):
        host_config["AutoRemove"] = False
        modified = True

    if modified:
        new_body = json.dumps(body).encode()
        print(f"[docker-proxy] Rewrote container-create: Image={body.get('Image')}, "
              f"SecurityOpt={host_config.get('SecurityOpt')}", flush=True)
        # Replace Transfer-Encoding: chunked with Content-Length
        header_str = header_bytes.decode("latin-1")
        header_str = re.sub(r"(?i)transfer-encoding:[^\r]*\r\n", "", header_str)
        header_str = re.sub(
            r"(?i)content-length:\s*\d+",
            f"Content-Length: {len(new_body)}",
            header_str,
        )
        if "content-length" not in header_str.lower():
            # Insert Content-Length before the final \r\n\r\n
            header_str = header_str[:-2] + f"Content-Length: {len(new_body)}\r\n\r\n"
        return header_str.encode("latin-1"), new_body

    return header_bytes, body_bytes


def read_http_request(sock):
    """Read a complete HTTP request (headers + body) from a socket.

    Returns (raw_header_bytes, body_bytes) or (b"", b"") on EOF/error.
    Handles both Content-Length and Transfer-Encoding: chunked bodies.
    """
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(BUFFER_SIZE)
        if not chunk:
            return b"", b""
        buf += chunk

    header_end = buf.index(b"\r\n\r\n") + 4
    header_bytes = buf[:header_end]
    body = buf[header_end:]

    # Check for Transfer-Encoding: chunked
    is_chunked = False
    content_length = 0
    for line in header_bytes.split(b"\r\n"):
        lower = line.lower()
        if lower.startswith(b"transfer-encoding:") and b"chunked" in lower:
            is_chunked = True
        if lower.startswith(b"content-length:"):
            try:
                content_length = int(line.split(b":", 1)[1].strip())
            except ValueError:
                pass

    if is_chunked:
        # Read until we see the chunk terminator: 0\r\n\r\n
        while b"0\r\n\r\n" not in body and b"0\r\n" not in body.rstrip():
            chunk = sock.recv(BUFFER_SIZE)
            if not chunk:
                break
            body += chunk
    else:
        while len(body) < content_length:
            chunk = sock.recv(min(BUFFER_SIZE, content_length - len(body)))
            if not chunk:
                break
            body += chunk

    return header_bytes, body


def force_connection_close(header_bytes):
    """Inject or replace Connection: close in raw HTTP headers.

    Forces one-request-per-connection so every request gets inspected.
    """
    header_str = header_bytes.decode("latin-1")
    if re.search(r"(?i)^connection:", header_str, re.MULTILINE):
        header_str = re.sub(
            r"(?i)^connection:.*$",
            "Connection: close",
            header_str,
            flags=re.MULTILINE,
        )
    else:
        # Insert before the final \r\n\r\n
        header_str = header_str[:-2] + "Connection: close\r\n\r\n"
    return header_str.encode("latin-1")


def handle_connection(client_sock, upstream_path, rewrite_images):
    """Handle a single proxied connection."""
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        upstream.connect(upstream_path)
    except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
        # Send a minimal HTTP 502 and close
        try:
            client_sock.sendall(
                b"HTTP/1.1 502 Bad Gateway\r\n"
                b"Content-Length: 0\r\n\r\n"
            )
        except OSError:
            pass
        client_sock.close()
        return

    if not rewrite_images:
        # Pure bidirectional forwarding — no HTTP parsing
        t1 = threading.Thread(target=forward, args=(client_sock, upstream), daemon=True)
        t2 = threading.Thread(target=forward, args=(upstream, client_sock), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    else:
        # Peek at the first bytes to check if this is a container-create.
        # Only container-create requests need rewriting; everything else
        # is forwarded as raw bytes to preserve exact HTTP semantics
        # (important for long-poll endpoints like /wait).
        try:
            peeked = client_sock.recv(BUFFER_SIZE, socket.MSG_PEEK)
        except OSError:
            peeked = b""
        if not peeked:
            client_sock.close()
            upstream.close()
            return

        if CONTAINER_CREATE_RE.match(peeked):
            # Parse the complete request, rewrite, and forward
            header_bytes, body_bytes = read_http_request(client_sock)
            request_line = header_bytes.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
            print(f"[docker-proxy] REQ: {request_line} body={len(body_bytes)}b", flush=True)
            header_bytes, body_bytes = rewrite_container_create(header_bytes, body_bytes)
            header_bytes = force_connection_close(header_bytes)
            upstream.sendall(header_bytes + body_bytes)
        else:
            # Forward raw — no parsing, no modification
            first = client_sock.recv(BUFFER_SIZE)
            req_line = first.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
            print(f"[docker-proxy] RAW: {req_line}", flush=True)
            upstream.sendall(first)

        # Bidirectional forwarding for the rest of the connection
        t1 = threading.Thread(target=forward, args=(client_sock, upstream, "c->u"), daemon=True)
        t2 = threading.Thread(target=forward, args=(upstream, client_sock, "u->c"), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    client_sock.close()
    upstream.close()


def main():
    parser = argparse.ArgumentParser(description="Docker socket proxy")
    parser.add_argument("--listen", required=True, help="Path for the listen socket")
    parser.add_argument("--upstream", required=True, help="Path to the upstream daemon socket")
    parser.add_argument(
        "--rewrite-images",
        action="store_true",
        help="Add localhost/ prefix to image names in container-create (for Podman)",
    )
    args = parser.parse_args()

    # Remove stale socket
    try:
        os.unlink(args.listen)
    except FileNotFoundError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(args.listen)
    os.chmod(args.listen, 0o666)
    server.listen(64)

    print(f"[docker-proxy] {args.listen} -> {args.upstream}"
          f"{' (rewrite-images)' if args.rewrite_images else ''}", flush=True)

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
            target=handle_connection,
            args=(client, args.upstream, args.rewrite_images),
            daemon=True,
        )
        t.start()

    try:
        os.unlink(args.listen)
    except FileNotFoundError:
        pass
    print("[docker-proxy] Stopped.", flush=True)


if __name__ == "__main__":
    main()
