#!/bin/sh
set -e

# --- Start mock LLM server in background ---
python3 /opt/mock_llm_server.py --port 11434 --host 127.0.0.1 &
MOCK_PID=$!

# Wait for mock LLM to be ready
elapsed=0
while ! curl -sf http://127.0.0.1:11434/v1/models >/dev/null 2>&1; do
    sleep 0.1
    elapsed=$((elapsed + 1))
    if [ "$elapsed" -ge 300 ]; then
        echo "[entrypoint] ERROR: Mock LLM server failed to start after 30s" >&2
        exit 1
    fi
done
echo "[entrypoint] Mock LLM server ready on :11434"

# --- Optional: start Docker socket proxy ---
if [ -n "$DOCKER_PROXY_UPSTREAM" ]; then
    PROXY_ARGS="--listen /var/run/docker.sock --upstream $DOCKER_PROXY_UPSTREAM"
    if [ "$DOCKER_PROXY_REWRITE_IMAGES" = "1" ]; then
        PROXY_ARGS="$PROXY_ARGS --rewrite-images"
    fi
    python3 /opt/docker_socket_proxy.py $PROXY_ARGS &
    sleep 0.5
    echo "[entrypoint] Docker socket proxy started"
fi

# --- Ensure workspace exists and is writable by sandbox user (1000) ---
# WORKSPACE_DIR can be overridden for shared-daemon topologies where the
# sandbox bind mount source path must match a real host path.
WORKSPACE_DIR="${WORKSPACE_DIR:-/tmp/workspace}"
mkdir -p "$WORKSPACE_DIR"
chown 1000:1000 "$WORKSPACE_DIR"

# --- Start ironclaw ---
echo "[entrypoint] Starting ironclaw agent..."
cd "$WORKSPACE_DIR"
exec /usr/local/bin/ironclaw
