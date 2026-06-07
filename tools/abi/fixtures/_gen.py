"""Fixture generator for ABI checker tests.

Produces minimal but structurally valid ELF32-LE MIPS files.  Each fixture is
the ground truth for *one* distinguishable case: one known-good and one
per-field known-bad.  Both the Python and the Nim ABI checkers are tested
against these same bytes (RFC §3 G2).

Layout of generated ELFs
─────────────────────────
Offset  Size  Region
0       52    ELF header (Ehdr32)
52      K     Program headers (Phdr32, 32 bytes each)
52+K    24    .MIPS.abiflags section content (always present)
52+K+24  ?    PT_INTERP string  (only for ET_EXEC fixtures)
...     ...   section-header name strings (.shstrtab)
...     ...   Section headers (Shdr32, 40 bytes each)

All offsets are absolute from file start, kept small so the generator is easy
to audit.

Run this script to (re)generate the fixture directory:

    python3 tools/abi/fixtures/_gen.py

All generated files are committed; this script is kept alongside them so the
set is reproducible.
"""

from __future__ import annotations

import struct
import os

# ──────────────────────────────────────────────────────────────────────────────
# ELF constants (ELF32 LE)
# ──────────────────────────────────────────────────────────────────────────────

ELFMAG = b"\x7fELF"
ELFCLASS32 = 1
ELFDATA2LSB = 1          # little-endian
ELFDATA2MSB = 2          # big-endian (used for the bad-endianness fixture)

ET_EXEC = 2
ET_DYN  = 3

EM_MIPS = 8
EM_ARM  = 40             # used for the bad-machine fixture

PT_LOAD   = 1
PT_INTERP = 3

SHT_NULL      = 0
SHT_STRTAB    = 3
SHT_MIPS_ABIFLAGS = 0x7000002A   # sh_type for .MIPS.abiflags

# EF_MIPS_* e_flags bits — verified against real device value 0x70001407
EF_MIPS_NOREORDER = 0x00000001
EF_MIPS_PIC       = 0x00000002
EF_MIPS_CPIC      = 0x00000004
EF_MIPS_NAN2008   = 0x00000400   # bit 10
EF_MIPS_ABI_MASK  = 0x0000F000   # bits 15:12
EF_MIPS_ABI_O32   = 0x00001000   # O32 ABI (value within mask)
EF_MIPS_ARCH_MASK = 0xF0000000   # bits 31:28
EF_MIPS_ARCH_32R2 = 0x70000000   # MIPS32r2

# Canonical device e_flags — all three required bits set plus the "noise" bits
# that readelf/binutils show on real cross-compiled objects.
DEVICE_EFLAGS = (
    EF_MIPS_ARCH_32R2
    | EF_MIPS_ABI_O32
    | EF_MIPS_NAN2008
    | EF_MIPS_NOREORDER
    | EF_MIPS_PIC
    | EF_MIPS_CPIC
)
assert DEVICE_EFLAGS == 0x70001407, hex(DEVICE_EFLAGS)

# .MIPS.abiflags struct (24 bytes, little-endian)
#  offset  size  field
#  0       2     version (0)
#  2       1     isa_level (32)
#  3       1     isa_rev (2 for mips32r2)
#  4       1     gpr_size (0=ANY)
#  5       1     cpr1_size (3 = DOUBLE → FP64 registers)
#  6       1     cpr2_size (0)
#  7       1     fp_abi
#  8       4     isa_ext (0)
#  12      4     ases (0)
#  16      4     flags1 (0)
#  20      4     flags2 (0)

# fp_abi values (MIPS .MIPS.abiflags fp_abi field)
FP_ABI_DOUBLE = 1   # legacy / soft
FP_ABI_FPXX   = 5
FP_ABI_FP64   = 6
FP_ABI_FP64A  = 7

# Expected loader
LOADER = "/lib/ld-linux-mipsn8.so.1"


# ──────────────────────────────────────────────────────────────────────────────
# struct helpers
# ──────────────────────────────────────────────────────────────────────────────

