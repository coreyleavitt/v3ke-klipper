## `v3ke verify` — assert the MIPS host artifacts match the device ABI before flashing/deploying.
## Checks: mipsel · nan2008 · o32 · mips32r2 · fp_abi (FP64) · loader (ld-linux-mipsn8.so.1).
## Uses elf.nim for parsing and ABI checking — no external readelf dependency.
import std/[options, os, strformat, strutils]
import common
import elf

proc checkMips(path: string, kind: ArtifactKind): bool =
  echo path
  if not fileExists(path):
    warn(&"missing: {path}"); return false
  let info = readElf(path)
  let res  = checkAbi(info, kind)
  result = res.ok

  let interpStr = if info.interp.len > 0: info.interp else: "(none)"
  let fpStr     = if info.fpAbi.isSome: $info.fpAbi.get else: "(absent)"
  note(&"flags=0x{info.flags:08x}  type={info.etype}  interp={interpStr}  fp_abi={fpStr}")

  if not res.ok:
    for v in res.violations:
      case v.kind
      of IntViolation:
        warn(&"{v.fieldName}: expected 0x{v.intExpected:x}, got 0x{v.intActual:x}")
      of LoaderViolation:
        warn(&"loader: expected .../{v.loaderExpected}, got '{v.loaderActual}'")
  else:
    let loaderNote = if kind == Executable: " / " & ExpectedLoader else: ""
    ok(&"matches device ABI (mipsel / nan2008 / o32 / mips32r2 / fp64{loaderNote})")

proc verifyCmd*(args: seq[string]): int =
  var chelper = "klipper/c_helper/c_helper.so"
  var hostmcu = "klipper/klipper_host_mcu/klipper_mcu.elf"
  if args.len == 2:
    chelper = args[0]; hostmcu = args[1]
  elif args.len != 0:
    echo "usage: v3ke verify [<c_helper.so> <klipper_mcu.elf>]"; return 1

  echo "=== ABI verification (mipsel / nan2008 / o32 / mips32r2 / fp64 / loader ld-linux-mipsn8.so.1) ==="
  var good = true
  if not checkMips(chelper, SharedLibrary): good = false   # shared lib: no PT_INTERP check
  if not checkMips(hostmcu, Executable):   good = false   # executable: must load mipsn8
  if good:
    okBanner("ABI VERIFICATION PASSED")
    return 0
  errln("ABI VERIFICATION FAILED")
  return 1
