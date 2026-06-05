## v3ke — Ender 3 V3 KE mainline toolkit (hardware side). Single static binary.
## Build:  nim c -d:release --opt:size -o:v3ke tools/v3ke/v3ke.nim
import std/os
import common, flash, verify, deploy, rollout

proc usage() =
  echo """v3ke — Ender 3 V3 KE mainline toolkit (hardware side)

usage:
  v3ke verify [<c_helper.so> <klipper_mcu.elf>]
        Verify the MIPS host artifacts match the device ABI
        (mipsel / nan2008 / o32 / mips32r2, loader ld-linux-mipsn8.so.1).

  v3ke deploy [<c_helper.so> <klipper_mcu.elf>]
        Push host artifacts to a staging dir on the device and validate they load
        against the device's own glibc/Python. Non-destructive. Env: V3KE_HOST (default v3ke).

  v3ke flash {backup [file] | katapult | klipper | all}
        Flash the motion MCU over SWD (openocd + ST-Link). `all` backs up the
        stock firmware, confirms, erases, flashes, and read-back-verifies.
        Env: FW_DIR (default mcu-firmware), V3KE_INTERFACE (default interface/stlink-dap.cfg).

  v3ke rollout
        Switch to mainline: verify -> deploy -> flash (with gates). The final live-system
        swap stays manual per DEPLOY.md.

  v3ke help
"""

when isMainModule:
  let args = commandLineParams()
  if args.len == 0: usage(); quit(0)
  try:
    case args[0]
    of "verify":  quit(verifyCmd(args[1 .. ^1]))
    of "deploy":  quit(deployCmd(args[1 .. ^1]))
    of "flash":   quit(flashCmd(args[1 .. ^1]))
    of "rollout": quit(rolloutCmd(args[1 .. ^1]))
    of "help", "-h", "--help": usage(); quit(0)
    else:
      errln("unknown command: " & args[0]); usage(); quit(1)
  except V3keError as e:
    errln(e.msg); quit(1)