def _pack_ehdr(
    ei_class: int,
    ei_data: int,
    e_type: int,
    e_machine: int,
    e_flags: int,
    e_phoff: int,
    e_phnum: int,
    e_shoff: int,
    e_shnum: int,
    e_shstrndx: int,
) -> bytes:
    """Pack a 52-byte ELF32 header."""
    ident = struct.pack(
        "4sBBBBBxxxxxxx",
        ELFMAG,
        ei_class,   # EI_CLASS
        ei_data,    # EI_DATA
        1,          # EI_VERSION = EV_CURRENT
        0,          # EI_OSABI = ELFOSABI_NONE
        0,          # EI_ABIVERSION
    )
    assert len(ident) == 16
    # ELF32 Ehdr rest — 36 bytes
    rest = struct.pack(
        "<HHIIIIIHHHHHH",
        e_type,
        e_machine,
        1,           # e_version = EV_CURRENT
        0,           # e_entry
        e_phoff,     # e_phoff
        e_shoff,     # e_shoff
        e_flags,
        52,          # e_ehsize
        32,          # e_phentsize (Phdr32 = 32 bytes)
        e_phnum,
        40,          # e_shentsize (Shdr32 = 40 bytes)
        e_shnum,
        e_shstrndx,
    )
    assert len(rest) == 36
    return ident + rest


def _pack_phdr(p_type: int, p_offset: int, p_filesz: int, p_memsz: int) -> bytes:
    """Pack a 32-byte ELF32 program header (Phdr32)."""
    return struct.pack(
        "<IIIIIIII",
        p_type,
        p_offset,   # p_offset
        0,          # p_vaddr
        0,          # p_paddr
        p_filesz,   # p_filesz
        p_memsz,    # p_memsz
        4,          # p_flags = PF_R
        0,          # p_align
    )


def _pack_shdr(
    sh_name: int,
    sh_type: int,
    sh_offset: int,
    sh_size: int,
    sh_link: int = 0,
    sh_info: int = 0,
    sh_addralign: int = 1,
    sh_entsize: int = 0,
) -> bytes:
    """Pack a 40-byte ELF32 section header (Shdr32)."""
    return struct.pack(
        "<IIIIIIIIII",
        sh_name,
        sh_type,
        0,           # sh_flags
        0,           # sh_addr
        sh_offset,
        sh_size,
        sh_link,
        sh_info,
        sh_addralign,
        sh_entsize,
    )


def _pack_abiflags(
    fp_abi: int,
    isa_level: int = 32,
    isa_rev: int = 2,
    cpr1_size: int = 3,
) -> bytes:
    """Pack a 24-byte .MIPS.abiflags struct."""
    return struct.pack(
        "<HBBBBBBiiii",
        0,           # version
        isa_level,
        isa_rev,
        0,           # gpr_size
        cpr1_size,
        0,           # cpr2_size
        fp_abi,
        0,           # isa_ext
        0,           # ases
        0,           # flags1
        0,           # flags2
    )


# ──────────────────────────────────────────────────────────────────────────────
# ELF builder
# ──────────────────────────────────────────────────────────────────────────────

