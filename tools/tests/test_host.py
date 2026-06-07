"""chelper_sources() tests — slice A2.

TDD order: RED (this file, no host.py yet) → GREEN (host.py) → REFACTOR.

chelper_sources(init_py: str | Path) -> list[str]
  - str:  treated as Python source text (pure, no I/O in the function itself)
  - Path: the function reads the file then delegates to the str path

The snapshot fixture ``fixtures/chelper_sources.txt`` was generated from the
pinned submodule at ``external/klipper/klippy/chelper/__init__.py`` (21 files,
klipper e60fe3d).  The test parses that same real file so a submodule bump is
caught with a reviewable set-diff rather than a silent count mismatch.
"""

from __future__ import annotations

import pathlib
import textwrap

import pytest

from build.host import ChelperParseError, chelper_sources

# ──────────────────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────────────────

_FIXTURES = pathlib.Path(__file__).parent / "fixtures"
_SNAPSHOT  = _FIXTURES / "chelper_sources.txt"

# Real pinned submodule — this is the authoritative source for the snapshot.
_REAL_CHELPER = (
    pathlib.Path(__file__).parent.parent.parent
    / "external" / "klipper" / "klippy" / "chelper" / "__init__.py"
)


def _load_snapshot() -> frozenset[str]:
    return frozenset(_SNAPSHOT.read_text().splitlines())


# ──────────────────────────────────────────────────────────────────────────────
# § A2-1 — snapshot test (the named A2 test)
#   Parses the real submodule file and asserts the result matches the snapshot.
#   A set-diff failure is reviewable; it explicitly shows added/removed files.
# ──────────────────────────────────────────────────────────────────────────────

class TestChelperSourcesSnapshot:
    def test_matches_snapshot_set(self):
        """Parsed SOURCE_FILES from real chelper __init__.py matches snapshot."""
        actual   = frozenset(chelper_sources(_REAL_CHELPER))
        expected = _load_snapshot()

        added   = actual - expected
        removed = expected - actual

        assert not added and not removed, (
            f"chelper SOURCE_FILES drift vs snapshot:\n"
            f"  added  : {sorted(added)}\n"
            f"  removed: {sorted(removed)}\n"
            f"If this is intentional (submodule bump), regenerate "
            f"tools/tests/fixtures/chelper_sources.txt."
        )

    def test_result_count(self):
        """Convenience: 21 files in current pinned submodule."""
        assert len(chelper_sources(_REAL_CHELPER)) == 21

    def test_path_input_accepted(self):
        """Path input (not str) works — function handles file reading."""
        result = chelper_sources(_REAL_CHELPER)
        assert isinstance(result, list)
        assert all(f.endswith(".c") for f in result)

    def test_str_input_accepted(self):
        """str source-text input works (pure path, no I/O inside the function)."""
        src = _REAL_CHELPER.read_text()
        result = chelper_sources(src)
        assert frozenset(result) == _load_snapshot()

    def test_order_preserved(self):
        """Order from the source list literal is preserved (list, not set)."""
        result = chelper_sources(_REAL_CHELPER)
        # First and last entries from the real file
        assert result[0] == "pyhelper.c"
        assert result[-1] == "kin_generic.c"


# ──────────────────────────────────────────────────────────────────────────────
# § A2-2 — minimal well-formed sample
#   Verifies correct extraction on a small self-contained snippet.
# ──────────────────────────────────────────────────────────────────────────────

_MINIMAL_SAMPLE = textwrap.dedent("""\
    SOURCE_FILES = ['alpha.c', 'beta.c', 'gamma.c']
    HC_SOURCE_FILES = ['hub-ctrl.c']
""")


class TestChelperSourcesMinimal:
    def test_minimal_sample_yields_correct_files(self):
        result = chelper_sources(_MINIMAL_SAMPLE)
        assert result == ["alpha.c", "beta.c", "gamma.c"]

    def test_hc_source_files_not_included(self):
        """HC_SOURCE_FILES (hub-ctrl) must not appear in the result."""
        result = chelper_sources(_MINIMAL_SAMPLE)
        assert "hub-ctrl.c" not in result

    def test_multiline_list_parsed(self):
        src = textwrap.dedent("""\
            SOURCE_FILES = [
                'one.c',
                'two.c',
            ]
        """)
        assert chelper_sources(src) == ["one.c", "two.c"]


# ──────────────────────────────────────────────────────────────────────────────
# § A2-3 — malformed input → ChelperParseError
# ──────────────────────────────────────────────────────────────────────────────

class TestChelperParseError:
    def test_missing_source_files_raises(self):
        """No SOURCE_FILES assignment at all → ChelperParseError."""
        src = "HC_SOURCE_FILES = ['hub-ctrl.c']\n"
        with pytest.raises(ChelperParseError, match="SOURCE_FILES"):
            chelper_sources(src)

    def test_non_list_raises(self):
        """SOURCE_FILES = 'a string' (not a list) → ChelperParseError."""
        src = "SOURCE_FILES = 'pyhelper.c'\n"
        with pytest.raises(ChelperParseError, match="SOURCE_FILES"):
            chelper_sources(src)

    def test_non_literal_element_raises(self):
        """A variable reference inside the list → ChelperParseError (no silent skip)."""
        src = "SOURCE_FILES = ['good.c', some_var, 'other.c']\n"
        with pytest.raises(ChelperParseError, match="SOURCE_FILES"):
            chelper_sources(src)

    def test_fstring_element_raises(self):
        """An f-string element → ChelperParseError (JoinedStr, not Constant)."""
        src = "SOURCE_FILES = [f'kin_{name}.c']\n"
        with pytest.raises(ChelperParseError, match="SOURCE_FILES"):
            chelper_sources(src)

    def test_non_string_literal_element_raises(self):
        """An integer literal inside the list → ChelperParseError."""
        src = "SOURCE_FILES = ['good.c', 42]\n"
        with pytest.raises(ChelperParseError, match="SOURCE_FILES"):
            chelper_sources(src)

    def test_syntax_error_raises(self):
        """Unparseable source text → ChelperParseError wrapping SyntaxError."""
        src = "SOURCE_FILES = ['a.c'\n"   # unclosed bracket
        with pytest.raises(ChelperParseError):
            chelper_sources(src)

    def test_path_not_found_raises(self):
        """Path that doesn't exist → ChelperParseError (wraps FileNotFoundError)."""
        missing = pathlib.Path("/nonexistent/chelper/__init__.py")
        with pytest.raises(ChelperParseError, match="not found"):
            chelper_sources(missing)
