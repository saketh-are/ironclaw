#!/usr/bin/env bash
#
# QEMU/KVM VM launcher for Approach A (vm-per-agent) benchmarking.
#
# Usage:
#   launch.sh start <agent-id> <memory-mb> <cpus> <vm-image> [extra-env...]
#   launch.sh stop <agent-id>
#   launch.sh stop-all
#
# Each VM gets:
#   - A COW overlay on the base image (minimal disk usage)
#   - A unique SSH port (2200 + agent_id_number) for debugging
#   - Config passed via agent-env file on cidata ISO
#
# Prerequisites:
#   - qemu-system-x86_64 with KVM support
#   - /dev/kvm accessible

set -euo pipefail

VM_BASE_DIR="${VM_BASE_DIR:-/tmp/bench-vms}"

request_powerdown() {
    local MONITOR_SOCK="${1:?monitor socket required}"
    python3 - "$MONITOR_SOCK" <<'PY'
import socket
import sys

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.settimeout(2)
sock.connect(sys.argv[1])
sock.sendall(b"system_powerdown\n")
sock.close()
PY
}

emit_agent_exit_event() {
    local VM_DIR="${1:?vm dir required}"
    local DEFAULT_AGENT_ID="${2:?agent id required}"
    local EXIT_CODE="${3:-0}"
    local ENV_FILE="${VM_DIR}/cidata/agent-env"
    local EVENT_LOG="${VM_DIR}/shared/evidence/agent-events.jsonl"

    [ -f "${ENV_FILE}" ] || return 0
    mkdir -p "$(dirname "${EVENT_LOG}")"

    if [ -f "${EVENT_LOG}" ] && grep -q '"event": "agent_exited"' "${EVENT_LOG}"; then
        return 0
    fi

    (
        set -a
        # shellcheck disable=SC1090
        . "${ENV_FILE}"
        set +a
        python3 - "${EVENT_LOG}" "${BENCH_AGENT_ID:-${DEFAULT_AGENT_ID}}" "${BENCH_RUN_ID:-unknown}" "${EXIT_CODE}" <<'PY'
import json
import sys
import time

path, agent_id, run_id, exit_code = sys.argv[1:]
payload = {
    "event": "agent_exited",
    "agent_id": agent_id,
    "run_id": run_id,
    "ts_unix_ms": int(time.time() * 1000),
    "exit_code": exit_code,
}
with open(path, "a") as f:
    f.write(json.dumps(payload) + "\n")
PY
    )
}

ensure_kvm_access() {
    if [ ! -e /dev/kvm ] || [ "${EUID}" -eq 0 ] || [ -w /dev/kvm ]; then
        return 0
    fi

    local CURRENT_USER
    CURRENT_USER="$(id -un)"
    sudo -n setfacl -m "u:${CURRENT_USER}:rw" /dev/kvm
}

start_vm() {
    local AGENT_ID="${1:?agent-id required}"
    local MEMORY_MB="${2:-4096}"
    local CPUS="${3:-2}"
    local VM_IMAGE="${4:?vm-image path required}"
    shift 4

    local VM_DIR="${VM_BASE_DIR}/${AGENT_ID}"
    mkdir -p "$VM_DIR"
    mkdir -p "${VM_DIR}/shared"

    # Create COW overlay
    local OVERLAY="${VM_DIR}/overlay.qcow2"
    qemu-img create -f qcow2 -b "$(realpath "$VM_IMAGE")" -F qcow2 "$OVERLAY" > /dev/null

    # Parse agent numeric ID for port assignment
    local ID_NUM
    ID_NUM=$(echo "$AGENT_ID" | grep -o '[0-9]*$' || echo "0")
    local SSH_PORT=$((2200 + ID_NUM))

    # Build host port forwarding: SSH always, orchestrator if requested
    local ORCH_HOST_PORT="${ORCH_HOST_PORT:-}"
    local GATEWAY_GUEST_PORT="${GATEWAY_GUEST_PORT:-8080}"
    local HOSTFWD="hostfwd=tcp::${SSH_PORT}-:22"
    if [ -n "$ORCH_HOST_PORT" ]; then
        HOSTFWD="${HOSTFWD},hostfwd=tcp::${ORCH_HOST_PORT}-:${GATEWAY_GUEST_PORT}"
    fi

    # Build cloud-init ISO with agent-env config file. The benchmark depends
    # on this ISO for runtime config, so silently skipping it produces invalid
    # runs instead of a usable fallback.
    local ISO_CMD=""
    if command -v genisoimage &>/dev/null; then
        ISO_CMD="genisoimage"
    elif command -v mkisofs &>/dev/null; then
        ISO_CMD="mkisofs"
    else
        echo "ERROR: genisoimage or mkisofs is required to launch benchmark VMs." >&2
        return 1
    fi

    local CLOUD_INIT_ARGS=""
    local CIDATA="${VM_DIR}/cidata.iso"
    mkdir -p "${VM_DIR}/cidata"

    # Write agent config as sourceable env file
    {
        echo "AGENT_ID=\"${AGENT_ID}\""
        for var in "$@"; do
            echo "$var" | sed 's/=\(.*\)/="\1"/'
        done
    } > "${VM_DIR}/cidata/agent-env"

    # Minimal cloud-init meta-data
    cat > "${VM_DIR}/cidata/meta-data" <<META
instance-id: ${AGENT_ID}
local-hostname: ${AGENT_ID}
META

    # Empty user-data (agent is started by OpenRC bench-agent service)
    echo "#cloud-config" > "${VM_DIR}/cidata/user-data"

    "$ISO_CMD" -quiet -output "$CIDATA" -volid cidata -joliet -rock \
        "${VM_DIR}/cidata/agent-env" \
        "${VM_DIR}/cidata/user-data" \
        "${VM_DIR}/cidata/meta-data" 2>/dev/null
    CLOUD_INIT_ARGS="-drive file=${CIDATA},format=raw,if=virtio,readonly=on"

    # Launch QEMU
    ensure_kvm_access
    local MONITOR_SOCK="${VM_DIR}/monitor.sock"
    qemu-system-x86_64 \
        -enable-kvm \
        -m "$MEMORY_MB" \
        -smp "$CPUS" \
        -drive "file=${OVERLAY},format=qcow2,if=virtio" \
        ${CLOUD_INIT_ARGS} \
        -netdev "user,id=net0,${HOSTFWD}" \
        -device virtio-net-pci,netdev=net0 \
        -virtfs "local,path=${VM_DIR}/shared,mount_tag=benchshare,security_model=mapped-xattr,id=bench9p" \
        -monitor "unix:${MONITOR_SOCK},server,nowait" \
        -nographic \
        -serial "file:${VM_DIR}/console.log" \
        > "${VM_DIR}/qemu.log" 2>&1 &

    local PID=$!
    echo "$PID" > "${VM_DIR}/qemu.pid"
    echo "$SSH_PORT" > "${VM_DIR}/ssh_port"

    echo "[vm] Started ${AGENT_ID}: PID=${PID}, memory=${MEMORY_MB}MB, ssh=localhost:${SSH_PORT}"
}

