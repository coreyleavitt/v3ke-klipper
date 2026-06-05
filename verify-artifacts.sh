#!/usr/bin/env bash
# Verify the cross-built MIPS host artifacts actually match the device ABI before they ever
# reach the printer: mipsel + nan2008 + o32, and (for the dynamic exe) the device's loader
# ld-linux-mipsn8.so.1. The ARM MCU firmware (mcu-firmware/*.bin) is bare-metal and not checked
# here. Runs anywhere readelf is available (inside the toolchain image or on the host).
set -uo pipefail
cd "$(dirname "$0")"

rc=0
verify_mips() {   # <file> [required-interpreter]
  local f="$1" want="${2:-}"
  if [ ! -f "$f" ]; then echo "MISSING: $f"; rc=1; return; fi
  local mach flags
  mach=$(readelf -h "$f" | sed -n 's/^[[:space:]]*Machine:[[:space:]]*//p')
  flags=$(readelf -h "$f" | sed -n 's/^[[:space:]]*Flags:[[:space:]]*//p')
  echo "$f"
  echo "    Machine: $mach"
  echo "    Flags:   $flags"
  echo "$mach"  | grep -qi 'mips'    || { echo "    !! not MIPS";    rc=1; }
  echo "$flags" | grep -qi 'nan2008' || { echo "    !! not nan2008"; rc=1; }
  echo "$flags" | grep -qiw 'o32'    || { echo "    !! not o32";     rc=1; }
  if [ -n "$want" ]; then
    if readelf -l "$f" | grep -q "$want"; then
      echo "    Interp:  $want OK"
    else
      echo "    !! interpreter is not $want"; rc=1
    fi
  fi
}

echo "=== ABI verification (must match device: mipsel / nan2008 / o32, loader ld-linux-mipsn8.so.1) ==="
verify_mips klipper/c_helper/c_helper.so
verify_mips klipper/klipper_host_mcu/klipper_mcu.elf ld-linux-mipsn8.so.1

if [ "$rc" -eq 0 ]; then echo "ABI VERIFICATION PASSED"; else echo "ABI VERIFICATION FAILED"; fi
exit "$rc"
