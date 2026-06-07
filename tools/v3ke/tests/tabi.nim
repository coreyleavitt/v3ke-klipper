## Cross-language golden test (slice A1c) — RFC §3 G2
##
## The Nim ELF reader + ABI checker is run against the SAME fixtures as the Python A1 suite
## (tools/abi/fixtures/).  Every accept/reject verdict must match.  Rejections must identify
## the correct violated field, not just "failed" — so we can't have the two implementations
## silently diverge.
##
## Run (from tools/v3ke/ inside the v3ke-dev container):
##   nim c --hints:off -r tests/tabi.nim

import std/[os, options, strutils, unittest]
import elf

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

const FixtureDir = "../abi/fixtures"

proc readFixture(name: string): ElfInfo =
  readElf(FixtureDir / name)

proc fieldNames(r: AbiResult): seq[string] =
  for v in r.violations:
    result.add(v.fieldName)

proc findViolation(r: AbiResult, field: string): Violation =
  for v in r.violations:
    if v.fieldName == field:
      return v
  raiseAssert("no violation with field=" & field & " found in " & $r.violations.len & " violations")

# ──────────────────────────────────────────────────────────────────────────────
# § Field extraction — good_exec.elf / good_dyn.elf
# ──────────────────────────────────────────────────────────────────────────────

suite "ELF field extraction":
  test "good_exec: machine = EM_MIPS (8)":
    check readFixture("good_exec.elf").machine == 8'u16

  test "good_exec: EI_DATA = ELFDATA2LSB (1)":
    check readFixture("good_exec.elf").data == 1'u8

  test "good_exec: e_flags = 0x70001407":
    check readFixture("good_exec.elf").flags == 0x70001407'u32

  test "good_exec: etype = ET_EXEC (2)":
    check readFixture("good_exec.elf").etype == 2'u16

  test "good_exec: interp = /lib/ld-linux-mipsn8.so.1":
    check readFixture("good_exec.elf").interp == "/lib/ld-linux-mipsn8.so.1"

  test "good_exec: fp_abi = some(6) — FP64, from .MIPS.abiflags section":
    check readFixture("good_exec.elf").fpAbi == some(6'u8)

  test "good_dyn: interp = '' (ET_DYN has no PT_INTERP)":
    check readFixture("good_dyn.elf").interp == ""

  test "good_dyn: etype = ET_DYN (3)":
    check readFixture("good_dyn.elf").etype == 3'u16

  test "good_dyn: fp_abi = some(6)":
    check readFixture("good_dyn.elf").fpAbi == some(6'u8)

  test "bad_fp_abi: fp_abi = some(1) — read from abiflags, not e_flags":
    # Ensures the Nim reader actually reads the abiflags section (not an e_flags bit)
    check readFixture("bad_fp_abi.elf").fpAbi == some(1'u8)

# ──────────────────────────────────────────────────────────────────────────────
# § check_abi — accepts known-good fixtures
# ──────────────────────────────────────────────────────────────────────────────

suite "check_abi accepts":
  test "good_exec accepted as Executable":
    let r = checkAbi(readFixture("good_exec.elf"), Executable)
    check r.ok == true
    check r.violations.len == 0

  test "good_dyn accepted as SharedLibrary":
    let r = checkAbi(readFixture("good_dyn.elf"), SharedLibrary)
    check r.ok == true
    check r.violations.len == 0

# ──────────────────────────────────────────────────────────────────────────────
# § check_abi — rejects each bad fixture for the correct field
# ──────────────────────────────────────────────────────────────────────────────

suite "check_abi rejects":
  test "bad_machine → not ok, first violation field = machine":
    let r = checkAbi(readFixture("bad_machine.elf"), Executable)
    check r.ok == false
    check r.violations.len >= 1
    check r.violations[0].fieldName == "machine"

  test "bad_endian → not ok, first violation field = endianness":
    let r = checkAbi(readFixture("bad_endian.elf"), Executable)
    check r.ok == false
    check r.violations.len >= 1
    check r.violations[0].fieldName == "endianness"

  test "bad_nan2008 → field=nan2008, expected=0x400, actual=0":
    let r = checkAbi(readFixture("bad_nan2008.elf"), Executable)
    check r.ok == false
    let v = r.violations[0]
    check v.fieldName == "nan2008"
    check v.intExpected == 0x400'u32
    check v.intActual   == 0'u32

  test "bad_o32 → field=o32, expected=0x1000, actual=0":
    let r = checkAbi(readFixture("bad_o32.elf"), Executable)
    check r.ok == false
    let v = r.violations[0]
    check v.fieldName == "o32"
    check v.intExpected == 0x1000'u32
    check v.intActual   == 0'u32

  test "bad_mips32r2 → field=mips32r2, expected=0x70000000, actual=0":
    let r = checkAbi(readFixture("bad_mips32r2.elf"), Executable)
    check r.ok == false
    let v = r.violations[0]
    check v.fieldName == "mips32r2"
    check v.intExpected == 0x70000000'u32
    check v.intActual   == 0'u32

  test "bad_fp_abi → fp_abi violation, actual=1 (FP_ABI_DOUBLE), from abiflags not e_flags":
    let r = checkAbi(readFixture("bad_fp_abi.elf"), Executable)
    check r.ok == false
    let v = findViolation(r, "fp_abi")
    check v.intActual == 1'u32   # FP_ABI_DOUBLE — the abiflags byte, not an e_flags bit

  test "bad_loader → loader violation with expected suffix and actual path":
    let r = checkAbi(readFixture("bad_loader.elf"), Executable)
    check r.ok == false
    let v = findViolation(r, "loader")
    check v.loaderExpected.contains("ld-linux-mipsn8.so.1")
    check v.loaderActual.contains("ld-linux.so.3")

  test "bad_machine exhaustive: machine violation present in full violation set":
    let r = checkAbi(readFixture("bad_machine.elf"), Executable)
    check r.ok == false
    check fieldNames(r).contains("machine")
