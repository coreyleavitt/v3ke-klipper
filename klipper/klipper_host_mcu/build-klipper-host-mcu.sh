#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(dirname "$0")"
cd "$SCRIPT_DIR"
SCRIPT_DIR="$(pwd)"

# Repo root is two levels up from klipper/klipper_host_mcu; klipper submodule + toolchain live under it.
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
KLIPPER_DIR="${REPO_ROOT}/external/klipper"

# In the container image CROSS_TOOLCHAIN points at the crosstool-ng install; otherwise
# fall back to a toolchain checked out under the repo.
TC="${CROSS_TOOLCHAIN:-${REPO_ROOT}/toolchain/mips32r5el--glibc--bleeding-edge-2018.11-1}"
echo "TC: $TC"

if [ ! -e "${TC}/bin/mipsel-buildroot-linux-gnu-gcc" ]; then
  echo "Invalid TC dir: $TC, gcc missing, ABORTING"
  exit 2
fi

export CROSS_BIN="${TC}/mipsel-buildroot-linux-gnu/bin"
export CROSS_PREFIX="${TC}/bin/mipsel-buildroot-linux-gnu-"

pushd "$KLIPPER_DIR" >/dev/null
make clean KCONFIG_CONFIG="${SCRIPT_DIR}/klipper-host-mcu.config" >/dev/null
# Linux process MCU; olddefconfig keeps it non-interactive (config already sets MACH_LINUX)
make olddefconfig KCONFIG_CONFIG="${SCRIPT_DIR}/klipper-host-mcu.config"
make -j$(nproc) KCONFIG_CONFIG="${SCRIPT_DIR}/klipper-host-mcu.config" >/dev/null
popd >/dev/null

"${SCRIPT_DIR}/../read-elf-infos.sh" "${KLIPPER_DIR}/out/klipper.elf" | tail -n 5

cp -v "${KLIPPER_DIR}/out/klipper.elf" "${SCRIPT_DIR}/klipper_mcu.elf"
