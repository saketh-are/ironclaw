#!/usr/bin/env bash
#
# Download Firecracker's pre-built vmlinux kernel from GitHub releases.
# Cached locally at benchmarks/firecracker/vmlinux-5.10.bin.
#
# Usage:
#   bash firecracker/fetch-kernel.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KERNEL_FILE="${SCRIPT_DIR}/vmlinux-5.10.bin"

FC_VERSION="v1.7.0"
KERNEL_URL="https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/vmlinux-5.10-x86_64.bin"

if [ -f "$KERNEL_FILE" ]; then
    echo "[fetch-kernel] Kernel already cached at ${KERNEL_FILE}"
    exit 0
fi

echo "[fetch-kernel] Downloading vmlinux 5.10 from Firecracker ${FC_VERSION}..."
curl -fSL -o "$KERNEL_FILE" "$KERNEL_URL"
chmod 644 "$KERNEL_FILE"
echo "[fetch-kernel] Saved to ${KERNEL_FILE} ($(du -h "$KERNEL_FILE" | cut -f1))"
