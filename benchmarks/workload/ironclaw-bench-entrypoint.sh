#!/bin/sh
set -e

emit_event() {
    if [ -z "${BENCH_EVIDENCE_DIR:-}" ]; then
        return
    fi
    python3 - "$BENCH_EVIDENCE_DIR/agent-events.jsonl" "$1" "${BENCH_AGENT_ID:-unknown}" "${BENCH_RUN_ID:-unknown}" "$2" "$3" <<'PY'
import json
import sys
import time

path, event, agent_id, run_id, arg4, arg5 = sys.argv[1:]
payload = {
    "event": event,
    "agent_id": agent_id,
    "run_id": run_id,
    "ts_unix_ms": int(time.time() * 1000),
}
for raw in (arg4, arg5):
    if not raw:
        continue
    key, _, value = raw.partition("=")
    if key:
        payload[key] = value
with open(path, "a") as f:
    f.write(json.dumps(payload) + "\n")
PY
}

# --- Ensure mock LLM server is available ---
if ! curl -sf http://127.0.0.1:11434/v1/models >/dev/null 2>&1; then
    python3 /opt/mock_llm_server.py --port 11434 --host 127.0.0.1 &
fi

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
IRONCLAW_BASE_DIR="${IRONCLAW_BASE_DIR:-/tmp/.ironclaw}"
BENCH_EVIDENCE_DIR="${BENCH_EVIDENCE_DIR:-${IRONCLAW_BASE_DIR}/bench-evidence}"
mkdir -p "$WORKSPACE_DIR"
mkdir -p "$IRONCLAW_BASE_DIR" "$BENCH_EVIDENCE_DIR" "$WORKSPACE_DIR/.bench-evidence"
chown 1000:1000 "$WORKSPACE_DIR" 2>/dev/null || true
chmod 777 "$WORKSPACE_DIR" "$IRONCLAW_BASE_DIR" "$BENCH_EVIDENCE_DIR" "$WORKSPACE_DIR/.bench-evidence" 2>/dev/null || true

AGENT_STORAGE_PROOF="$WORKSPACE_DIR/.bench-evidence/agent-storage-${BENCH_AGENT_ID:-unknown}.txt"
printf 'agent-storage %s %s\n' "${BENCH_AGENT_ID:-unknown}" "$(date +%s)" > "$AGENT_STORAGE_PROOF"
emit_event "agent_storage_written" "path=$AGENT_STORAGE_PROOF" ""

# --- Start ironclaw ---
echo "[entrypoint] Starting ironclaw agent..."
cd "$WORKSPACE_DIR"
/usr/local/bin/ironclaw &
IRONCLAW_PID=$!

trap 'emit_event "agent_exiting" "signal=TERM" ""; kill -TERM "$IRONCLAW_PID" 2>/dev/null || true' INT TERM
trap 'status=$?; emit_event "agent_exited" "exit_code=$status" ""' EXIT

python3 - <<'PY'
import socket
import sys
import time
import os

timeout_s = float(os.environ.get("GATEWAY_READY_TIMEOUT_S", "30"))
deadline = time.time() + timeout_s
while time.time() < deadline:
    try:
        with socket.create_connection(("127.0.0.1", 3000), timeout=0.5):
            sys.exit(0)
    except OSError:
        time.sleep(0.1)
sys.exit(1)
PY

emit_event "agent_started" "pid=$IRONCLAW_PID" "port=3000"

wait "$IRONCLAW_PID"
