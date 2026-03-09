#!/bin/sh
set -e

# Production DinD entrypoint for IronClaw.
# Starts an inner Docker daemon (via Sysbox), loads the sandbox worker image,
# then hands off to the ironclaw binary.

# --- Start dockerd in background ---
# Sysbox: overlay2 works, iptables work — no extra flags needed.
# gVisor: would need --storage-driver=vfs --iptables=false --ip6tables=false
DOCKERD_ARGS="${DOCKERD_EXTRA_ARGS:-}"

echo "[dind-entrypoint] Starting dockerd... ${DOCKERD_ARGS}"
# shellcheck disable=SC2086
dockerd $DOCKERD_ARGS >/var/log/dockerd.log 2>&1 &

# Wait for dockerd (up to 120s)
elapsed=0
while ! docker info >/dev/null 2>&1; do
    sleep 1
    elapsed=$((elapsed + 1))
    if [ "$elapsed" -ge 120 ]; then
        echo "[dind-entrypoint] ERROR: dockerd did not start within 120s" >&2
        cat /var/log/dockerd.log >&2
        exit 1
    fi
done
echo "[dind-entrypoint] dockerd ready after ${elapsed}s"

# --- Load sandbox worker image ---
WORKER_TAR="/opt/.worker-image.tar"
if [ -f "$WORKER_TAR" ]; then
    echo "[dind-entrypoint] Loading sandbox worker image from ${WORKER_TAR}..."
    docker load < "$WORKER_TAR"
    echo "[dind-entrypoint] Sandbox worker image loaded."
else
    echo "[dind-entrypoint] WARNING: ${WORKER_TAR} not found, sandbox jobs may fail."
fi

# --- Start ironclaw ---
echo "[dind-entrypoint] Starting ironclaw..."

# Forward SIGTERM/SIGINT to ironclaw for graceful shutdown
trap 'echo "[dind-entrypoint] Shutting down..."; kill -TERM "$IRONCLAW_PID" 2>/dev/null || true' INT TERM

/usr/local/bin/ironclaw "$@" &
IRONCLAW_PID=$!

wait "$IRONCLAW_PID"
