"""B1 — the cross-toolchain base image is digest-pinned, and the non-reproducible
``zypper dup`` layer is documented as acknowledged.

Rationale (RFC B1): a rolling ``:latest`` base makes the image build silently drift,
so the base is pinned to an immutable ``@sha256:`` digest.  But pinning the base does
*not* make the image layers reproducible — the ``zypper dup``/install step still pulls
whatever the mirror serves.  That non-reproducibility is accepted on purpose (the image
is a cache; the from-source crosstool-ng build verified in B4 is the real anchor).  We
assert a comment records this so a future contributor doesn't "fix" it and break the
fast path.

These are constraint tests over the Containerfile text — hermetic, no network, no podman.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


def _repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    )
    return Path(out.stdout.strip())


@pytest.fixture(scope="module")
def containerfile_text() -> str:
    path = _repo_root() / "toolchain" / "Containerfile"
    return path.read_text(encoding="utf-8")


def _from_lines(text: str) -> list[str]:
    """Active (non-comment) FROM directives in the Containerfile."""
    lines = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        if re.match(r"(?i)^FROM\s+", stripped):
            lines.append(stripped)
    return lines


def test_has_a_from_directive(containerfile_text: str) -> None:
    assert _from_lines(containerfile_text), "toolchain/Containerfile has no FROM directive"


def test_base_image_is_digest_pinned(containerfile_text: str) -> None:
    """Every FROM pins an immutable @sha256: digest."""
    froms = _from_lines(containerfile_text)
    for line in froms:
        assert "@sha256:" in line, f"FROM is not digest-pinned: {line!r}"


def test_base_image_is_not_a_mutable_latest_tag(containerfile_text: str) -> None:
    """No FROM uses the mutable :latest tag (the whole point of B1)."""
    for line in _from_lines(containerfile_text):
        assert ":latest" not in line, f"FROM still uses a mutable :latest tag: {line!r}"


def test_digest_is_a_full_sha256(containerfile_text: str) -> None:
    """The pinned digest is a well-formed 64-hex sha256 (not a truncated stub)."""
    froms = _from_lines(containerfile_text)
    for line in froms:
        m = re.search(r"@sha256:([0-9a-f]+)", line)
        assert m, f"FROM has no sha256 digest: {line!r}"
        assert len(m.group(1)) == 64, (
            f"sha256 digest is not 64 hex chars (got {len(m.group(1))}): {line!r}"
        )


def test_zypper_dup_documented_as_non_reproducible(containerfile_text: str) -> None:
    """A comment acknowledges the zypper dup/install layer as non-reproducible.

    Guards against a future contributor trying to pin/remove dup to chase
    reproducibility — the comment records that this layer is a cache, not the anchor.
    """
    comment_blob = "\n".join(
        line for line in containerfile_text.splitlines() if line.lstrip().startswith("#")
    ).lower()
    assert "dup" in comment_blob, "no comment mentions `zypper dup`"
    assert re.search(r"(not .*reproducib|non-reproducib|not byte-reproducib)", comment_blob), (
        "no comment acknowledges the dup layer as non-reproducible"
    )
