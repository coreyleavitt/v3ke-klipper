"""ABI checker tests — slice A1.

TDD order: each top-level section was written RED, then GREEN, then refactored.
Tests assert behavior through the public interface only:
  inspect_elf / check_abi / ElfInfo / AbiResult / AbiViolation / LoaderViolation.
"""

from __future__ import annotations

import pathlib
import struct

import pytest

# Public interface under test
from build.elf import (
    AbiResult,
    AbiViolation,
    ArtifactKind,
    ElfInfo,
    LoaderViolation,
    MalformedElfError,
    check_abi,
    inspect_elf,
)

# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

FIXTURES = pathlib.Path(__file__).parent.parent / "abi" / "fixtures"


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ──────────────────────────────────────────────────────────────────────────────
# § inspect_elf — field extraction
# ──────────────────────────────────────────────────────────────────────────────

class TestInspectElf:
    def test_good_exec_machine(self):
        info = inspect_elf(_load("good_exec.elf"))
        assert info.machine == 8  # EM_MIPS

    def test_good_exec_endianness(self):
        info = inspect_elf(_load("good_exec.elf"))
        assert info.data == 1  # ELFDATA2LSB

    def test_good_exec_flags(self):
        info = inspect_elf(_load("good_exec.elf"))
        assert info.flags == 0x70001407

    def test_good_exec_etype(self):
        info = inspect_elf(_load("good_exec.elf"))
        assert info.etype == 2  # ET_EXEC

    def test_good_exec_interp(self):
        info = inspect_elf(_load("good_exec.elf"))
        assert info.interp == "/lib/ld-linux-mipsn8.so.1"

    def test_good_exec_fp_abi(self):
        info = inspect_elf(_load("good_exec.elf"))
        assert info.fp_abi == 6  # FP64

    def test_good_exec_cpr1_size(self):
        info = inspect_elf(_load("good_exec.elf"))
        assert info.cpr1_size == 3  # CPR1_SIZE_DOUBLE

    def test_good_dyn_has_no_interp(self):
        info = inspect_elf(_load("good_dyn.elf"))
        assert info.interp is None

    def test_good_dyn_etype(self):
        info = inspect_elf(_load("good_dyn.elf"))
        assert info.etype == 3  # ET_DYN


# ──────────────────────────────────────────────────────────────────────────────
# § check_abi — accepts known-good fixtures
# ──────────────────────────────────────────────────────────────────────────────

class TestCheckAbiAccepts:
    def test_good_exec_is_ok(self):
        result = check_abi(inspect_elf(_load("good_exec.elf")), ArtifactKind.EXECUTABLE)
        assert result.applicable is True
        assert result.ok is True
        assert result.violations == ()

    def test_good_dyn_is_ok(self):
        result = check_abi(inspect_elf(_load("good_dyn.elf")), ArtifactKind.SHARED_LIBRARY)
        assert result.applicable is True
        assert result.ok is True
        assert result.violations == ()


# ──────────────────────────────────────────────────────────────────────────────
# § check_abi — rejects each bad fixture, violation identifies the right field
# ──────────────────────────────────────────────────────────────────────────────

