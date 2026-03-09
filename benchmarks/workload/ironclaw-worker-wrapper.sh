#!/bin/sh
set -eu

WORKSPACE_DIR="${IRONCLAW_WORKSPACE:-/workspace}"
JOB_ID="${IRONCLAW_JOB_ID:-unknown}"
EVIDENCE_DIR="${WORKSPACE_DIR}/.bench-evidence"

mkdir -p "$EVIDENCE_DIR"

TS_MS="$(date +%s%3N)"
HOSTNAME_VALUE="$(hostname)"
cat >"$EVIDENCE_DIR/worker-started-${JOB_ID}.json" <<EOF
{"event":"worker_started","job_id":"${JOB_ID}","ts_unix_ms":${TS_MS},"pid":$$,"hostname":"${HOSTNAME_VALUE}"}
EOF

exec /usr/local/bin/ironclaw worker "$@"
