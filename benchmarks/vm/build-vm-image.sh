#!/bin/bash
set -euo pipefail

# Build ironclaw benchmark VM image using virt-builder.
#
# Creates a Debian 12 (bookworm) QEMU/KVM image with:
#   - Docker CE daemon
#   - ironclaw binary (pre-compiled)
#   - mock LLM server
#   - sandbox worker image (pre-loaded into Docker)
#   - Auto-start ironclaw on boot via systemd
#
# Prerequisites:
#   - virt-builder, virt-customize (libguestfs-tools)
#   - ironclaw binary at workload/.ironclaw-bin
#   - ironclaw-bench-sandbox:latest Docker image (for tarball)
#
# Usage:
#   sudo bash build-vm-image.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BENCH_DIR="$(dirname "$SCRIPT_DIR")"
WORKLOAD_DIR="$BENCH_DIR/workload"
VM_IMAGE="$SCRIPT_DIR/ironclaw-agent.qcow2"
if [ "${EUID}" -eq 0 ]; then
    SUDO=""
else
    SUDO="sudo"
fi

IRONCLAW_BIN="$WORKLOAD_DIR/.ironclaw-bin"
MOCK_LLM="$WORKLOAD_DIR/mock_llm_server.py"
ENTRYPOINT="$WORKLOAD_DIR/ironclaw-bench-entrypoint.sh"
SANDBOX_TAR="/tmp/ironclaw-bench-sandbox-vm.tar"

echo "=== Building ironclaw benchmark VM image ==="

# Verify prerequisites
if [ ! -f "$IRONCLAW_BIN" ]; then
    echo "ERROR: ironclaw binary not found at $IRONCLAW_BIN"
    echo "Build with: cargo build --release --no-default-features --features libsql"
    exit 1
fi

if [ ! -f "$MOCK_LLM" ]; then
    echo "ERROR: mock_llm_server.py not found at $MOCK_LLM"
    exit 1
fi

if [ ! -f "$ENTRYPOINT" ]; then
    echo "ERROR: ironclaw-bench-entrypoint.sh not found at $ENTRYPOINT"
    exit 1
fi

# Save sandbox image to tarball
echo "--- Saving sandbox image to tarball ---"
docker save -o "$SANDBOX_TAR" ironclaw-bench-sandbox:latest

# Build the base VM image
echo "--- Building base VM with virt-builder ---"
${SUDO} virt-builder debian-12 \
    --output "$VM_IMAGE" \
    --format qcow2 \
    --size 8G \
    --no-network \
    --root-password password:root \
    --run-command "mkdir -p /opt/ironclaw" \
    --copy-in "$IRONCLAW_BIN:/usr/local/bin/" \
    --run-command "mv /usr/local/bin/.ironclaw-bin /usr/local/bin/ironclaw && chmod +x /usr/local/bin/ironclaw" \
    --copy-in "$MOCK_LLM:/opt/" \
    --copy-in "$ENTRYPOINT:/usr/local/bin/" \
    --run-command "chmod +x /usr/local/bin/ironclaw-bench-entrypoint.sh" \
    --copy-in "$SANDBOX_TAR:/opt/ironclaw/"

# Create the systemd service and boot script
echo "--- Customizing VM with virt-customize ---"

# Create ironclaw systemd service (boot.sh handles everything)
cat > /tmp/ironclaw-bench.service << 'SVCEOF'
[Unit]
Description=IronClaw Benchmark Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/opt/ironclaw/boot.sh
Restart=no
EnvironmentFile=/etc/ironclaw-bench.env

[Install]
WantedBy=multi-user.target
SVCEOF

# Create boot script that sets up everything and starts ironclaw
cat > /tmp/ironclaw-boot.sh << 'BOOTEOF'
#!/bin/bash
set -euo pipefail

# Host-visible benchmark share (9p) and per-run env via cidata ISO.
mkdir -p /mnt/benchshare /mnt/cidata
mount -t 9p -o trans=virtio,version=9p2000.L benchshare /mnt/benchshare 2>/dev/null || true
mount -L cidata /mnt/cidata 2>/dev/null || true

if [ -f /mnt/cidata/agent-env ]; then
    set -a
    . /mnt/cidata/agent-env
    set +a
fi

