#!/usr/bin/env bash
#
# Build an Alpine Linux VM image with Docker pre-installed for benchmarking.
#
# Output: alpine-agent.qcow2
#
# This script uses virt-customize (from libguestfs-tools) to modify an Alpine
# cloud image. If virt-customize is not available, it prints manual instructions.
#
# Prerequisites:
#   - libguestfs-tools (apt install libguestfs-tools)
#   - wget or curl
#   - ~2GB disk space
#
# Usage:
#   ./build-image.sh [output-path]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BENCH_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
OUTPUT="${1:-${SCRIPT_DIR}/alpine-agent.qcow2}"
ALPINE_VERSION="3.19"
ALPINE_ARCH="x86_64"
ALPINE_URL="https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VERSION}/releases/cloud/nocloud_alpine-${ALPINE_VERSION}.0-${ALPINE_ARCH}-bios-cloudinit-r0.qcow2"

echo "=== Building Alpine VM image with Docker ==="
echo "Output: ${OUTPUT}"
echo ""

# Download Alpine cloud image if not cached
CACHED_IMAGE="${SCRIPT_DIR}/.alpine-base.qcow2"
if [ ! -f "$CACHED_IMAGE" ]; then
    echo "Downloading Alpine cloud image..."
    curl -fSL -o "$CACHED_IMAGE" "$ALPINE_URL"
    echo "Downloaded: $CACHED_IMAGE"
else
    echo "Using cached base image: $CACHED_IMAGE"
fi

# Copy base image to output
cp "$CACHED_IMAGE" "$OUTPUT"

# Resize to accommodate Docker + images
qemu-img resize "$OUTPUT" 4G

# Check for virt-customize
if ! command -v virt-customize &>/dev/null; then
    echo ""
    echo "ERROR: virt-customize not found."
    echo ""
    echo "Install it with: sudo apt install libguestfs-tools"
    echo ""
    echo "Or build the image manually:"
    echo "  1. Boot the image: qemu-system-x86_64 -enable-kvm -m 2048 -drive file=${OUTPUT},format=qcow2 -nographic"
    echo "  2. Log in as root (no password)"
    echo "  3. Run: apk add docker python3 py3-pip bash coreutils && pip install docker && rc-update add docker default"
    echo "  4. Copy agent.py and worker.py into /usr/local/bin/"
    echo "  5. docker pull and save the worker image"
    echo "  6. Shut down and use the image"
    exit 1
fi

echo "Customizing image with virt-customize..."

# Build the worker image tarball to bake into the VM. Always refresh it so the
# guest cannot silently run a stale worker image after host-side code changes.
WORKER_TAR="${SCRIPT_DIR}/.worker-image.tar"
echo "Saving worker Docker image to tarball..."
docker save bench-worker:latest -o "$WORKER_TAR" 2>/dev/null || {
    echo "ERROR: bench-worker:latest not found. Run 'make images' first."
    exit 1
}

# Create the bench-agent OpenRC init script (starts after docker, reads config from cidata)
AGENT_INITD=$(mktemp)
cat > "$AGENT_INITD" <<'INITEOF'
#!/sbin/openrc-run

description="Benchmark agent process"
depend() {
    need docker
    after docker
}

