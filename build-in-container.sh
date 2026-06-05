#!/usr/bin/env bash
# Build all device artifacts inside the toolchain image (toolchain is baked into the image):
#   katapult.bin + klipper.bin (ARM MCU fw), klipper_mcu.elf + c_helper.so (MIPS host).
# Outputs land in the repo (mcu-firmware/, klipper/c_helper/, klipper/klipper_host_mcu/) via
# the bind mount; klipper object files persist there too, so make stays incremental.
#
# Prereq: ./toolchain/build-image.sh
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(pwd)"
IMAGE="${IMAGE:-v3ke-toolchain}"

if ! podman image exists "$IMAGE"; then
  echo "Image '$IMAGE' not found. Run: toolchain/build-image.sh" >&2; exit 1
fi

# The image carries the toolchain + CROSS_TOOLCHAIN + PATH. To iterate with a backed-up toolchain
# from the snapshot volume instead, set XTOOLS_VOL and it gets mounted over /opt/x-tools.
mount_args=()
if [ -n "${XTOOLS_VOL:-}" ]; then
  mount_args+=(-v "${XTOOLS_VOL}":/opt/x-tools:ro)
fi

# Build, then verify the MIPS artifacts match the device ABI (fails the run if not).
exec podman run --rm \
  -v "$REPO":/work -w /work \
  "${mount_args[@]}" \
  "$IMAGE" bash -c './build-bootloader-mcu-and-host-firmware.sh && ./verify-artifacts.sh'
