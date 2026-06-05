## `v3ke deploy` — push the MIPS host artifacts to a staging dir on the device and validate they
## actually load against the device's own glibc 2.29 / Python. NON-DESTRUCTIVE: it stages + tests,
## it does not switch the live klipper (that's `rollout`, and it requires the MCU flashed to match).
import std/[strformat, strutils]
import common, sshdev

const StageDir* = "/usr/data/v3ke-staging"   # persistent partition; survives reboots

proc deployCmd*(args: seq[string]): int =
  var chelper = "klipper/c_helper/c_helper.so"
  var hostmcu = "klipper/klipper_host_mcu/klipper_mcu.elf"
  if args.len == 2:
    chelper = args[0]; hostmcu = args[1]
  elif args.len != 0:
    echo "usage: v3ke deploy [<c_helper.so> <klipper_mcu.elf>]"; return 1

  echo &"=== deploy host artifacts -> {sshHost()}:{StageDir} (staging; non-destructive) ==="
  discard runRemote(&"mkdir -p {StageDir}")
  pushFile(chelper, StageDir & "/c_helper.so")
  pushFile(hostmcu, StageDir & "/klipper_mcu.elf")
  discard runRemote(&"chmod +x {StageDir}/klipper_mcu.elf")

  note("validating c_helper.so loads into the device's klippy-env Python...")
  let py = runRemote("ls /usr/share/klippy-env/bin/python* 2>/dev/null | head -1").strip()
  if py.len == 0: fail("device klippy-env python not found")
  let probe = &"""{py} -c 'import ctypes; ctypes.CDLL("{StageDir}/c_helper.so"); print("LOADED")'"""
  if "LOADED" notin runRemote(probe): fail("c_helper.so failed to load on device")
  ok("c_helper.so loaded into device Python (ABI matches the live glibc)")

  note("validating klipper_mcu.elf starts on the device kernel...")
  let mcuOut = runRemote(&"cd {StageDir}; ./klipper_mcu.elf >o.txt 2>&1 & P=$!; " &
                         "sleep 2; kill $P 2>/dev/null; cat o.txt; rm -f o.txt")
  if "too old" in mcuOut.toLowerAscii: fail(&"klipper_mcu.elf rejected by device kernel:\n{mcuOut}")
  ok("klipper_mcu.elf runs on the device kernel")

  okBanner("DEPLOY (staging) OK — host artifacts validated on the real device")
  return 0