start() {
    ebegin "Starting benchmark agent"

    # Wait for Docker socket to actually be usable (daemon may still be initializing)
    local n=0
    while [ $n -lt 120 ]; do
        if [ -S /var/run/docker.sock ] && docker info >/dev/null 2>&1; then
            break
        fi
        n=$((n + 1))
        sleep 1
    done

    if ! docker info >/dev/null 2>&1; then
        eerror "Docker daemon not ready after 120s"
        eend 1
        return 1
    fi

    # Load pre-baked worker image if present
    if [ -f /opt/.worker-image.tar ]; then
        docker load < /opt/.worker-image.tar 2>/dev/null
        rm -f /opt/.worker-image.tar
    fi

    # Detect Docker bridge gateway for worker callback URL.
    # In VM mode, the default route is the QEMU NAT gateway (10.0.2.2),
    # not the Docker bridge. Workers need to reach the agent via docker0.
    DOCKER_BRIDGE_GATEWAY=$(docker network inspect bridge --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null || echo "172.17.0.1")
    export DOCKER_BRIDGE_GATEWAY

    # Mount 9p shared directory for host-accessible JSONL output
    mkdir -p /mnt/benchshare
    mount -t 9p -o trans=virtio benchshare /mnt/benchshare 2>/dev/null || true

    # Read config from cidata volume (mounted by cloud-init or manually)
    local config_file=""
    for f in /media/cidata/agent-env /run/cidata/agent-env /mnt/cidata/agent-env; do
        if [ -f "$f" ]; then
            config_file="$f"
            break
        fi
    done

    # Also try mounting cidata if not already mounted
    if [ -z "$config_file" ]; then
        mkdir -p /mnt/cidata
        mount -L cidata /mnt/cidata 2>/dev/null || true
        if [ -f /mnt/cidata/agent-env ]; then
            config_file="/mnt/cidata/agent-env"
        fi
    fi

    if [ -n "$config_file" ]; then
        . "$config_file"
        export AGENT_ID BENCHMARK_MODE AGENT_BASELINE_MB SPAWN_INTERVAL_MEAN_S MAX_CONCURRENT_WORKERS
        export BENCHMARK_DURATION_S WORKER_IMAGE WORKER_MEMORY_LIMIT_MB WORKER_MEMORY_MB
        export WORKER_DURATION_MIN_S WORKER_DURATION_MAX_S WORKER_LIFETIME_MODE
        export PLATEAU_WORKERS_PER_AGENT PLATEAU_HOLD_S PLATEAU_SETTLE_S
        export RNG_SEED BENCH_RUN_ID BENCH_APPROACH
        export ORCHESTRATOR_PORT DOCKER_BRIDGE_GATEWAY
    fi

    # stdout (JSONL events) -> 9p share (clean, parseable by host)
    # stderr (tracebacks, debug) -> serial console (debug artifact)
    start-stop-daemon --start --background \
        --make-pidfile --pidfile /var/run/bench-agent.pid \
        --stdout /mnt/benchshare/agent.jsonl --stderr /dev/ttyS0 \
        --exec /usr/bin/python3 -- /usr/local/bin/agent.py

    eend $?
}

stop() {
    ebegin "Stopping benchmark agent"
    start-stop-daemon --stop --pidfile /var/run/bench-agent.pid 2>/dev/null
    eend $?
}
INITEOF

# Customize the image
CUSTOMIZE_ARGS=(
    --format qcow2
    -a "$OUTPUT"
    # Install packages (including Docker Python SDK for agent.py)
    --run-command "apk add --no-cache docker python3 py3-pip bash coreutils"
    --run-command "pip3 install --break-system-packages docker"
    # Configure cgroups (hybrid mode for Docker container support inside VM)
    --run-command "rc-update add cgroups default"
    --run-command "mkdir -p /etc/conf.d && echo 'rc_cgroup_mode=\"hybrid\"' > /etc/conf.d/cgroups"
    # Enable Docker daemon (starts after cgroups)
    --run-command "rc-update add docker default"
    # Configure Docker
    --run-command "mkdir -p /etc/docker"
    --write '/etc/docker/daemon.json:{"storage-driver":"overlay2","log-driver":"json-file","log-opts":{"max-size":"10m"}}'
    # Disable swap inside guest
    --run-command "swapoff -a 2>/dev/null; sed -i '/swap/d' /etc/fstab 2>/dev/null || true"
    # Copy scripts
    --copy-in "${BENCH_DIR}/workload/agent.py:/usr/local/bin/"
    --copy-in "${BENCH_DIR}/workload/worker.py:/usr/local/bin/"
    # Enable serial console
    --run-command "sed -i 's/^#ttyS0/ttyS0/' /etc/inittab || true"
    # Install bench-agent OpenRC service
    --upload "${AGENT_INITD}:/etc/init.d/bench-agent"
    --run-command "chmod +x /etc/init.d/bench-agent"
    --run-command "rc-update add bench-agent default"
)

# Pre-load worker image if available
if [ -n "${WORKER_TAR:-}" ] && [ -f "$WORKER_TAR" ]; then
    CUSTOMIZE_ARGS+=(
        --copy-in "${WORKER_TAR}:/opt/"
    )
fi

virt-customize "${CUSTOMIZE_ARGS[@]}"

rm -f "$AGENT_INITD"

echo ""
echo "VM image built successfully: ${OUTPUT}"
echo "Size: $(du -h "$OUTPUT" | cut -f1)"
