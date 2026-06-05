#!/usr/bin/env bash
# Flash the MCU (GD32F303RET6, driven as STM32F103) over SWD with openocd + ST-Link.
# Reads the firmware built by build-bootloader-mcu-and-host-firmware.sh into mcu-firmware/.
#
# Hardware: ST-Link wired to the mainboard SWD pads (SWDIO/SWCLK/GND; +3V3 ref). The printer
# must be opened to reach them — see pinout/. There is NO software-only flash on a stock KE.
#
# NOTE: ST-Link V3 needs modern openocd's interface/stlink-dap.cfg (default below). For a
# V2 probe, run with INTERFACE=interface/stlink.cfg.
set -euo pipefail
cd "$(dirname "$0")"
FW="$(pwd)/mcu-firmware"

INTERFACE="${INTERFACE:-interface/stlink-dap.cfg}"
TARGET="${TARGET:-target/stm32f3x.cfg}"   # GD32F303 uses the stm32f3x openocd target
OCD=(openocd -f "$INTERFACE" -f "$TARGET")

usage() { echo "usage: $0 {erase|katapult|klipper|all}" >&2; exit 1; }

erase() {
  echo "== Erasing flash (full chip) =="
  "${OCD[@]}" -c "adapter speed 100; init; halt; reset halt; \
    flash erase_address 0x08000000 0x60000; \
    flash erase_address 0x08060000 0x20000; exit"
}
flash_katapult() {
  echo "== Flashing Katapult bootloader -> 0x08000000 =="
  "${OCD[@]}" -c "init; reset halt; \
    flash write_image erase $FW/katapult.bin 0x08000000; \
    verify_image $FW/katapult.bin 0x08000000; reset run; exit"
}
flash_klipper() {
  echo "== Flashing Klipper firmware -> 0x08002000 (after 8KiB bootloader) =="
  "${OCD[@]}" -c "init; reset halt; \
    flash write_image erase $FW/klipper.bin 0x08002000; \
    verify_image $FW/klipper.bin 0x08002000; reset run; exit"
}

case "${1:-}" in
  erase)    erase ;;
  katapult) flash_katapult ;;
  klipper)  flash_klipper ;;
  all)      erase; flash_katapult; flash_klipper ;;
  *) usage ;;
esac
