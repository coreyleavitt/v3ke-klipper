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

# ──────────────────────────────────────────────────────────────────────────────
# § H3 — absent .MIPS.abiflags section is rejected as an fp_abi violation
# ──────────────────────────────────────────────────────────────────────────────

suite "absent abiflags":
  test "bad_no_abiflags: fpAbi is none() — section absent":
    check readFixture("bad_no_abiflags.elf").fpAbi == none(uint8)

  test "bad_no_abiflags: check_abi rejects with fp_abi violation":
    let r = checkAbi(readFixture("bad_no_abiflags.elf"), Executable)
    check r.ok == false
    check fieldNames(r).contains("fp_abi")

  test "bad_no_abiflags: fp_abi violation actual = uint32.high (absent sentinel)":
    let r = checkAbi(readFixture("bad_no_abiflags.elf"), Executable)
    let v = findViolation(r, "fp_abi")
    check v.intActual == uint32.high

# ──────────────────────────────────────────────────────────────────────────────
# § M4 — truncated phdr / shdr table raises ElfError (not silent break)
# ──────────────────────────────────────────────────────────────────────────────

suite "truncated header tables":
  test "truncated phdr table raises ElfError":
    ## Keep the 52-byte ehdr + 16 bytes of first phdr (half of 32) — phdr table is truncated.
    let good = readFile(FixtureDir / "good_exec.elf")
    let truncated = good[0 ..< 52 + 16]
    let tmpPath = getTempDir() / "tabi_trunc_phdr.elf"
    writeFile(tmpPath, truncated)
    expect ElfError:
      discard readElf(tmpPath)

  test "truncated shdr table raises ElfError":
    ## Keep everything up to mid-way through the first section header (20 < 40 bytes).
    let good = readFile(FixtureDir / "good_exec.elf")
    # e_shoff is a uint32 at offset 32 in the ELF header (LE)
    let eShoff = uint32(ord(good[32])) or (uint32(ord(good[33])) shl 8) or
                 (uint32(ord(good[34])) shl 16) or (uint32(ord(good[35])) shl 24)
    let truncated = good[0 ..< eShoff.int + 20]
    let tmpPath = getTempDir() / "tabi_trunc_shdr.elf"
    writeFile(tmpPath, truncated)
    expect ElfError:
      discard readElf(tmpPath)

# ──────────────────────────────────────────────────────────────────────────────
# § R2-M3 — undersized ePhentsize / eShentsize raises ElfError (not IndexDefect)
# ──────────────────────────────────────────────────────────────────────────────

proc craftElfWithSmallPhentsize(phentsize: uint16): string =
  ## Build a minimal ELF32 LE with the given ePhentsize and one program-header
  ## entry placed at ePhoff=52.  The entry occupies exactly `phentsize` bytes,
  ## starting with p_type=PT_INTERP (3) so the parser tries to read p_offset
  ## at +4 and p_filesz at +16.  The file is sized to just cover the entry
  ## (52 + phentsize bytes), so the existing `p + ePhentsize <= b.len` guard
  ## PASSES — but reads at p+16 land out-of-bounds if phentsize < 20.
  var buf = newString(52 + phentsize.int)
  buf[0] = '\x7f'; buf[1] = 'E'; buf[2] = 'L'; buf[3] = 'F'
  buf[4] = '\x01'  # EI_CLASS = ELFCLASS32
  buf[5] = '\x01'  # EI_DATA  = ELFDATA2LSB
  # e_type = ET_EXEC (2) at offset 16
  buf[16] = '\x02'
  # e_machine = EM_MIPS (8) at offset 18
  buf[18] = '\x08'
  # e_phoff = 52 at offset 28 (LE uint32)
  buf[28] = '\x34'  # 52 = 0x34
  # e_phentsize at offset 42
  buf[42] = chr(phentsize.int and 0xff)
  buf[43] = chr((phentsize.int shr 8) and 0xff)
  # e_phnum = 1 at offset 44
  buf[44] = '\x01'
  # e_shnum = 0 at offset 48 (already 0 from newString)
  # Program header entry at offset 52: p_type = PT_INTERP = 3 (LE uint32)
  buf[52] = '\x03'
  result = buf

