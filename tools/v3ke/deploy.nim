## `v3ke deploy` — push the MIPS host artifacts to a staging dir on the device and validate they
## actually load against the device's own glibc 2.29 / Python. NON-DESTRUCTIVE: it stages + tests,
## it does not switch the live klipper (that's `rollout`, and it requires the MCU flashed to match).
import std/[strformat, strutils]
import common, sshdev

const StageDir* = "/usr/data/v3ke-staging"   # persistent partition; survives reboots

proc defaultDeployArtifacts*(): tuple[chelper, hostElf: string] =
  ## Return the default artifact paths that deployCmd uses when invoked with no
  ## arguments.  These match the release-zip layout (host/ directory).
  ## Exposed as a pure proc so tests can assert the contract without inspecting source text.
  (chelper: "host/c_helper.so", hostElf: "host/klipper.elf")

proc deployCmd*(args: seq[string]): int =
  let defaults = defaultDeployArtifacts()
  var chelper = defaults.chelper
  var hostmcu = defaults.hostElf
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
  # py is device-controlled output — shell-quote it before reusing it as a command.
  let probe = &"""{shQuote(py)} -c 'import ctypes; ctypes.CDLL("{StageDir}/c_helper.so"); print("LOADED")'"""
  if "LOADED" notin runRemote(probe): fail("c_helper.so failed to load on device")
  ok("c_helper.so loaded into device Python (ABI matches the live glibc)")

  # Ask the device's OWN dynamic loader whether it can load the ELF — a precise ABI check that
  # does NOT execute the binary (no sleep race, no orphaned process; `ldd` isn't on this BusyBox).
  note("validating klipper_mcu.elf is loadable by the device's glibc loader...")
  const Loader = "/lib/ld-linux-mipsn8.so.1"
  let v = runRemote(&"{Loader} --verify {StageDir}/klipper_mcu.elf && echo VOK || echo VFAIL")
  if "VOK" notin v: fail(&"klipper_mcu.elf not loadable by the device loader (ABI mismatch):\n{v}")
  ok("klipper_mcu.elf passes the device loader --verify (ABI matches)")

  okBanner("DEPLOY (staging) OK — host artifacts validated on the real device")
  return 0