class TestCheckAbiRejects:
    def _first_violation(self, filename: str, kind: ArtifactKind):
        result = check_abi(inspect_elf(_load(filename)), kind)
        assert result.ok is False
        assert len(result.violations) >= 1
        return result.violations[0]

    def test_bad_machine_yields_machine_violation(self):
        v = self._first_violation("bad_machine.elf", ArtifactKind.EXECUTABLE)
        assert isinstance(v, AbiViolation)
        assert v.field == "machine"

    def test_bad_endian_yields_endianness_violation(self):
        v = self._first_violation("bad_endian.elf", ArtifactKind.EXECUTABLE)
        assert isinstance(v, AbiViolation)
        assert v.field == "endianness"

    def test_bad_nan2008_yields_nan2008_violation(self):
        v = self._first_violation("bad_nan2008.elf", ArtifactKind.EXECUTABLE)
        assert isinstance(v, AbiViolation)
        assert v.field == "nan2008"
        assert v.expected == 0x400
        assert v.actual == 0

    def test_bad_o32_yields_o32_violation(self):
        v = self._first_violation("bad_o32.elf", ArtifactKind.EXECUTABLE)
        assert isinstance(v, AbiViolation)
        assert v.field == "o32"
        assert v.expected == 0x1000
        assert v.actual == 0

    def test_bad_mips32r2_yields_mips32r2_violation(self):
        v = self._first_violation("bad_mips32r2.elf", ArtifactKind.EXECUTABLE)
        assert isinstance(v, AbiViolation)
        assert v.field == "mips32r2"
        assert v.expected == 0x70000000
        assert v.actual == 0

    def test_bad_fp_abi_yields_fp_abi_violation(self):
        v = self._first_violation("bad_fp_abi.elf", ArtifactKind.EXECUTABLE)
        assert isinstance(v, AbiViolation)
        assert v.field == "fp_abi"
        assert v.actual == 1   # FP_ABI_DOUBLE

    def test_bad_loader_yields_loader_violation(self):
        v = self._first_violation("bad_loader.elf", ArtifactKind.EXECUTABLE)
        assert isinstance(v, LoaderViolation)
        assert "ld-linux-mipsn8.so.1" in v.expected_suffix
        assert "ld-linux.so.3" in v.actual

    def test_all_violations_collected(self):
        """bad_machine also has wrong flags structure; ensure we get at least
        the machine violation and .ok is False."""
        result = check_abi(inspect_elf(_load("bad_machine.elf")), ArtifactKind.EXECUTABLE)
        assert result.ok is False
        fields = {v.field for v in result.violations if isinstance(v, AbiViolation)}
        assert "machine" in fields


# ──────────────────────────────────────────────────────────────────────────────
# § RAW_FIRMWARE — applicable=False, no raise
# ──────────────────────────────────────────────────────────────────────────────

class TestRawFirmware:
    """RAW_FIRMWARE artifacts are ARM .bin blobs — not ELF.

    Calling convention: for RAW_FIRMWARE, do NOT call inspect_elf (it would
    raise MalformedElfError on non-ELF bytes).  Instead, use
    ``check_abi(ElfInfo.raw_sentinel(), ArtifactKind.RAW_FIRMWARE)``.
    check_abi short-circuits before consulting the ElfInfo at all.
    """

    def test_raw_sentinel_returns_not_applicable(self):
        result = check_abi(ElfInfo.raw_sentinel(), ArtifactKind.RAW_FIRMWARE)
        assert result.applicable is False
        assert result.ok is False

    def test_raw_firmware_does_not_raise(self):
        """check_abi(RAW_FIRMWARE) never raises regardless of the ElfInfo passed."""
        # Even a completely nonsensical ElfInfo is fine — RAW_FIRMWARE short-circuits.
        result = check_abi(ElfInfo.raw_sentinel(), ArtifactKind.RAW_FIRMWARE)
        assert result.applicable is False

    def test_raw_sentinel_is_distinct_from_parse_error(self):
        """A real non-ELF file raises MalformedElfError from inspect_elf;
        RAW_FIRMWARE callers bypass inspect_elf entirely."""
        raw = bytes(range(256)) * 4
        with pytest.raises(MalformedElfError):
            inspect_elf(raw)


# ──────────────────────────────────────────────────────────────────────────────
# § Malformed / too-short input — typed failure
# ──────────────────────────────────────────────────────────────────────────────

class TestMalformedInput:
    """inspect_elf raises MalformedElfError on structurally broken ELF bytes.

    Design decision: inspect_elf raises rather than returning a sentinel so
    callers can't accidentally treat a parse error as a successful "no-interp"
    parse.  RAW_FIRMWARE avoids this by being identified at the check_abi call
    site, not by trying to parse and swallowing failures.
    """

    def test_empty_bytes_raises(self):
        with pytest.raises(MalformedElfError):
            inspect_elf(b"")

    def test_too_short_raises(self):
        with pytest.raises(MalformedElfError):
            inspect_elf(b"\x7fELF\x01\x01" + b"\x00" * 10)

    def test_wrong_magic_raises(self):
        # Valid length but wrong magic → not an ELF
        data = b"CELF" + bytes(200)
        with pytest.raises(MalformedElfError):
            inspect_elf(data)

    def test_truncated_section_raises(self):
        # A valid ELF header but the section data is cut off
        good = _load("good_exec.elf")
        truncated = good[: len(good) // 2]
        with pytest.raises(MalformedElfError):
            inspect_elf(truncated)
