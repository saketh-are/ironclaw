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
    rb"^(POST\s+(?:/v[\d.]+/)?containers/create\s)",
    re.IGNORECASE,
)


def forward(src, dst):
    """Forward data from src to dst until EOF."""
    try:
        while True:
            data = src.recv(BUFFER_SIZE)
            if not data:
                break
            dst.sendall(data)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def rewrite_image_in_body(raw_request):
    """If the request is a container-create, add localhost/ prefix to the Image field.

    Returns the (possibly modified) raw bytes.
    """
    # Split headers from body
    sep = raw_request.find(b"\r\n\r\n")
    if sep < 0:
        return raw_request

    header_bytes = raw_request[:sep + 4]
    body_bytes = raw_request[sep + 4:]

    if not CONTAINER_CREATE_RE.match(header_bytes):
        return raw_request

    if not body_bytes:
        return raw_request

    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw_request

    image = body.get("Image", "")
    if image and not image.startswith("localhost/") and "/" not in image.split(":")[0]:
        body["Image"] = f"localhost/{image}"
        new_body = json.dumps(body).encode()
        # Update Content-Length header
        header_str = header_bytes.decode("latin-1")
        header_str = re.sub(
            r"(?i)content-length:\s*\d+",
            f"Content-Length: {len(new_body)}",
            header_str,
        )
        return header_str.encode("latin-1") + new_body

    return raw_request


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
        # Read the first request from the client, rewrite if needed, then
        # fall back to raw forwarding for the rest of the connection.
        first_chunk = client_sock.recv(BUFFER_SIZE)
        if first_chunk:
            modified = rewrite_image_in_body(first_chunk)
            upstream.sendall(modified)

        # Bidirectional forwarding for the rest
        t1 = threading.Thread(target=forward, args=(client_sock, upstream), daemon=True)
        t2 = threading.Thread(target=forward, args=(upstream, client_sock), daemon=True)
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
