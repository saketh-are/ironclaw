#!/bin/sh
set -e

# --- Start dockerd in background ---
# For gVisor: --storage-driver=vfs --iptables=false --ip6tables=false
# For Sysbox: overlay2 works, iptables work
DOCKERD_ARGS="${DOCKERD_EXTRA_ARGS:-}"

echo "[entrypoint] Starting dockerd... $DOCKERD_ARGS"
# shellcheck disable=SC2086
dockerd $DOCKERD_ARGS >/var/log/dockerd.log 2>&1 &

# Wait for dockerd (up to 120s)
elapsed=0
while ! docker info >/dev/null 2>&1; do
    sleep 1
    elapsed=$((elapsed + 1))
    if [ "$elapsed" -ge 120 ]; then
        echo "[entrypoint] ERROR: dockerd did not start within 120s" >&2
        cat /var/log/dockerd.log >&2
        exit 1
    fi
done
echo "[entrypoint] dockerd ready after ${elapsed}s"

# --- Load sandbox worker image ---
WORKER_TAR="/opt/.worker-image.tar"
if [ -f "$WORKER_TAR" ]; then
    echo "[entrypoint] Loading sandbox worker image from ${WORKER_TAR}..."
    docker load < "$WORKER_TAR"
    echo "[entrypoint] Sandbox worker image loaded."
else
    echo "[entrypoint] WARNING: ${WORKER_TAR} not found, sandbox may fail."
fi

# --- Start mock LLM server ---
python3 /opt/mock_llm_server.py --port 11434 --host 127.0.0.1 &
MOCK_PID=$!

elapsed=0
while ! curl -sf http://127.0.0.1:11434/v1/models >/dev/null 2>&1; do
    sleep 0.1
    elapsed=$((elapsed + 1))
    if [ "$elapsed" -ge 300 ]; then
        echo "[entrypoint] ERROR: Mock LLM server failed to start" >&2
        exit 1
    fi
done
echo "[entrypoint] Mock LLM server ready on :11434"

# --- Ensure workspace exists ---
mkdir -p /tmp/workspace

# --- Start ironclaw ---
echo "[entrypoint] Starting ironclaw agent..."
cd /tmp/workspace
exec /usr/local/bin/ironclaw
