#!/bin/bash

# See: https://docs.vorondesign.com/community/howto/drachenkatze/automating_klipper_mcu_updates.html

set -euo pipefail
cd "$(dirname "$0")"

SCRIPT_DIR="$(cd . && pwd)"
# klipper/katapult are vendored as submodules under external/ (see external/ + .gitmodules)
KATAPULT_ROOT_DIR="$(cd external/katapult && pwd)"
KATAPULT_CONFIG="$(cd mcu-firmware/ && pwd)/katapult.config"
KLIPPER_ROOT_DIR="$(cd external/klipper && pwd)"
KLIPPER_CONFIG="$(cd mcu-firmware/ && pwd)/klipper.config"

if [ ! -d "$KATAPULT_ROOT_DIR" ]; then
  echo "KATAPULT_ROOT_DIR: $KATAPULT_ROOT_DIR is not a directory! ABORTING"
  exit 2
fi
if [ ! -f "$KATAPULT_CONFIG" ]; then
  echo "KATAPULT_CONFIG: $KATAPULT_ROOT_DIR is not a file! ABORTING"
  exit 3
fi
if [ ! -d "$KLIPPER_ROOT_DIR" ]; then
  echo "KLIPPER_ROOT_DIR: $KLIPPER_ROOT_DIR is not a directory! ABORTING"
  exit 4
fi
if [ ! -f "$KLIPPER_CONFIG" ]; then
  echo "KLIPPER_CONFIG: $KLIPPER_CONFIG is not a file! ABORTING"
  exit 5
fi

echo ""
echo "Starting katapult build ..."
pushd "$KATAPULT_ROOT_DIR" >/dev/null
make clean KCONFIG_CONFIG="$KATAPULT_CONFIG" >/dev/null
make olddefconfig KCONFIG_CONFIG="$KATAPULT_CONFIG"
make -j$(nproc) KCONFIG_CONFIG="$KATAPULT_CONFIG" >/dev/null
cp -v "${KATAPULT_ROOT_DIR}"/out/katapult.* "${SCRIPT_DIR}/mcu-firmware"
popd >/dev/null
echo "... finished katapult build"
echo ""

echo "Starting klipper build ..."
pushd "$KLIPPER_ROOT_DIR" >/dev/null
make clean KCONFIG_CONFIG="$KLIPPER_CONFIG" >/dev/null
make olddefconfig KCONFIG_CONFIG="$KLIPPER_CONFIG"
make -j$(nproc) KCONFIG_CONFIG="$KLIPPER_CONFIG" >/dev/null
cp -v "${KLIPPER_ROOT_DIR}"/out/klipper.* "${SCRIPT_DIR}/mcu-firmware"
popd >/dev/null
echo "... finished klipper build"
echo ""

echo "Starting klipper host mcu build ..."
klipper/klipper_host_mcu/build-klipper-host-mcu.sh
echo "... finished klipper host mcu build"
echo ""

echo "Starting c_helper build ..."
klipper/c_helper/build-chelper.sh
echo "... finished c_helper build"
echo ""

echo "Finished"
echo ""
