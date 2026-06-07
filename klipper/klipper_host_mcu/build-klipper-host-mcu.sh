#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(dirname "$0")"
cd "$SCRIPT_DIR"
SCRIPT_DIR="$(pwd)"

# Repo root is two levels up from klipper/klipper_host_mcu; klipper submodule + toolchain live under it.
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
KLIPPER_DIR="${REPO_ROOT}/external/klipper"

# CROSS_TOOLCHAIN points at the crosstool-ng install (set by the container / tools/build.py).
# Required — there is no in-repo fallback toolchain (the old Bootlin tarball is gone).
TC="${CROSS_TOOLCHAIN:?CROSS_TOOLCHAIN not set — build via tools/build.py artifacts (it runs this in the container)}"
echo "TC: $TC"

if [ ! -e "${TC}/bin/mipsel-buildroot-linux-gnu-gcc" ]; then
  echo "Invalid TC dir: $TC, gcc missing, ABORTING"
  exit 2
fi

# klipper's Makefile builds its compiler as $(CROSS_PREFIX)gcc.
export CROSS_PREFIX="${TC}/bin/mipsel-buildroot-linux-gnu-"

# Subshell (not pushd/popd): a failed make exits the subshell and propagates under set -e without
# leaving the directory stack dirty — consistent with build-bootloader-mcu-and-host-firmware.sh.
(
  cd "$KLIPPER_DIR"
  make clean KCONFIG_CONFIG="${SCRIPT_DIR}/klipper-host-mcu.config" >/dev/null
  # Linux process MCU; olddefconfig keeps it non-interactive (config already sets MACH_LINUX)
  make olddefconfig KCONFIG_CONFIG="${SCRIPT_DIR}/klipper-host-mcu.config"
  make -j"$(nproc)" KCONFIG_CONFIG="${SCRIPT_DIR}/klipper-host-mcu.config" >/dev/null
)

"${SCRIPT_DIR}/../read-elf-infos.sh" "${KLIPPER_DIR}/out/klipper.elf" | tail -n 5

cp -v "${KLIPPER_DIR}/out/klipper.elf" "${SCRIPT_DIR}/klipper_mcu.elf"
