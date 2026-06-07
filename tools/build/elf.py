"""ELF32 little-endian inspection and device-ABI checking.

Public interface
────────────────
  inspect_elf(data: bytes) -> ElfInfo
      Parse ELF bytes (must be ELF32 LE).  Raises MalformedElfError on
      structural failures.  For RAW_FIRMWARE callers: do not call inspect_elf;
      pass the raw bytes through directly — check_abi(ArtifactKind.RAW_FIRMWARE)
      short-circuits before any parse.

  check_abi(info: ElfInfo, kind: ArtifactKind) -> AbiResult
      Walk the DEVICE_ABI table + fp_abi check + loader check.
      RAW_FIRMWARE → AbiResult(applicable=False) immediately.

  ElfInfo       — frozen dataclass of parsed ELF fields
  ArtifactKind  — SHARED_LIBRARY / EXECUTABLE / RAW_FIRMWARE
  AbiResult     — frozen dataclass: violations tuple + .ok + .applicable
  AbiViolation  — typed violation for integer field mismatches
  LoaderViolation — typed violation for PT_INTERP string mismatch
  MalformedElfError — raised by inspect_elf on parse failure
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

from abi.abi_spec import (
    ACCEPTED_FP_ABI,
    DEVICE_ABI,
    ELFDATA2LSB,
    EM_MIPS,
    EXPECTED_LOADER,
    ArtifactKind,
)

# Re-export ArtifactKind so callers can do `from build.elf import ArtifactKind`
__all__ = [
    "ElfInfo",
    "ArtifactKind",
    "AbiResult",
    "AbiViolation",
    "LoaderViolation",
    "MalformedElfError",
    "inspect_elf",
    "check_abi",
]

# ──────────────────────────────────────────────────────────────────────────────
# ELF constants (parsing only — spec-level constants live in abi_spec)
# ──────────────────────────────────────────────────────────────────────────────

_ELFMAG       = b"\x7fELF"
_ELFCLASS32   = 1
_PT_INTERP    = 3
_SHT_MIPS_ABIFLAGS = 0x7000002A   # sh_type for .MIPS.abiflags section
_EHDR32_SIZE  = 52
_PHDR32_SIZE  = 32
_SHDR32_SIZE  = 40
_ABIFLAGS_MIN_SIZE = 24           # minimum valid .MIPS.abiflags section


# ──────────────────────────────────────────────────────────────────────────────
# Public types
# ──────────────────────────────────────────────────────────────────────────────

class MalformedElfError(Exception):
    """Raised by inspect_elf when the byte stream is not a valid ELF32 LE."""


@dataclass(frozen=True)
class ElfInfo:
    """Parsed ELF header fields, populated by inspect_elf.

    For RAW_FIRMWARE artifacts (ARM .bin blobs — not ELF), use
    ``ElfInfo.raw_sentinel()`` instead of inspect_elf.  check_abi short-circuits
    for RAW_FIRMWARE and never reads the ElfInfo fields.
    """
    machine:   int             # e_machine (raw value, e.g. 8 = EM_MIPS)
    data:      int             # EI_DATA   (1 = LE, 2 = BE)
    flags:     int             # e_flags
    etype:     int             # e_type    (2 = ET_EXEC, 3 = ET_DYN, …)
    interp:    Optional[str]   # PT_INTERP string, or None if absent
    fp_abi:    Optional[int]   # .MIPS.abiflags fp_abi byte, or None if absent
    cpr1_size: Optional[int]   # .MIPS.abiflags cpr1_size byte, or None if absent

    @classmethod
    def raw_sentinel(cls) -> "ElfInfo":
        """Return a placeholder ElfInfo for RAW_FIRMWARE artifacts.

        check_abi(kind=RAW_FIRMWARE) ignores the ElfInfo entirely; this
        sentinel avoids the need for Optional[ElfInfo] in the call site.
        """
        return cls(machine=-1, data=-1, flags=0, etype=-1,
                   interp=None, fp_abi=None, cpr1_size=None)


@dataclass(frozen=True)
class AbiViolation:
    """A mismatch in an integer-valued ABI field (e_flags bit, machine, endianness, fp_abi)."""
    field:    str   # human-readable field name from DEVICE_ABI (or "machine"/"endianness"/"fp_abi")
    expected: int
    actual:   int


@dataclass(frozen=True)
class LoaderViolation:
    """The PT_INTERP interpreter path did not end with the expected suffix."""
    expected_suffix: str
    actual: str       # full actual interp string (or "" if absent)


@dataclass(frozen=True)
class AbiResult:
    """Result of check_abi.

    Attributes
    ----------
    violations : tuple[AbiViolation | LoaderViolation, ...]
        All violations found, in check order.  Empty iff .ok is True.
    applicable : bool
        False only for RAW_FIRMWARE (ELF ABI checks don't apply).
    ok : bool
        True iff applicable and violations is empty.
    """
    violations: tuple
    applicable: bool

    @property
    def ok(self) -> bool:
        return self.applicable and len(self.violations) == 0

    @classmethod
    def not_applicable(cls) -> "AbiResult":
        return cls(violations=(), applicable=False)


# ──────────────────────────────────────────────────────────────────────────────
# Parsing helpers (pure — no I/O)
# ──────────────────────────────────────────────────────────────────────────────

def _safe_unpack(fmt: str, data: bytes, offset: int) -> tuple:
    """struct.unpack_from with a MalformedElfError on out-of-bounds reads."""
    size = struct.calcsize(fmt)
    if offset < 0 or offset + size > len(data):
        raise MalformedElfError(
            f"ELF too short: need {offset + size} bytes, have {len(data)}"
        )
    return struct.unpack_from(fmt, data, offset)


def _read_cstring(data: bytes, offset: int) -> str:
    """Read a null-terminated string from *data* at *offset*.  MalformedElfError if no NUL found."""
    end = data.find(b"\x00", offset)
    if end == -1:
        raise MalformedElfError(f"Unterminated C string at offset {offset}")
    return data[offset:end].decode("ascii", errors="replace")


# ──────────────────────────────────────────────────────────────────────────────
# inspect_elf
# ──────────────────────────────────────────────────────────────────────────────

def inspect_elf(data: bytes) -> ElfInfo:
    """Parse *data* as an ELF32 little-endian file and return an ElfInfo.

    Raises MalformedElfError if:
    - data is too short to hold a valid ELF32 header
    - the magic bytes are wrong
    - EI_CLASS != ELFCLASS32
    - any referenced section/program header is out of bounds
    - a section/program header area is truncated

    Does NOT raise if EI_DATA != ELFDATA2LSB or e_machine != EM_MIPS — those
    are ABI violations, not parse failures; inspect_elf records them faithfully
    and check_abi reports them.
    """
    # ── 1. ELF ident ─────────────────────────────────────────────────────────
    if len(data) < _EHDR32_SIZE:
        raise MalformedElfError(
            f"Too short for ELF32 header: {len(data)} bytes (need {_EHDR32_SIZE})"
        )
    if data[:4] != _ELFMAG:
        raise MalformedElfError(f"Bad ELF magic: {data[:4]!r}")

    ei_class = data[4]
    ei_data  = data[5]

    if ei_class != _ELFCLASS32:
        raise MalformedElfError(f"Only ELF32 supported (EI_CLASS={ei_class})")

    # ── 2. ELF32 header ───────────────────────────────────────────────────────
    # <HHIIIIIHHHHHH> = e_type, e_machine, e_version, e_entry, e_phoff,
    #                   e_shoff, e_flags, e_ehsize, e_phentsize, e_phnum,
    #                   e_shentsize, e_shnum, e_shstrndx
    (e_type, e_machine, _e_version, _e_entry,
     e_phoff, e_shoff, e_flags,
     _e_ehsize, e_phentsize, e_phnum,
     e_shentsize, e_shnum, e_shstrndx
    ) = _safe_unpack("<HHIIIIIHHHHHH", data, 16)

    # ── 3. Program headers → PT_INTERP ───────────────────────────────────────
    interp: Optional[str] = None

    for i in range(e_phnum):
        phdr_offset = e_phoff + i * _PHDR32_SIZE
        (p_type, p_offset, _p_vaddr, _p_paddr,
         p_filesz, _p_memsz, _p_flags, _p_align
        ) = _safe_unpack("<IIIIIIII", data, phdr_offset)

        if p_type == _PT_INTERP:
            if p_offset + p_filesz > len(data):
                raise MalformedElfError(
                    f"PT_INTERP extends beyond file (offset={p_offset}, size={p_filesz})"
                )
            # interp string is null-terminated; strip the NUL
            raw = data[p_offset : p_offset + p_filesz]
            interp = raw.rstrip(b"\x00").decode("ascii", errors="replace")
            break

    # ── 4. Section headers → .MIPS.abiflags ──────────────────────────────────
    fp_abi:    Optional[int] = None
    cpr1_size: Optional[int] = None

    for i in range(e_shnum):
        shdr_offset = e_shoff + i * _SHDR32_SIZE
        (_sh_name, sh_type, _sh_flags, _sh_addr,
         sh_offset, sh_size,
         _sh_link, _sh_info, _sh_addralign, _sh_entsize
        ) = _safe_unpack("<IIIIIIIIII", data, shdr_offset)

        if sh_type == _SHT_MIPS_ABIFLAGS:
            if sh_size < _ABIFLAGS_MIN_SIZE:
                raise MalformedElfError(
                    f".MIPS.abiflags section too small: {sh_size} bytes (need {_ABIFLAGS_MIN_SIZE})"
                )
            if sh_offset + sh_size > len(data):
                raise MalformedElfError(
                    f".MIPS.abiflags section extends beyond file"
                )
            sec = data[sh_offset : sh_offset + sh_size]
            # struct layout (24 bytes):
            #  [0:2]  version    (uint16)
            #  [2]    isa_level  (uint8)
            #  [3]    isa_rev    (uint8)
            #  [4]    gpr_size   (uint8)
            #  [5]    cpr1_size  (uint8)
            #  [6]    cpr2_size  (uint8)
            #  [7]    fp_abi     (uint8)
            #  [8:12] isa_ext    (uint32)
            #  …
            cpr1_size = sec[5]
            fp_abi    = sec[7]
            break

    return ElfInfo(
        machine   = e_machine,
        data      = ei_data,
        flags     = e_flags,
        etype     = e_type,
        interp    = interp,
        fp_abi    = fp_abi,
        cpr1_size = cpr1_size,
    )


# ──────────────────────────────────────────────────────────────────────────────
# check_abi
# ──────────────────────────────────────────────────────────────────────────────

def check_abi(info: ElfInfo, kind: ArtifactKind) -> AbiResult:
    """Walk the DEVICE_ABI table and all supplementary checks for *kind*.

    For RAW_FIRMWARE, returns AbiResult(applicable=False) immediately without
    consulting *info* at all (so callers may pass a partially-parsed or dummy
    ElfInfo for firmware blobs).

    Violations are collected exhaustively (all checks run), so the caller sees
    the full picture rather than stopping at the first failure.
    """
    if kind is ArtifactKind.RAW_FIRMWARE:
        return AbiResult.not_applicable()

    violations: list = []

    # ── 1. Machine ───────────────────────────────────────────────────────────
    if info.machine != EM_MIPS:
        violations.append(AbiViolation(field="machine", expected=EM_MIPS, actual=info.machine))

    # ── 2. Endianness ────────────────────────────────────────────────────────
    if info.data != ELFDATA2LSB:
        violations.append(AbiViolation(field="endianness", expected=ELFDATA2LSB, actual=info.data))

    # ── 3. e_flags table (nan2008 / o32 / mips32r2) ──────────────────────────
    for field_name, mask, expected_masked in DEVICE_ABI:
        actual_masked = info.flags & mask
        if actual_masked != expected_masked:
            violations.append(
                AbiViolation(field=field_name, expected=expected_masked, actual=actual_masked)
            )

    # ── 4. fp_abi (from .MIPS.abiflags, NOT e_flags) ─────────────────────────
    accepted = ACCEPTED_FP_ABI.get(kind)
    if accepted is not None:
        if info.fp_abi is None or info.fp_abi not in accepted:
            actual_fp = info.fp_abi if info.fp_abi is not None else -1
            # expected: report the canonical accepted value for display purposes
            # (the set may have multiple; we report the minimum as the "primary")
            expected_fp = min(accepted)
            violations.append(
                AbiViolation(field="fp_abi", expected=expected_fp, actual=actual_fp)
            )

    # ── 5. Loader / PT_INTERP ─────────────────────────────────────────────────
    if kind is ArtifactKind.EXECUTABLE:
        actual_interp = info.interp or ""
        if not actual_interp.endswith(EXPECTED_LOADER):
            violations.append(
                LoaderViolation(expected_suffix=EXPECTED_LOADER, actual=actual_interp)
            )
    # SHARED_LIBRARY: no PT_INTERP expected — absence is correct, presence is ignored

    return AbiResult(violations=tuple(violations), applicable=True)