def build_elf(
    *,
    ei_data: int = ELFDATA2LSB,
    e_type: int = ET_EXEC,
    e_machine: int = EM_MIPS,
    e_flags: int = DEVICE_EFLAGS,
    fp_abi: int = FP_ABI_FP64,
    interp: str | None = LOADER,          # None → omit PT_INTERP
    include_abiflags: bool = True,
) -> bytes:
    """Build a minimal ELF32 file with the given parameters.

    For ET_EXEC, *interp* is included as PT_INTERP (None means no interp).
    For ET_DYN, interp is always omitted.
    """
    if e_type == ET_DYN:
        interp = None

    # ── plan layout ──────────────────────────────────────────────────────────
    # Program headers start immediately after Ehdr (offset 52).
    phdrs: list[tuple[int, int, bytes]] = []   # (p_type, content_offset_placeholder, content)

    # We'll place content after all phdrs. Compute phdr table size first.
    # Number of phdrs: 1 (PT_LOAD) + 1 if interp
    n_phdrs = 1 + (1 if interp is not None else 0)
    phdr_table_size = n_phdrs * 32
    phdr_table_offset = 52

    # Content area starts after phdr table
    content_base = phdr_table_offset + phdr_table_size

    # abiflags content
    abiflags_data = _pack_abiflags(fp_abi) if include_abiflags else b""
    abiflags_offset = content_base
    abiflags_size   = len(abiflags_data)

    # interp content (null-terminated string)
    if interp is not None:
        interp_data   = interp.encode() + b"\x00"
        interp_offset = abiflags_offset + abiflags_size
        interp_size   = len(interp_data)
    else:
        interp_data   = b""
        interp_offset = abiflags_offset + abiflags_size
        interp_size   = 0

    # shstrtab: null-term strings for section names
    #   [0] = "" (SHN_UNDEF)
    #   [1] = ".shstrtab"
    #   [2] = ".MIPS.abiflags"  (only if include_abiflags)
    shstrtab = b"\x00.shstrtab\x00"
    shstrtab_name_shstrtab = 1   # offset of ".shstrtab" in shstrtab
    if include_abiflags:
        shstrtab_name_abiflags = len(shstrtab)
        shstrtab += b".MIPS.abiflags\x00"
    else:
        shstrtab_name_abiflags = 0

    shstrtab_offset = interp_offset + interp_size
    shstrtab_size   = len(shstrtab)

    # Section headers come after shstrtab, aligned to 4
    shdr_base = shstrtab_offset + shstrtab_size
    # Align to 4
    if shdr_base % 4 != 0:
        shdr_base += 4 - (shdr_base % 4)
    padding_before_shdrs = shdr_base - (shstrtab_offset + shstrtab_size)

    # Sections: [0]=NULL, [1]=.shstrtab, [2]=.MIPS.abiflags (if present)
    n_sections = 2 + (1 if include_abiflags else 0)
    shstrndx   = 1   # .shstrtab is always section 1

    # ── build phdrs ──────────────────────────────────────────────────────────
    # PT_LOAD covering the whole file (simplest valid ELF)
    # We'll compute total file size at the end; use placeholder.
    # For now, calculate where shdrs end.
    shdr_table_size = n_sections * 40
    total_size = shdr_base + shdr_table_size

    load_phdr = _pack_phdr(PT_LOAD, 0, total_size, total_size)

    phdr_bytes = load_phdr
    if interp is not None:
        interp_phdr = _pack_phdr(PT_INTERP, interp_offset, interp_size, interp_size)
        phdr_bytes += interp_phdr

    # ── build section headers ─────────────────────────────────────────────────
    null_shdr = _pack_shdr(0, SHT_NULL, 0, 0)
    shstrtab_shdr = _pack_shdr(
        shstrtab_name_shstrtab, SHT_STRTAB,
        shstrtab_offset, shstrtab_size
    )
    shdrs = null_shdr + shstrtab_shdr
    if include_abiflags:
        abiflags_shdr = _pack_shdr(
            shstrtab_name_abiflags, SHT_MIPS_ABIFLAGS,
            abiflags_offset, abiflags_size,
            sh_addralign=8,
        )
        shdrs += abiflags_shdr

    # ── build ELF header ──────────────────────────────────────────────────────
    ehdr = _pack_ehdr(
        ei_class   = ELFCLASS32,
        ei_data    = ei_data,
        e_type     = e_type,
        e_machine  = e_machine,
        e_flags    = e_flags,
        e_phoff    = phdr_table_offset,
        e_phnum    = n_phdrs,
        e_shoff    = shdr_base,
        e_shnum    = n_sections,
        e_shstrndx = shstrndx,
    )

    # ── assemble ──────────────────────────────────────────────────────────────
    body = (
        ehdr
        + phdr_bytes
        + abiflags_data
        + interp_data
        + shstrtab
        + (b"\x00" * padding_before_shdrs)
        + shdrs
    )
    assert len(body) == total_size, f"{len(body)} != {total_size}"
    return body


