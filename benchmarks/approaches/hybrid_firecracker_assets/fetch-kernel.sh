#!/usr/bin/env bash
#
# Download a pre-built vmlinux kernel for Firecracker microVMs.
# Cached locally at benchmarks/approaches/hybrid_firecracker_assets/vmlinux-5.10.bin.
#
# The kernel comes from Firecracker's quickstart guide S3 bucket (the GitHub
# releases no longer ship kernel binaries as of v1.7.0).
#
# Usage:
#   bash approaches/hybrid_firecracker_assets/fetch-kernel.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KERNEL_FILE="${SCRIPT_DIR}/vmlinux-5.10.bin"

KERNEL_URL="https://s3.amazonaws.com/spec.ccfc.min/img/quickstart_guide/x86_64/kernels/vmlinux.bin"

if [ -f "$KERNEL_FILE" ]; then
    echo "[fetch-kernel] Kernel already cached at ${KERNEL_FILE}"
    exit 0
fi

echo "[fetch-kernel] Downloading vmlinux kernel for Firecracker..."
curl -fSL -o "$KERNEL_FILE" "$KERNEL_URL"
chmod 644 "$KERNEL_FILE"
echo "[fetch-kernel] Saved to ${KERNEL_FILE} ($(du -h "$KERNEL_FILE" | cut -f1))"
