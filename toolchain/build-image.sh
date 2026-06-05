#!/usr/bin/env bash
# Build the image, INCLUDING the cross toolchain baked in (~20-40 min the first time; build-time
# cache mounts make rebuilds cheap). The image is self-contained / distributable.
set -euo pipefail
cd "$(dirname "$0")"
IMAGE="${IMAGE:-v3ke-toolchain}"
exec podman build -t "$IMAGE" -f Containerfile .
