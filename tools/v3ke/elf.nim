## ELF32 little-endian reader and device-ABI checker — pure, no output side-effects.
##
## This module is the Nim counterpart to tools/build/elf.py.  Both are tested against
## the same golden fixtures (tools/abi/fixtures/) per RFC §3 G2, so the two implementations
## cannot silently drift.  The ABI constants mirror tools/abi/abi_spec.py exactly.
##
## Public interface
## ────────────────
##   readElf(path)              → ElfInfo   (raises ElfError on malformed input)
##   checkAbi(info, kind)       → AbiResult (exhaustive — all violations collected)
##   ElfInfo                    — parsed ELF32 header fields incl. fp_abi from .MIPS.abiflags
##   ArtifactKind               — Executable / SharedLibrary / RawFirmware
##   AbiResult                  — .ok, .applicable, .violations
##   Violation                  — case object: IntViolation | LoaderViolation
##   ElfError                   — raised by readElf on parse failure
##
## Operator-machine constraint: this code intentionally has no dependency on readelf,
## objdump, or any external tool — it runs on a bare Windows/Linux/macOS machine with
## only the v3ke binary in hand.

import std/[options, os, strformat, strutils]

# ──────────────────────────────────────────────────────────────────────────────
# Public exception
# ──────────────────────────────────────────────────────────────────────────────

type ElfError* = object of CatchableError
  ## Raised by readElf when the byte stream is not a valid ELF32 LE.

# ──────────────────────────────────────────────────────────────────────────────
# ArtifactKind — mirrors abi_spec.py ArtifactKind
# ──────────────────────────────────────────────────────────────────────────────

type ArtifactKind* = enum
  Executable    ## ET_EXEC: PT_INTERP required; MIPS ABI checks apply
  SharedLibrary ## ET_DYN: no PT_INTERP; MIPS ABI checks apply
  RawFirmware   ## ARM .bin blob — not ELF; ABI checks not applicable

# ──────────────────────────────────────────────────────────────────────────────
# ElfInfo — parsed ELF32 header fields
# ──────────────────────────────────────────────────────────────────────────────

type ElfInfo* = object
  etype*:   uint16          ## e_type  (2=ET_EXEC, 3=ET_DYN, …)
  machine*: uint16          ## e_machine (8=EM_MIPS)
  data*:    uint8           ## EI_DATA  (1=LE, 2=BE)
  flags*:   uint32          ## e_flags  (MIPS ABI/arch/nan bits)
  interp*:  string          ## PT_INTERP string; "" when absent
  fpAbi*:   Option[uint8]   ## .MIPS.abiflags fp_abi byte; none() when section absent

# ──────────────────────────────────────────────────────────────────────────────
# Violation — typed violation result
# ──────────────────────────────────────────────────────────────────────────────

type
  ViolationKind* = enum IntViolation, LoaderViolation

  Violation* = object
    fieldName*: string             ## human-readable field ("machine", "fp_abi", "loader", …)
    case kind*: ViolationKind
    of IntViolation:
      intExpected*: uint32
      intActual*:   uint32
    of LoaderViolation:
      loaderExpected*: string      ## expected suffix (e.g. "ld-linux-mipsn8.so.1")
      loaderActual*:   string      ## full actual PT_INTERP string (or "" if absent)

proc intViolation*(field: string; expected, actual: uint32): Violation =
  Violation(kind: IntViolation, fieldName: field, intExpected: expected, intActual: actual)

proc loaderViolation*(expected, actual: string): Violation =
  Violation(kind: LoaderViolation, fieldName: "loader",
            loaderExpected: expected, loaderActual: actual)

# ──────────────────────────────────────────────────────────────────────────────
# AbiResult
# ──────────────────────────────────────────────────────────────────────────────

type AbiResult* = object
  violations*:  seq[Violation]
  applicable*:  bool   ## false only for RawFirmware

proc ok*(r: AbiResult): bool =
  r.applicable and r.violations.len == 0

# ──────────────────────────────────────────────────────────────────────────────
# ABI constants — mirror abi_spec.py exactly
# ──────────────────────────────────────────────────────────────────────────────

