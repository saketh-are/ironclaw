#!/usr/bin/env bash
#
# Build a minimal Alpine ext4 rootfs for Firecracker worker VMs.
# Contains only Python 3, worker.py, and a custom /sbin/init.
#
# Output: benchmarks/firecracker/worker-rootfs.ext4
#
# Usage:
#   bash firecracker/build-rootfs.sh
#
# Requires: Docker, root (for loopback mount)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_DIR="$(dirname "$SCRIPT_DIR")"
ROOTFS_FILE="${SCRIPT_DIR}/worker-rootfs.ext4"
ROOTFS_SIZE_MB=128
BUILD_TAG="fc-rootfs-builder"
FORCE="${FORCE:-0}"
SOURCE_FILES=(
    "${BENCH_DIR}/workload/worker.py"
    "${SCRIPT_DIR}/init"
    "${SCRIPT_DIR}/build-rootfs.sh"
)

if [ -f "$ROOTFS_FILE" ]; then
    if [ "$FORCE" = "1" ]; then
        echo "[build-rootfs] FORCE=1 set; rebuilding ${ROOTFS_FILE}"
        rm -f "$ROOTFS_FILE"
    else
        up_to_date=1
        for src in "${SOURCE_FILES[@]}"; do
            if [ "$src" -nt "$ROOTFS_FILE" ]; then
                up_to_date=0
                break
            fi
        done
        if [ "$up_to_date" -eq 1 ]; then
            echo "[build-rootfs] Rootfs already up to date at ${ROOTFS_FILE}"
            echo "  Set FORCE=1 to rebuild anyway."
            exit 0
        fi
        echo "[build-rootfs] Rebuilding stale rootfs at ${ROOTFS_FILE}"
        rm -f "$ROOTFS_FILE"
    fi
fi

echo "[build-rootfs] Building minimal Alpine rootfs..."

# Step 1: Create a long-lived Docker container with Alpine + Python3.
# The build later uses `docker exec`, so the container must stay running.
CONTAINER_ID=$(docker create --name "$BUILD_TAG" alpine:3.19 sh -c "sleep infinity" 2>/dev/null || true)
if [ -z "$CONTAINER_ID" ]; then
    docker rm -f "$BUILD_TAG" 2>/dev/null || true
    CONTAINER_ID=$(docker create --name "$BUILD_TAG" alpine:3.19 sh -c "sleep infinity")
fi

# Install Python3 inside the container
docker start "$BUILD_TAG"
docker exec "$BUILD_TAG" apk add --no-cache python3 2>/dev/null
docker stop "$BUILD_TAG" 2>/dev/null || true

# Step 2: Export the container filesystem to a tarball
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"; docker rm -f "$BUILD_TAG" 2>/dev/null || true' EXIT

echo "[build-rootfs] Exporting container filesystem..."
docker export "$BUILD_TAG" > "${TMPDIR}/rootfs.tar"

# Step 3: Create an ext4 image and populate it
echo "[build-rootfs] Creating ${ROOTFS_SIZE_MB}MB ext4 image..."
dd if=/dev/zero of="$ROOTFS_FILE" bs=1M count="$ROOTFS_SIZE_MB" status=none
mkfs.ext4 -F -q "$ROOTFS_FILE"

# Mount and populate
MOUNT_DIR="${TMPDIR}/mnt"
mkdir -p "$MOUNT_DIR"
mount -o loop "$ROOTFS_FILE" "$MOUNT_DIR"

echo "[build-rootfs] Populating rootfs..."
tar -xf "${TMPDIR}/rootfs.tar" -C "$MOUNT_DIR"

# Copy worker.py
cp "${BENCH_DIR}/workload/worker.py" "${MOUNT_DIR}/usr/local/bin/worker.py"
chmod 755 "${MOUNT_DIR}/usr/local/bin/worker.py"

# Install custom init (remove symlink first to avoid overwriting busybox)
rm -f "${MOUNT_DIR}/sbin/init"
cp "${SCRIPT_DIR}/init" "${MOUNT_DIR}/sbin/init"
chmod 755 "${MOUNT_DIR}/sbin/init"

# Ensure required directories exist
mkdir -p "${MOUNT_DIR}/proc" "${MOUNT_DIR}/sys" "${MOUNT_DIR}/dev" "${MOUNT_DIR}/tmp"
mkdir -p "${MOUNT_DIR}/workspace"

# Cleanup and unmount
umount "$MOUNT_DIR"

echo "[build-rootfs] Rootfs created at ${ROOTFS_FILE} ($(du -h "$ROOTFS_FILE" | cut -f1))"