stop_vm() {
    local AGENT_ID="${1:?agent-id required}"
    local VM_DIR="${VM_BASE_DIR}/${AGENT_ID}"

    if [ -f "${VM_DIR}/qemu.pid" ]; then
        local PID
        local EXIT_CODE=0
        PID=$(cat "${VM_DIR}/qemu.pid")
        local MONITOR_SOCK="${VM_DIR}/monitor.sock"
        if [ -S "${MONITOR_SOCK}" ]; then
            request_powerdown "${MONITOR_SOCK}" 2>/dev/null || true
        fi
        # Wait for process to exit
        for _ in $(seq 1 40); do
            kill -0 "$PID" 2>/dev/null || break
            sleep 0.5
        done
        if kill -0 "$PID" 2>/dev/null; then
            kill "$PID" 2>/dev/null || true
            EXIT_CODE=143
        fi
        for _ in $(seq 1 20); do
            kill -0 "$PID" 2>/dev/null || break
            sleep 0.5
        done
        # Force kill if still running
        if kill -0 "$PID" 2>/dev/null; then
            kill -9 "$PID" 2>/dev/null || true
            EXIT_CODE=137
        fi
        emit_agent_exit_event "${VM_DIR}" "${AGENT_ID}" "${EXIT_CODE}"
        rm -f "${VM_DIR}/qemu.pid"
        echo "[vm] Stopped ${AGENT_ID}"
    else
        echo "[vm] ${AGENT_ID} not found"
    fi
}

clean_vm() {
    local AGENT_ID="${1:?agent-id required}"
    local VM_DIR="${VM_BASE_DIR}/${AGENT_ID}"
    rm -rf "$VM_DIR"
    echo "[vm] Cleaned ${AGENT_ID}"
}

clean_all() {
    if [ -d "$VM_BASE_DIR" ]; then
        rm -rf "$VM_BASE_DIR"
        echo "[vm] All VM directories cleaned."
    fi
}

stop_all() {
    if [ -d "$VM_BASE_DIR" ]; then
        for vm_dir in "${VM_BASE_DIR}"/*/; do
            if [ -f "${vm_dir}/qemu.pid" ]; then
                local AGENT_ID
                AGENT_ID=$(basename "$vm_dir")
                stop_vm "$AGENT_ID"
            fi
        done
        echo "[vm] All VMs stopped."
    fi
}

case "${1:?Usage: $0 start|stop|stop-all|clean|clean-all ...}" in
    start)
        shift
        start_vm "$@"
        ;;
    stop)
        shift
        stop_vm "$@"
        ;;
    stop-all)
        stop_all
        ;;
    clean)
        shift
        clean_vm "$@"
        ;;
    clean-all)
        clean_all
        ;;
    *)
        echo "Usage: $0 start|stop|stop-all|clean|clean-all ..."
        exit 1
        ;;
esac