# ──────────────────────────────────────────────────────────────────────────────
# Fixture catalogue
# ──────────────────────────────────────────────────────────────────────────────

def _bad_eflags_clear(mask: int) -> int:
    """Return DEVICE_EFLAGS with the bits in *mask* cleared (a wrong value)."""
    return DEVICE_EFLAGS & ~mask


# Each entry: (filename, kwargs to build_elf or special-case)
FIXTURES: list[tuple[str, dict]] = [
    # ── good fixtures ─────────────────────────────────────────────────────────
    (
        "good_exec.elf",
        dict(
            e_type    = ET_EXEC,
            e_machine = EM_MIPS,
            ei_data   = ELFDATA2LSB,
            e_flags   = DEVICE_EFLAGS,
            fp_abi    = FP_ABI_FP64,
            interp    = LOADER,
        ),
    ),
    (
        "good_dyn.elf",
        dict(
            e_type    = ET_DYN,
            e_machine = EM_MIPS,
            ei_data   = ELFDATA2LSB,
            e_flags   = DEVICE_EFLAGS,
            fp_abi    = FP_ABI_FP64,
            interp    = None,   # ET_DYN never has interp; build_elf enforces this
        ),
    ),
    # ── bad-machine ───────────────────────────────────────────────────────────
    (
        "bad_machine.elf",
        dict(
            e_machine = EM_ARM,
            e_flags   = DEVICE_EFLAGS,
            fp_abi    = FP_ABI_FP64,
            interp    = LOADER,
        ),
    ),
    # ── bad-endianness (EI_DATA = MSB) ───────────────────────────────────────
    # NOTE: we set EI_DATA=2 (MSB) but the rest of the fields are still written
    # LE.  A real MSB ELF would byte-swap everything; our parser detects the
    # EI_DATA byte before attempting field parsing, so this is sufficient to
    # trigger the endianness violation without needing a fully-byte-swapped ELF.
    (
        "bad_endian.elf",
        dict(ei_data = ELFDATA2MSB, interp = LOADER),
    ),
    # ── bad-nan2008 (bit 10 cleared) ─────────────────────────────────────────
    (
        "bad_nan2008.elf",
        dict(
            e_flags = _bad_eflags_clear(EF_MIPS_NAN2008),
            fp_abi  = FP_ABI_FP64,
            interp  = LOADER,
        ),
    ),
    # ── bad-o32 (ABI bits set to zero instead of O32) ────────────────────────
    (
        "bad_o32.elf",
        dict(
            e_flags = _bad_eflags_clear(EF_MIPS_ABI_MASK),
            fp_abi  = FP_ABI_FP64,
            interp  = LOADER,
        ),
    ),
    # ── bad-mips32r2 (arch bits set to zero) ─────────────────────────────────
    (
        "bad_mips32r2.elf",
        dict(
            e_flags = _bad_eflags_clear(EF_MIPS_ARCH_MASK),
            fp_abi  = FP_ABI_FP64,
            interp  = LOADER,
        ),
    ),
    # ── bad-fp_abi (FP_ABI_DOUBLE = 1, not in accepted set) ──────────────────
    (
        "bad_fp_abi.elf",
        dict(
            e_flags = DEVICE_EFLAGS,
            fp_abi  = FP_ABI_DOUBLE,      # 1 — real abiflags value, definitely wrong
            interp  = LOADER,
        ),
    ),
    # ── bad-loader (wrong PT_INTERP string) ───────────────────────────────────
    (
        "bad_loader.elf",
        dict(
            e_flags = DEVICE_EFLAGS,
            fp_abi  = FP_ABI_FP64,
            interp  = "/lib/ld-linux.so.3",   # plausible but wrong
        ),
    ),
]


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    for filename, kwargs in FIXTURES:
        data = build_elf(**kwargs)
        path = os.path.join(here, filename)
        with open(path, "wb") as f:
            f.write(data)
        print(f"  wrote {filename:35s}  ({len(data)} bytes)")
    print("Done.")


if __name__ == "__main__":
    main()
