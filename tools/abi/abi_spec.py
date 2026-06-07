"""Device-ABI specification for the Ender-3 V3 KE (Nebula Pad / XBurst2 MIPS).

This module is the single source of truth for every ABI constraint the build
pipeline enforces.  Both the Python checker (``build/elf.py``) and the Nim
checker (``v3ke/elf.nim``) import / mirror these constants; the shared fixture
set in ``abi/fixtures/`` ensures they can't silently drift (RFC §3 G2).

Table-driven design: adding a new flag constraint is a one-row edit to
``DEVICE_ABI``.
"""

from __future__ import annotations

from enum import Enum, auto


# ──────────────────────────────────────────────────────────────────────────────
# ELF header constants
# ──────────────────────────────────────────────────────────────────────────────

# e_machine
EM_MIPS: int = 8

# EI_DATA (endianness byte in ELF ident)
ELFDATA2LSB: int = 1   # little-endian

# EF_MIPS_* bit-field constants — verified against real device e_flags 0x70001407:
#   arch bits (31:28) = 0x70000000 → MIPS32r2  ✓
#   abi  bits (15:12) = 0x00001000 → O32       ✓
#   bit  10           = 0x00000400 → nan2008   ✓
EF_MIPS_NAN2008:   int = 0x00000400
EF_MIPS_ABI_MASK:  int = 0x0000F000
EF_MIPS_ABI_O32:   int = 0x00001000   # expected value after masking
EF_MIPS_ARCH_MASK: int = 0xF0000000
EF_MIPS_ARCH_32R2: int = 0x70000000   # expected value after masking

# ──────────────────────────────────────────────────────────────────────────────
# ArtifactKind
# ──────────────────────────────────────────────────────────────────────────────

class ArtifactKind(Enum):
    """Classification of a build artifact for ABI checking purposes."""
    SHARED_LIBRARY = auto()  # ET_DYN, no PT_INTERP, MIPS ABI checks apply
    EXECUTABLE     = auto()  # ET_EXEC, PT_INTERP required, MIPS ABI checks apply
    RAW_FIRMWARE   = auto()  # ARM .bin — not an ELF; ABI checks not applicable


# ──────────────────────────────────────────────────────────────────────────────
# Table-driven ABI spec
# ──────────────────────────────────────────────────────────────────────────────

# Each row: (field_name, e_flags_mask, expected_value_after_masking)
# The checker evaluates: (e_flags & mask) == expected for each row.
DEVICE_ABI: list[tuple[str, int, int]] = [
    ("nan2008",   EF_MIPS_NAN2008,   EF_MIPS_NAN2008),   # bit must be set → mask==expected
    ("o32",       EF_MIPS_ABI_MASK,  EF_MIPS_ABI_O32),
    ("mips32r2",  EF_MIPS_ARCH_MASK, EF_MIPS_ARCH_32R2),
]

# Expected loader (PT_INTERP string, excluding the null terminator)
EXPECTED_LOADER: str = "ld-linux-mipsn8.so.1"

# ──────────────────────────────────────────────────────────────────────────────
# FP ABI: accepted fp_abi values per artifact kind
# ──────────────────────────────────────────────────────────────────────────────

# fp_abi is the byte at offset 7 of the 24-byte .MIPS.abiflags section struct.
# Known values: 1=DOUBLE(legacy), 5=FPXX, 6=FP64, 7=FP64A.
#
# Resolved by the A-spike (O6) — uniform FP64, and it requires no build change:
#   * The crosstool-ng cross-gcc DEFAULTS to -mfp64 (-mfpxx disabled), so a plain
#     compile with no FP flags already emits FP64.  Verified 2026-06-06: a fresh
#     clean build of klipper_mcu.elf (Klipper adds no -mfp* flags) came out FP64
#     (fp_abi=6), identical to out/klipper.elf.
#   * The device's own userspace is FP64: /lib/libc.so.6 and ld-2.29.so are both
#     fp_abi=6.  An FP64 klipper.elf was deployed to the printer and exec'd/ran
#     with no SIGILL (the kernel runs FP registers in FR=1 mode).
#   * The lone FPXX(5) artifact we saw (a copied klipper_mcu.elf) was STALE — left
#     over from the old pre-crosstool-ng toolchain that defaulted to FPXX.  O6's
#     premise ("the build emits FPXX") was that stale binary, not current output.
# So every artifact this pipeline builds is FP64.  We accept ONLY {FP64=6}: an
# FPXX (or legacy DOUBLE) binary now means a stale/wrong-toolchain build, which is
# exactly the regression the ABI checker exists to catch — accepting it would
# blunt the guard.  Good fixtures are fp_abi=6 (accepted); the bad-fp_abi fixture
# is fp_abi=1 (rejected); fp_abi=5 (FPXX) is likewise rejected.
ACCEPTED_FP_ABI: dict[ArtifactKind, frozenset[int]] = {
    ArtifactKind.SHARED_LIBRARY: frozenset({6}),   # FP64
    ArtifactKind.EXECUTABLE:     frozenset({6}),   # FP64
    # RAW_FIRMWARE: not applicable; no entry intentionally (KeyError is a bug)
}
