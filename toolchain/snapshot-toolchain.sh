#!/usr/bin/env bash
# Snapshot the toolchain baked into the image into a named volume — a dev-cycle safety net so a
# known-good toolchain survives image rebuilds / `podman system prune` without a 20-40 min
# rebuild. The image remains the distributable source of truth; this volume is just a backup.
#
#   ./snapshot-toolchain.sh           # image -> volume  (back up)
#   ./snapshot-toolchain.sh restore   # volume -> a fresh tag, for inspection/use
set -euo pipefail
cd "$(dirname "$0")"
IMAGE="${IMAGE:-v3ke-toolchain}"
XTOOLS_VOL="${XTOOLS_VOL:-v3ke-xtools}"

podman image exists "$IMAGE" || { echo "Image '$IMAGE' not found. Run: ./build-image.sh" >&2; exit 1; }

case "${1:-backup}" in
  backup)
    podman run --rm -v "$XTOOLS_VOL":/backup "$IMAGE" \
      sh -c 'rm -rf /backup/* && cp -a /opt/x-tools/. /backup/'
    echo "Toolchain backed up: image '$IMAGE' -> volume '$XTOOLS_VOL'."
    ;;
  restore)
    # Sanity-check the backed-up toolchain is usable.
    podman run --rm -v "$XTOOLS_VOL":/opt/x-tools:ro "$IMAGE" \
      /opt/x-tools/mipsel-buildroot-linux-gnu/bin/mipsel-buildroot-linux-gnu-gcc --version | head -1
    echo "Volume '$XTOOLS_VOL' holds a working toolchain (mount it at /opt/x-tools to use)."
    ;;
  *) echo "usage: $0 {backup|restore}" >&2; exit 1 ;;
esac
