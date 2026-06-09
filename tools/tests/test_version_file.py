"""VERSION-file based resolve_version — behavioral tests.

Covers the new file-read contract (no git describe, no runner param).

TDD order (each RED→GREEN):
  VF-1: reads a valid VERSION file → returns bare semver
  VF-2: strips surrounding whitespace/newlines
  VF-3: accepts a prerelease suffix
  VF-4: raises ReleaseError when VERSION is missing
  VF-5: raises ReleaseError when VERSION is empty/whitespace-only
  VF-6: raises ReleaseError when content is malformed (various cases)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from build.release import ReleaseError, resolve_version


# ──────────────────────────────────────────────────────────────────────────────
# VF-1: reads a valid VERSION file, returns bare semver
# ──────────────────────────────────────────────────────────────────────────────

class TestResolveVersionReadsFile:
    def test_returns_bare_semver(self, tmp_path):
        (tmp_path / "VERSION").write_text("1.2.3\n")
        assert resolve_version(tmp_path) == "1.2.3"

    def test_three_part_version(self, tmp_path):
        (tmp_path / "VERSION").write_text("0.1.0\n")
        assert resolve_version(tmp_path) == "0.1.0"

    def test_large_version_numbers(self, tmp_path):
        (tmp_path / "VERSION").write_text("10.20.300\n")
        assert resolve_version(tmp_path) == "10.20.300"


# ──────────────────────────────────────────────────────────────────────────────
# VF-2: strips surrounding whitespace/newlines
# ──────────────────────────────────────────────────────────────────────────────

class TestResolveVersionStripsWhitespace:
    def test_strips_trailing_newline(self, tmp_path):
        (tmp_path / "VERSION").write_text("1.2.3\n")
        assert resolve_version(tmp_path) == "1.2.3"

    def test_strips_leading_whitespace(self, tmp_path):
        (tmp_path / "VERSION").write_text("  1.2.3")
        assert resolve_version(tmp_path) == "1.2.3"

    def test_strips_trailing_whitespace(self, tmp_path):
        (tmp_path / "VERSION").write_text("1.2.3  ")
        assert resolve_version(tmp_path) == "1.2.3"

    def test_strips_crlf(self, tmp_path):
        (tmp_path / "VERSION").write_bytes(b"1.2.3\r\n")
        assert resolve_version(tmp_path) == "1.2.3"


# ──────────────────────────────────────────────────────────────────────────────
# VF-3: accepts a prerelease suffix
# ──────────────────────────────────────────────────────────────────────────────

class TestResolveVersionPrerelease:
    def test_rc_suffix(self, tmp_path):
        (tmp_path / "VERSION").write_text("0.2.0-rc.1\n")
        assert resolve_version(tmp_path) == "0.2.0-rc.1"

    def test_alpha_suffix(self, tmp_path):
        (tmp_path / "VERSION").write_text("1.0.0-alpha\n")
        assert resolve_version(tmp_path) == "1.0.0-alpha"

    def test_numeric_prerelease(self, tmp_path):
        (tmp_path / "VERSION").write_text("0.1.0-1\n")
        assert resolve_version(tmp_path) == "0.1.0-1"

    def test_complex_prerelease(self, tmp_path):
        (tmp_path / "VERSION").write_text("1.2.3-beta.2\n")
        assert resolve_version(tmp_path) == "1.2.3-beta.2"


# ──────────────────────────────────────────────────────────────────────────────
# VF-4: raises ReleaseError when VERSION is missing
# ──────────────────────────────────────────────────────────────────────────────

class TestResolveVersionMissingFile:
    def test_raises_when_version_file_absent(self, tmp_path):
        with pytest.raises(ReleaseError):
            resolve_version(tmp_path)

    def test_error_message_mentions_version_file(self, tmp_path):
        with pytest.raises(ReleaseError) as exc_info:
            resolve_version(tmp_path)
        assert "VERSION" in str(exc_info.value)

    def test_error_message_mentions_prepare_version(self, tmp_path):
        """Error message must guide users toward the CI prepare-version job."""
        with pytest.raises(ReleaseError) as exc_info:
            resolve_version(tmp_path)
        msg = str(exc_info.value)
        assert "prepare-version" in msg or "prepare_version" in msg or "CI" in msg


# ──────────────────────────────────────────────────────────────────────────────
# VF-5: raises ReleaseError when VERSION is empty or whitespace-only
# ──────────────────────────────────────────────────────────────────────────────

class TestResolveVersionEmptyFile:
    def test_raises_on_empty_file(self, tmp_path):
        (tmp_path / "VERSION").write_text("")
        with pytest.raises(ReleaseError):
            resolve_version(tmp_path)

    def test_raises_on_whitespace_only(self, tmp_path):
        (tmp_path / "VERSION").write_text("   \n")
        with pytest.raises(ReleaseError):
            resolve_version(tmp_path)

    def test_raises_on_newline_only(self, tmp_path):
        (tmp_path / "VERSION").write_text("\n")
        with pytest.raises(ReleaseError):
            resolve_version(tmp_path)

    def test_error_message_mentions_version_file(self, tmp_path):
        (tmp_path / "VERSION").write_text("")
        with pytest.raises(ReleaseError) as exc_info:
            resolve_version(tmp_path)
        assert "VERSION" in str(exc_info.value)


# ──────────────────────────────────────────────────────────────────────────────
# VF-6: raises ReleaseError when content is malformed
# ──────────────────────────────────────────────────────────────────────────────

class TestResolveVersionMalformed:
    def test_rejects_not_a_version(self, tmp_path):
        (tmp_path / "VERSION").write_text("not-a-version\n")
        with pytest.raises(ReleaseError):
            resolve_version(tmp_path)

    def test_rejects_two_part_version(self, tmp_path):
        """Must be X.Y.Z — two-part is invalid."""
        (tmp_path / "VERSION").write_text("1.2\n")
        with pytest.raises(ReleaseError):
            resolve_version(tmp_path)

    def test_rejects_v_prefix(self, tmp_path):
        """File must be bare (no v prefix) — CI prepare-version writes bare."""
        (tmp_path / "VERSION").write_text("v1.2.3\n")
        with pytest.raises(ReleaseError):
            resolve_version(tmp_path)

    def test_rejects_freeform_text(self, tmp_path):
        (tmp_path / "VERSION").write_text("release-1.2.3\n")
        with pytest.raises(ReleaseError):
            resolve_version(tmp_path)

    def test_rejects_four_part_version(self, tmp_path):
        (tmp_path / "VERSION").write_text("1.2.3.4\n")
        with pytest.raises(ReleaseError):
            resolve_version(tmp_path)

    def test_error_message_mentions_version_file(self, tmp_path):
        (tmp_path / "VERSION").write_text("not-a-version\n")
        with pytest.raises(ReleaseError) as exc_info:
            resolve_version(tmp_path)
        assert "VERSION" in str(exc_info.value)
