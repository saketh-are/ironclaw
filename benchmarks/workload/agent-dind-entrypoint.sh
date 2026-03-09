#!/bin/sh
set -e

# Start dockerd in the background.
# --iptables=false / --ip6tables=false: gVisor's netstack handles networking;
# host iptables are not available inside the sandbox.
# --storage-driver=vfs: gVisor doesn't support overlay mounts, so we use vfs.
# vfs is slower (full copies) but works universally inside gVisor's sandbox.
echo "[dind-entrypoint] Starting dockerd..."
dockerd \
    --iptables=false \
    --ip6tables=false \
    --storage-driver=vfs \
    >/var/log/dockerd.log 2>&1 &

# Wait for dockerd to be ready (up to 120 seconds)
echo "[dind-entrypoint] Waiting for dockerd to be ready..."
timeout=120
elapsed=0
while ! docker info >/dev/null 2>&1; do
    sleep 1
    elapsed=$((elapsed + 1))
    if [ "$elapsed" -ge "$timeout" ]; then
        echo "[dind-entrypoint] ERROR: dockerd did not start within ${timeout}s"
        cat /var/log/dockerd.log
        exit 1
    fi
done
echo "[dind-entrypoint] dockerd ready after ${elapsed}s."

# Load the worker image from the bind-mounted tarball
WORKER_TAR="/opt/.worker-image.tar"
if [ -f "$WORKER_TAR" ]; then
    echo "[dind-entrypoint] Loading worker image from ${WORKER_TAR}..."
    docker load < "$WORKER_TAR"
    echo "[dind-entrypoint] Worker image loaded."
else
    echo "[dind-entrypoint] WARNING: ${WORKER_TAR} not found, workers may fail to start."
fi

# Detect the inner docker0 bridge gateway IP for worker->agent communication.
# The outer Docker network uses 172.17.0.x, so the inner dockerd picks a
# different subnet (e.g., 172.18.0.1). We must discover this dynamically.
BRIDGE_IP=$(python3 -c "
import socket, struct, fcntl
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
ip = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, struct.pack('256s', b'docker0'))[20:24])
print(ip)
" 2>/dev/null || echo "")

if [ -n "$BRIDGE_IP" ]; then
    echo "[dind-entrypoint] Inner docker0 bridge IP: ${BRIDGE_IP}"
    export DOCKER_BRIDGE_GATEWAY="$BRIDGE_IP"
else
    echo "[dind-entrypoint] WARNING: Could not detect docker0 bridge IP"
fi

# Hand off to agent.py as PID 1 (for clean signal handling)
echo "[dind-entrypoint] Starting agent..."
exec python3 /usr/local/bin/agent.py