const
  EmMips*          = 8'u16
  ElfData2Lsb*     = 1'u8

  EfNan2008*       = 0x0000_0400'u32   ## EF_MIPS_NAN2008
  EfAbiMask*       = 0x0000_F000'u32   ## EF_MIPS_ABI_MASK
  EfAbiO32*        = 0x0000_1000'u32   ## EF_MIPS_ABI_O32  (expected after masking)
  EfArchMask*      = 0xF000_0000'u32   ## EF_MIPS_ARCH_MASK
  EfArch32r2*      = 0x7000_0000'u32   ## EF_MIPS_ARCH_32R2 (expected after masking)

  ExpectedLoader*  = "ld-linux-mipsn8.so.1"

  # Resolved by the A-spike (O6) — uniform FP64, no build change: the cross-gcc
  # defaults to -mfp64, the device userspace is FP64, and a fresh klipper_mcu.elf
  # builds FP64 (the lone FPXX binary was a stale old-toolchain artifact).  Accept
  # ONLY {FP64=6}; FPXX(5)/DOUBLE(1)/absent are rejected as stale/wrong-toolchain.
  # Mirrors abi_spec.ACCEPTED_FP_ABI exactly (G2); see abi_spec.py for rationale.
  AcceptedFpAbi*: set[uint8] = {6'u8}   ## accepted for both Executable and SharedLibrary

# ──────────────────────────────────────────────────────────────────────────────
# Parsing helpers
# ──────────────────────────────────────────────────────────────────────────────

const MaxElfBytes = 64 * 1024 * 1024   ## guard against slurping huge/untrusted files

proc u8(b: string; o: int): uint8  = cast[uint8](b[o])
proc u16(b: string; o: int): uint16 = uint16(ord(b[o])) or (uint16(ord(b[o+1])) shl 8)
proc u32(b: string; o: int): uint32 =
  uint32(ord(b[o])) or (uint32(ord(b[o+1])) shl 8) or
  (uint32(ord(b[o+2])) shl 16) or (uint32(ord(b[o+3])) shl 24)

template elfCheck(cond: bool; msg: string) =
  if not cond: raise newException(ElfError, msg)

# ──────────────────────────────────────────────────────────────────────────────
# readElf — parse an ELF32 LE file
# ──────────────────────────────────────────────────────────────────────────────

proc readElf*(path: string): ElfInfo =
  ## Parse *path* as an ELF32 little-endian file and return an ElfInfo.
  ##
  ## Raises ElfError if:
  ## - the file is too large (> 64 MB)
  ## - the file is too short for a valid ELF32 header (< 52 bytes)
  ## - the ELF magic bytes are wrong
  ## - EI_CLASS != ELFCLASS32 (1)
  ## - any referenced section/program header lies outside the file
  ##
  ## Does NOT raise if EI_DATA != 1 (LE) or e_machine != EM_MIPS — these are
  ## ABI violations that check_abi reports; readElf records them faithfully.
  elfCheck getFileSize(path) <= MaxElfBytes,
    &"{path}: file too large (> {MaxElfBytes div (1024*1024)} MB)"
  let b = readFile(path)
  elfCheck b.len >= 52, &"{path}: too short for ELF32 header ({b.len} bytes, need 52)"
  elfCheck b[0..3] == "\x7fELF", &"{path}: bad ELF magic"
  elfCheck ord(b[4]) == 1, &"{path}: only ELF32 supported (EI_CLASS={ord(b[4])})"

  # ── ELF ident + header ───────────────────────────────────────────────────────
  let eiData  = u8(b, 5)
  let eType   = u16(b, 16)
  let eMachine = u16(b, 18)
  let eFlags  = u32(b, 36)
  let ePhoff  = u32(b, 28).int
  let ePhentsize = u16(b, 42).int
  let ePhnum  = u16(b, 44).int
  let eShoff  = u32(b, 32).int
  let eShentsize = u16(b, 46).int
  let eShnum  = u16(b, 48).int

  # ── Program headers → PT_INTERP ─────────────────────────────────────────────
  var interp = ""
  for i in 0 ..< ePhnum:
    let p = ePhoff + i * ePhentsize
    if p + 20 > b.len: break
    if u32(b, p) == 3'u32:   # PT_INTERP
      let off = u32(b, p + 4).int
      let sz  = u32(b, p + 16).int
      elfCheck off + sz <= b.len,
        &"{path}: PT_INTERP extends beyond file (offset={off}, size={sz})"
      interp = b[off ..< off + sz].strip(chars = {'\0'})
      break

  # ── Section headers → .MIPS.abiflags (sh_type = 0x7000002A) ────────────────
  var fpAbi: Option[uint8] = none(uint8)
  const ShMipsAbiflags = 0x7000002A'u32
  const AbiflagsMinSize = 24
  for i in 0 ..< eShnum:
    let s = eShoff + i * eShentsize
    if s + 40 > b.len: break
    let shType   = u32(b, s + 4)
    let shOffset = u32(b, s + 16).int
    let shSize   = u32(b, s + 20).int
    if shType == ShMipsAbiflags:
      elfCheck shSize >= AbiflagsMinSize,
        &"{path}: .MIPS.abiflags section too small ({shSize} bytes, need {AbiflagsMinSize})"
      elfCheck shOffset + shSize <= b.len,
        &"{path}: .MIPS.abiflags section extends beyond file"
      # Struct layout (24 bytes, all LE):
      #  [0:2]  version   (uint16)
      #  [2]    isa_level (uint8)
      #  [3]    isa_rev   (uint8)
      #  [4]    gpr_size  (uint8)
      #  [5]    cpr1_size (uint8)
      #  [6]    cpr2_size (uint8)
      #  [7]    fp_abi    (uint8)   ← what we want
      #  [8:12] isa_ext   (uint32)
      #  …
      fpAbi = some(u8(b, shOffset + 7))
      break

  ElfInfo(
    etype:   eType,
    machine: eMachine,
    data:    eiData,
    flags:   eFlags,
    interp:  interp,
    fpAbi:   fpAbi,
  )

# ──────────────────────────────────────────────────────────────────────────────
# checkAbi — walk the ABI table; collect all violations
# ──────────────────────────────────────────────────────────────────────────────

proc checkAbi*(info: ElfInfo; kind: ArtifactKind): AbiResult =
  ## Check *info* against the device-ABI spec for *kind*.
  ##
  ## RawFirmware → AbiResult(applicable=false) immediately (no ELF checks apply).
  ## All other violations are collected exhaustively (no early exit) so the caller
  ## sees the complete picture.  Check order matches abi_spec.py / check_abi (Python):
  ##   machine → endianness → nan2008 → o32 → mips32r2 → fp_abi → loader
  if kind == RawFirmware:
    return AbiResult(applicable: false)

  var vs: seq[Violation]

  # 1. Machine
  if info.machine != EmMips:
    vs.add intViolation("machine", EmMips.uint32, info.machine.uint32)

  # 2. Endianness
  if info.data != ElfData2Lsb:
    vs.add intViolation("endianness", ElfData2Lsb.uint32, info.data.uint32)

  # 3–5. e_flags table: nan2008, o32, mips32r2
  let flagChecks = [
    ("nan2008",  EfNan2008,  EfNan2008),
    ("o32",      EfAbiMask,  EfAbiO32),
    ("mips32r2", EfArchMask, EfArch32r2),
  ]
  for (name, mask, expected) in flagChecks:
    let actual = info.flags and mask
    if actual != expected:
      vs.add intViolation(name, expected, actual)

  # 6. fp_abi — from .MIPS.abiflags section (NOT e_flags bits)
  # Accepted = {FPXX=5, FP64=6} (A-spike O6, FR=1 device).  See abi_spec.py.
  let fpActual = if info.fpAbi.isSome: info.fpAbi.get.uint32 else: uint32.high
  if info.fpAbi.isNone or info.fpAbi.get notin AcceptedFpAbi:
    # Report the canonical expected value = min of accepted set (mirrors Python's min(accepted)).
    var fpExpected = uint8.high
    for v in AcceptedFpAbi:
      if v < fpExpected: fpExpected = v
    vs.add intViolation("fp_abi", fpExpected.uint32, fpActual)

  # 7. Loader (PT_INTERP) — only for Executable
  if kind == Executable:
    if not info.interp.endsWith(ExpectedLoader):
      vs.add loaderViolation(ExpectedLoader, info.interp)

  AbiResult(violations: vs, applicable: true)