proc craftElfWithSmallShentsize(shentsize: uint16): string =
  ## Build a minimal ELF32 LE with the given eShentsize and one section-header
  ## entry placed at eShoff=52, e_phnum=0 (skip phdr loop).  The entry occupies
  ## exactly `shentsize` bytes.  When shentsize >= 8, sh_type=SHT_MIPS_ABIFLAGS
  ## is set so the parser tries to read sh_offset at +16 and sh_size at +20.
  ## The file is sized to just cover the entry so `s + eShentsize <= b.len` PASSES
  ## — but reads at s+16/s+20 land out-of-bounds if shentsize < 24 (when it's <21).
  ## For very small shentsize (< 8), sh_type bytes are not written — the guard
  ## must fire on the entsize floor check alone.
  var buf = newString(52 + shentsize.int)
  buf[0] = '\x7f'; buf[1] = 'E'; buf[2] = 'L'; buf[3] = 'F'
  buf[4] = '\x01'  # EI_CLASS = ELFCLASS32
  buf[5] = '\x01'  # EI_DATA  = ELFDATA2LSB
  buf[16] = '\x02'  # e_type = ET_EXEC
  buf[18] = '\x08'  # e_machine = EM_MIPS
  # e_shoff = 52 at offset 32
  buf[32] = '\x34'
  # e_phnum = 0 at offset 44 (already 0)
  # e_shentsize at offset 46
  buf[46] = chr(shentsize.int and 0xff)
  buf[47] = chr((shentsize.int shr 8) and 0xff)
  # e_shnum = 1 at offset 48
  buf[48] = '\x01'
  # Section header at offset 52: sh_type = SHT_MIPS_ABIFLAGS = 0x7000002A at +4
  # Only write if the buffer is large enough to hold up to offset 52+7.
  if shentsize.int >= 8:
    buf[52+4] = '\x2A'
    buf[52+5] = '\x00'
    buf[52+6] = '\x00'
    buf[52+7] = '\x70'
  result = buf

suite "R2-M3: undersized entsize raises ElfError not IndexDefect":
  test "ePhentsize=4 (< 20) raises ElfError, not IndexDefect":
    ## A crafted ELF with ePhentsize=4 passes the existing bounds guard
    ## (p + 4 <= b.len) but then u32(b, p+16) would go out-of-bounds,
    ## raising IndexDefect instead of ElfError.  After the fix, an explicit
    ## elfCheck floor on ePhentsize raises ElfError before any read.
    let tmpPath = getTempDir() / "tabi_small_phentsize.elf"
    writeFile(tmpPath, craftElfWithSmallPhentsize(4'u16))
    expect ElfError:
      discard readElf(tmpPath)

  test "ePhentsize=19 (< 20) raises ElfError":
    ## Off-by-one: 19 bytes is one short of the minimum needed to read p_filesz.
    let tmpPath = getTempDir() / "tabi_phentsize19.elf"
    writeFile(tmpPath, craftElfWithSmallPhentsize(19'u16))
    expect ElfError:
      discard readElf(tmpPath)

  test "ePhentsize=20 (minimum valid for field reads) does not raise ElfError on parse":
    ## 20 bytes is exactly enough to reach p_filesz (+16..+19). The parser should
    ## not raise on the entsize check. (The entry itself may still produce
    ## violations when ABI-checked, but readElf must not throw ElfError here.)
    ## NOTE: with 0-filled PT_INTERP fields (off=0, size=0), readElf won't raise.
    let tmpPath = getTempDir() / "tabi_phentsize20.elf"
    writeFile(tmpPath, craftElfWithSmallPhentsize(20'u16))
    # Should not raise ElfError — may return a mostly-zero ElfInfo
    var raised = false
    try:
      discard readElf(tmpPath)
    except ElfError:
      raised = true
    check raised == false

  test "eShentsize=4 (< 24) raises ElfError, not IndexDefect":
    ## A crafted ELF with eShentsize=4 passes the existing `s + 4 <= b.len` guard
    ## then u32(b, s+4) reads sh_type, but u32(b, s+16) for sh_offset is OOB.
    ## After the fix an elfCheck floor raises ElfError.
    let tmpPath = getTempDir() / "tabi_small_shentsize.elf"
    writeFile(tmpPath, craftElfWithSmallShentsize(4'u16))
    expect ElfError:
      discard readElf(tmpPath)

  test "eShentsize=23 (< 24) raises ElfError":
    ## Off-by-one: 23 bytes is one short of reaching sh_size at +20..+23.
    let tmpPath = getTempDir() / "tabi_shentsize23.elf"
    writeFile(tmpPath, craftElfWithSmallShentsize(23'u16))
    expect ElfError:
      discard readElf(tmpPath)

  test "good fixtures still parse without ElfError (ePhentsize=32, eShentsize=40)":
    ## Confirm the floor checks don't break legitimate ELFs.
    var raised = false
    try:
      discard readElf(FixtureDir / "good_exec.elf")
      discard readElf(FixtureDir / "good_dyn.elf")
    except ElfError:
      raised = true
    check raised == false
