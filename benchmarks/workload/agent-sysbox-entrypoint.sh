#!/bin/sh
set -e

# Start dockerd in the background.
# Sysbox provides full Linux namespace isolation with user-ns remapping,
# so iptables and overlay2 work natively — no workarounds needed.
echo "[sysbox-entrypoint] Starting dockerd..."
DOCKERD_ARGS="--storage-driver=overlay2"
if [ "${DOCKERD_DEBUG:-0}" = "1" ]; then
    DOCKERD_ARGS="$DOCKERD_ARGS --debug"
fi
if [ -n "${DOCKERD_EXTRA_ARGS:-}" ]; then
    DOCKERD_ARGS="$DOCKERD_ARGS ${DOCKERD_EXTRA_ARGS}"
fi

# shellcheck disable=SC2086
dockerd $DOCKERD_ARGS >/var/log/dockerd.log 2>&1 &

# Wait for dockerd to be ready (up to 120 seconds)
echo "[sysbox-entrypoint] Waiting for dockerd to be ready..."
timeout=120
elapsed=0
while ! docker info >/dev/null 2>&1; do
    sleep 1
    elapsed=$((elapsed + 1))
    if [ "$elapsed" -ge "$timeout" ]; then
        echo "[sysbox-entrypoint] ERROR: dockerd did not start within ${timeout}s"
        cat /var/log/dockerd.log
        exit 1
    fi
done
echo "[sysbox-entrypoint] dockerd ready after ${elapsed}s."

# Load the worker image from the bind-mounted tarball
WORKER_TAR="/opt/.worker-image.tar"
if [ -f "$WORKER_TAR" ]; then
    echo "[sysbox-entrypoint] Loading worker image from ${WORKER_TAR}..."
    docker load < "$WORKER_TAR"
    echo "[sysbox-entrypoint] Worker image loaded."
else
    echo "[sysbox-entrypoint] WARNING: ${WORKER_TAR} not found, workers may fail to start."
fi

# Detect the inner docker0 bridge gateway IP for worker->agent communication.
BRIDGE_IP=$(python3 -c "
import socket, struct, fcntl
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
ip = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, struct.pack('256s', b'docker0'))[20:24])
print(ip)
" 2>/dev/null || echo "")

if [ -n "$BRIDGE_IP" ]; then
    echo "[sysbox-entrypoint] Inner docker0 bridge IP: ${BRIDGE_IP}"
    export DOCKER_BRIDGE_GATEWAY="$BRIDGE_IP"
else
    echo "[sysbox-entrypoint] WARNING: Could not detect docker0 bridge IP"
fi

# Hand off to agent.py as PID 1 (for clean signal handling)
echo "[sysbox-entrypoint] Starting agent..."
exec python3 /usr/local/bin/agent.py
