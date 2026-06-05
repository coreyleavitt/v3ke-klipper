## `v3ke verify` — assert the MIPS host artifacts match the device ABI before flashing/deploying.
## Checks: mipsel · nan2008 · o32 · mips32r2, and the host-MCU executable's loader.
import std/[os, strformat, strutils]
import common

const
  EM_MIPS      = 8'u16
  EF_NAN2008   = 0x0000_0400'u32   ## EF_MIPS_NAN2008
  EF_ABI_MASK  = 0x0000_F000'u32
  EF_ABI_O32   = 0x0000_1000'u32   ## EF_MIPS_ABI_O32
  EF_ARCH_MASK = 0xF000_0000'u32
  EF_ARCH_32R2 = 0x7000_0000'u32   ## EF_MIPS_ARCH_32R2
  DeviceLoader = "ld-linux-mipsn8.so.1"

proc checkMips(path: string, wantInterp: bool): bool =
  echo path
  if not fileExists(path):
    warn(&"missing: {path}"); return false
  let e = readElf(path)
  result = true
  if e.machine != EM_MIPS:                       warn(&"not MIPS (machine={e.machine})"); result = false
  if (e.flags and EF_NAN2008) == 0'u32:          warn("not nan2008"); result = false
  if (e.flags and EF_ABI_MASK) != EF_ABI_O32:    warn("not o32"); result = false
  if (e.flags and EF_ARCH_MASK) != EF_ARCH_32R2: warn("not mips32r2"); result = false
  let interpStr = if e.interp.len > 0: e.interp else: "(none)"
  note(&"flags=0x{e.flags:08x}  type={e.etype}  interp={interpStr}")
  if wantInterp and not e.interp.endsWith(DeviceLoader):   # interp is a full path, e.g. /lib/...
    warn(&"loader is '{interpStr}', expected .../{DeviceLoader}"); result = false
  if result:
    ok("matches device ABI (mipsel / nan2008 / o32 / mips32r2" &
       (if wantInterp: " / " & DeviceLoader else: "") & ")")

proc verifyCmd*(args: seq[string]): int =
  var chelper = "klipper/c_helper/c_helper.so"
  var hostmcu = "klipper/klipper_host_mcu/klipper_mcu.elf"
  if args.len == 2:
    chelper = args[0]; hostmcu = args[1]
  elif args.len != 0:
    echo "usage: v3ke verify [<c_helper.so> <klipper_mcu.elf>]"; return 1

  echo "=== ABI verification (mipsel / nan2008 / o32, loader ld-linux-mipsn8.so.1) ==="
  var good = true
  if not checkMips(chelper, wantInterp = false): good = false   # shared lib: no interpreter
  if not checkMips(hostmcu, wantInterp = true):  good = false   # executable: must load mipsn8
  if good:
    okBanner("ABI VERIFICATION PASSED")
    return 0
  errln("ABI VERIFICATION FAILED")
  return 1
