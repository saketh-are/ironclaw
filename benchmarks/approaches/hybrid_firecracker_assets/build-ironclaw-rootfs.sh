#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
ROOTFS_FILE="${SCRIPT_DIR}/ironclaw-worker-rootfs.ext4"
ROOTFS_SIZE_MB=512
BUILD_TAG="fc-ironclaw-rootfs-builder"
FORCE="${FORCE:-0}"
SOURCE_FILES=(
    "${BENCH_DIR}/workload/.ironclaw-bin"
    "${SCRIPT_DIR}/init-ironclaw"
    "${SCRIPT_DIR}/build-ironclaw-rootfs.sh"
)

if [ -f "$ROOTFS_FILE" ]; then
    if [ "$FORCE" = "1" ]; then
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
            echo "[build-ironclaw-rootfs] Rootfs already up to date at ${ROOTFS_FILE}"
            exit 0
        fi
        rm -f "$ROOTFS_FILE"
    fi
fi

CONTAINER_ID=$(docker create --name "$BUILD_TAG" debian:trixie-slim sh -c "sleep infinity" 2>/dev/null || true)
if [ -z "$CONTAINER_ID" ]; then
    docker rm -f "$BUILD_TAG" 2>/dev/null || true
    CONTAINER_ID=$(docker create --name "$BUILD_TAG" debian:trixie-slim sh -c "sleep infinity")
fi

docker start "$BUILD_TAG"
docker exec "$BUILD_TAG" bash -lc 'apt-get update >/dev/null && apt-get install -y --no-install-recommends python3 ca-certificates iproute2 >/dev/null'
docker stop "$BUILD_TAG" >/dev/null 2>&1 || true

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"; docker rm -f "$BUILD_TAG" >/dev/null 2>&1 || true' EXIT

docker export "$BUILD_TAG" > "${TMPDIR}/rootfs.tar"

dd if=/dev/zero of="$ROOTFS_FILE" bs=1M count="$ROOTFS_SIZE_MB" status=none
mkfs.ext4 -F -q "$ROOTFS_FILE"

MOUNT_DIR="${TMPDIR}/mnt"
mkdir -p "$MOUNT_DIR"
mount -o loop "$ROOTFS_FILE" "$MOUNT_DIR"

tar -xf "${TMPDIR}/rootfs.tar" -C "$MOUNT_DIR"
cp "${BENCH_DIR}/workload/.ironclaw-bin" "${MOUNT_DIR}/usr/local/bin/ironclaw"
chmod 755 "${MOUNT_DIR}/usr/local/bin/ironclaw"
rm -f "${MOUNT_DIR}/sbin/init"
cp "${SCRIPT_DIR}/init-ironclaw" "${MOUNT_DIR}/sbin/init"
chmod 755 "${MOUNT_DIR}/sbin/init"
mkdir -p "${MOUNT_DIR}/proc" "${MOUNT_DIR}/sys" "${MOUNT_DIR}/dev" "${MOUNT_DIR}/tmp" "${MOUNT_DIR}/workspace"
umount "$MOUNT_DIR"

echo "[build-ironclaw-rootfs] Rootfs created at ${ROOTFS_FILE}"
