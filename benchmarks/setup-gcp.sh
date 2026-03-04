#!/usr/bin/env bash
#
# One-time setup for running benchmarks on a GCP VM.
#
# Prerequisites:
#   - Ubuntu 22.04 GCP VM with nested virtualization enabled
#   - Run as root or with sudo
#
# Usage:
#   sudo bash setup-gcp.sh
#
# After setup, run benchmarks as your normal user:
#   make images
#   make run APPROACH=container-docker AGENTS=5

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo: sudo bash setup-gcp.sh"
    exit 1
fi

REAL_USER="${SUDO_USER:-$(whoami)}"

echo "============================================"
echo " GCP Benchmark VM Setup"
echo "============================================"
echo ""

# --- System packages ---
echo "[1/9] Installing system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    qemu-system-x86 \
    qemu-utils \
    libguestfs-tools \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    git \
    jq \
    > /dev/null

echo "  Done."

# --- Docker ---
echo "[2/9] Installing Docker..."
if command -v docker &>/dev/null; then
    echo "  Docker already installed: $(docker --version)"
else
    curl -fsSL https://get.docker.com | sh -s -- --quiet
    echo "  Installed: $(docker --version)"
fi

# Add user to docker group so they can run without sudo
usermod -aG docker "$REAL_USER"
echo "  Added $REAL_USER to docker group."

# Ensure Docker is running
systemctl enable docker
systemctl start docker

# --- Python dependencies ---
echo "[3/9] Installing Python dependencies..."
pip3 install -q matplotlib docker
echo "  Done."

# --- Verify KVM ---
echo "[4/9] Verifying KVM support..."
if [ -e /dev/kvm ]; then
    echo "  /dev/kvm found."
    # Ensure the user can access KVM
    chmod 666 /dev/kvm
    echo "  KVM access granted."
else
    echo "  WARNING: /dev/kvm not found!"
    echo "  The VM approach (vm-qemu) will not work."
    echo "  Make sure the GCP instance was created with --enable-nested-virtualization."
    echo "  Continuing anyway (container-docker approach will still work)."
fi

# --- Host tuning ---
echo "[5/9] Applying host tuning for accurate measurements..."

# Disable transparent huge pages
if [ -f /sys/kernel/mm/transparent_hugepage/enabled ]; then
    echo never > /sys/kernel/mm/transparent_hugepage/enabled
    echo "  THP disabled."
fi

# Disable kernel same-page merging
if [ -f /sys/kernel/mm/ksm/run ]; then
    echo 0 > /sys/kernel/mm/ksm/run
    echo "  KSM disabled."
fi

# Disable swap (critical for accurate density measurements)
if swapon --show | grep -q .; then
    swapoff -a
    echo "  Swap disabled."
    # Persist: comment out swap entries in fstab
    sed -i '/\bswap\b/s/^/#/' /etc/fstab 2>/dev/null || true
    echo "  Swap disabled in /etc/fstab."
else
    echo "  Swap already disabled."
fi

# Make tuning persistent across reboots
cat > /etc/systemd/system/bench-tuning.service <<'EOF'
[Unit]
Description=Benchmark host tuning (disable THP, KSM, swap)
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'echo never > /sys/kernel/mm/transparent_hugepage/enabled; echo 0 > /sys/kernel/mm/ksm/run; swapoff -a'
RemainAfterExit=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable bench-tuning.service > /dev/null 2>&1
echo "  Tuning persisted via systemd."

# --- Podman (rootless containers) ---
echo "[6/9] Installing Podman..."
if command -v podman &>/dev/null; then
    echo "  Podman already installed: $(podman --version)"
else
    apt-get install -y -qq podman uidmap systemd-container dbus-user-session > /dev/null
    echo "  Installed: $(podman --version)"
fi

# --- Firecracker ---
echo "[7/9] Installing Firecracker..."
FC_VERSION="v1.7.0"
FC_ARCH="x86_64"
if command -v firecracker &>/dev/null; then
    echo "  Firecracker already installed: $(firecracker --version 2>&1 | head -1)"
else
    FC_URL="https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-${FC_ARCH}.tgz"
    TMPFC=$(mktemp -d)
    curl -fsSL "$FC_URL" | tar -xz -C "$TMPFC"
    install -m 755 "${TMPFC}/release-${FC_VERSION}-${FC_ARCH}/firecracker-${FC_VERSION}-${FC_ARCH}" /usr/local/bin/firecracker
    rm -rf "$TMPFC"
    echo "  Installed: $(firecracker --version 2>&1 | head -1)"
fi

# --- Drop caches (do this last, after all installs) ---
echo "[8/9] Dropping page caches..."
echo 3 > /proc/sys/vm/drop_caches
echo "  Done."

# --- Summary ---
echo "[9/9] Verifying installation..."
echo ""
echo "  Docker:          $(docker --version 2>/dev/null || echo 'NOT FOUND')"
echo "  Podman:          $(podman --version 2>/dev/null || echo 'NOT FOUND')"
echo "  QEMU:            $(qemu-system-x86_64 --version 2>/dev/null | head -1 || echo 'NOT FOUND')"
echo "  Firecracker:     $(firecracker --version 2>&1 | head -1 || echo 'NOT FOUND')"
echo "  virt-customize:  $(virt-customize --version 2>/dev/null || echo 'NOT FOUND')"
echo "  Python:          $(python3 --version 2>/dev/null || echo 'NOT FOUND')"
echo "  matplotlib:      $(python3 -c 'import matplotlib; print(matplotlib.__version__)' 2>/dev/null || echo 'NOT FOUND')"
echo "  KVM:             $([ -e /dev/kvm ] && echo 'available' || echo 'NOT AVAILABLE')"
echo ""
echo "============================================"
echo " Setup complete!"
echo "============================================"
echo ""
echo " NOTE: Log out and back in for docker group"
echo " membership to take effect, or run:"
echo ""
echo "   newgrp docker"
echo ""
echo " Then build images and run benchmarks:"
echo ""
echo "   cd benchmarks/"
echo "   make images"
echo "   make run APPROACH=container-docker AGENTS=5"
echo ""
echo " For the VM approach:"
echo ""
echo "   make vm-image"
echo "   make run APPROACH=vm-qemu AGENTS=5"
echo ""
echo " For the Podman rootless approach:"
echo ""
echo "   make podman-setup"
echo "   make run APPROACH=podman-rootless AGENTS=5"
echo ""
echo " For the hybrid Firecracker approach:"
echo ""
echo "   make fc-setup"
echo "   make run APPROACH=hybrid-firecracker AGENTS=5"
echo ""