if mountpoint -q /mnt/benchshare; then
    export WORKSPACE_DIR="${WORKSPACE_DIR:-/mnt/benchshare/workspace}"
    export IRONCLAW_BASE_DIR="${IRONCLAW_BASE_DIR:-/mnt/benchshare/ironclaw}"
    export BENCH_EVIDENCE_DIR="${BENCH_EVIDENCE_DIR:-/mnt/benchshare/evidence}"
else
    export WORKSPACE_DIR="${WORKSPACE_DIR:-/tmp/workspace}"
    export IRONCLAW_BASE_DIR="${IRONCLAW_BASE_DIR:-/tmp/.ironclaw}"
    export BENCH_EVIDENCE_DIR="${BENCH_EVIDENCE_DIR:-/tmp/.ironclaw/bench-evidence}"
fi

export DEBIAN_FRONTEND=noninteractive
if ! command -v docker >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
    apt-get update
    apt-get install -y docker.io python3 curl ca-certificates
fi

systemctl enable docker >/dev/null 2>&1 || true
systemctl start docker

# Load sandbox worker image into Docker
SANDBOX_TAR="/opt/ironclaw/ironclaw-bench-sandbox-vm.tar"
if [ -f "$SANDBOX_TAR" ] && ! docker image inspect ironclaw-bench-sandbox:latest >/dev/null 2>&1; then
    echo "[boot] Loading sandbox worker image..."
    docker load < "$SANDBOX_TAR"
    echo "[boot] Sandbox image loaded."
fi

# Start mock LLM server
if ! curl -sf http://127.0.0.1:11434/v1/models >/dev/null 2>&1; then
    python3 /opt/mock_llm_server.py --port 11434 --host 127.0.0.1 &
    # Wait for it
    for i in $(seq 1 60); do
        if curl -sf http://127.0.0.1:11434/v1/models >/dev/null 2>&1; then
            echo "[boot] Mock LLM ready"
            break
        fi
        sleep 0.5
    done
fi

echo "[boot] Ready for ironclaw"

# Start the shared benchmark entrypoint (writes host-visible evidence).
exec /usr/local/bin/ironclaw-bench-entrypoint.sh
BOOTEOF

# Create default env file
cat > /tmp/ironclaw-bench.env << 'ENVEOF'
ONBOARD_COMPLETED=true
DATABASE_BACKEND=libsql
LIBSQL_PATH=/tmp/ironclaw-agent.db
LLM_BACKEND=openai_compatible
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_API_KEY=mock-key
LLM_MODEL=mock-bench
GATEWAY_ENABLED=true
GATEWAY_HOST=0.0.0.0
GATEWAY_PORT=3000
GATEWAY_AUTH_TOKEN=bench-token
GATEWAY_USER_ID=benchmark
ALLOW_LOCAL_TOOLS=true
AGENT_AUTO_APPROVE_TOOLS=true
SANDBOX_ENABLED=true
SANDBOX_IMAGE=ironclaw-bench-sandbox:latest
SANDBOX_AUTO_PULL=false
SANDBOX_POLICY=workspace_write
CLI_ENABLED=false
HEARTBEAT_ENABLED=false
ROUTINES_ENABLED=false
SKILLS_ENABLED=false
EMBEDDING_ENABLED=false
HTTP_WEBHOOK_ENABLED=false
AGENT_NAME=bench-vm
RUST_LOG=ironclaw=debug
ENVEOF

${SUDO} virt-customize --no-network -a "$VM_IMAGE" \
    --copy-in /tmp/ironclaw-bench.service:/etc/systemd/system/ \
    --copy-in /tmp/ironclaw-boot.sh:/opt/ironclaw/ \
    --run-command "mv /opt/ironclaw/ironclaw-boot.sh /opt/ironclaw/boot.sh && chmod +x /opt/ironclaw/boot.sh" \
    --copy-in /tmp/ironclaw-bench.env:/etc/ \
    --run-command "systemctl enable ironclaw-bench.service" \
    --run-command "echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf" \
    --run-command "echo -e 'auto ens3\niface ens3 inet dhcp\nauto enp0s3\niface enp0s3 inet dhcp' >> /etc/network/interfaces"

# Clean up temp files
rm -f /tmp/ironclaw-bench.service /tmp/ironclaw-boot.sh /tmp/ironclaw-bench.env "$SANDBOX_TAR"

echo "=== VM image built: $VM_IMAGE ==="
echo "Size: $(du -h "$VM_IMAGE" | cut -f1)"
