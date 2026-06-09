"""Pure, unit-testable helpers for build.py image operations.

Extracted so tests can import without side-effects from the CLI entry-point.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO = Path(__file__).resolve().parent.parent          # tools/build_main.py -> repo root
TOOLCHAIN = REPO / "toolchain"
_DIGEST_FILE = TOOLCHAIN / "IMAGE_DIGEST"

_DIGEST_RE = re.compile(r"sha256:([0-9a-f]{64})(?!\w)")
_BARE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# ---------------------------------------------------------------------------
# Helper: parse_repo_digest
# ---------------------------------------------------------------------------


def parse_repo_digest(inspect_output: str) -> str:
    """Extract a bare ``sha256:<64-hex>`` token from container inspect output.

    Both runtimes emit the pushed image's RepoDigest as a line like:

        ghcr.io/coreyleavitt/v3ke-toolchain@sha256:<64hex>

    Podman may wrap it in list brackets (``[...]``) or quotes; Docker usually
    does not.  This function is tolerant of leading/trailing whitespace and
    bracket/quote wrapping, but strict about the digest format itself.

    Args:
        inspect_output: Raw stdout from ``<runtime> inspect --format
            '{{index .RepoDigests 0}}' <image>``.

    Returns:
        The bare ``sha256:<64-hex>`` token, e.g.
        ``sha256:abc123...`` (70 chars total).

    Raises:
        ValueError: If no valid ``sha256:<64-hex>`` token is found in the
            input, or if the hex portion is not exactly 64 lowercase characters.
    """
    text = inspect_output.strip()

    # Fast path: bare digest with nothing else
    if _BARE_DIGEST_RE.match(text):
        return text

    # Find all sha256: occurrences followed by exactly 64 lowercase hex chars
    # that are NOT followed by more word characters (would indicate wrong length).
    match = _DIGEST_RE.search(text)
    if match is None:
        raise ValueError(
            f"No valid sha256:<64-hex> digest found in inspect output: {inspect_output!r}. "
            "Expected output like 'ghcr.io/image@sha256:<64 lowercase hex chars>'."
        )

    return "sha256:" + match.group(1)


# ---------------------------------------------------------------------------
# Helper: write_image_digest
# ---------------------------------------------------------------------------

_COMMENT_HEADER = """\
# Toolchain image digest — managed by: build.py image --push
#
# This file is updated by the local operator after building and pushing the
# toolchain image.  CI pulls the image strictly by this digest (no inline-
# build fallback).  The grep pattern used by CI is:
#
#   grep -E '^sha256:[0-9a-f]{{64}}$' toolchain/IMAGE_DIGEST | head -1
#
# Do not edit manually; run: python3 tools/build.py --image <ref> image --push
"""


def write_image_digest(path: Path, digest: str) -> None:
    """Write *digest* into the IMAGE_DIGEST file at *path*.

    The file will contain a short comment header followed by the bare
    ``sha256:...`` line.  The CI grep pattern
    ``^sha256:[0-9a-f]{64}$`` must match exactly that one line.

    Args:
        path: Destination path (e.g. ``toolchain/IMAGE_DIGEST``).  Any
            existing content is replaced.
        digest: Must match ``^sha256:[0-9a-f]{64}$`` exactly.

    Raises:
        ValueError: If *digest* does not match the required format.
    """
    if not _BARE_DIGEST_RE.match(digest):
        raise ValueError(
            f"Invalid digest {digest!r}: must match sha256:[0-9a-f]{{64}} "
            "(exactly 64 lowercase hex chars, no prefix, no suffix)."
        )
    content = _COMMENT_HEADER + digest + "\n"
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Runner type alias
# ---------------------------------------------------------------------------

Runner = Callable[[list[str]], str | None]


def _default_runner(cmd: list[str]) -> None:
    """Real runner: prints + executes, returns None."""
    print("+ " + " ".join(map(str, cmd)), flush=True)
    subprocess.run(cmd, check=True)


def _capturing_runner(cmd: list[str]) -> str:
    """Runner used for inspect: captures and returns stdout."""
    print("+ " + " ".join(map(str, cmd)), flush=True)
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# cmd_image
# ---------------------------------------------------------------------------


def cmd_image(
    a,
    *,
    runner: Runner | None = None,
    digest_path: Path | None = None,
) -> None:
    """Build (and optionally push + pin) the toolchain container image.

    Args:
        a: Parsed argparse namespace.  Expected attributes:

            - ``a.runtime``  – ``"podman"`` or ``"docker"``
            - ``a.image``    – image tag/ref (e.g. ``ghcr.io/org/repo:v0.1.0``)
            - ``a.ctng_version`` – optional build-arg override
            - ``a.push``     – bool; when True, push after build, capture
              the registry digest via ``inspect``, and write it to
              *digest_path* (or the default ``toolchain/IMAGE_DIGEST``).

        runner: Callable ``(cmd: list[str]) -> str | None``.  Defaults to the
            real subprocess runner.  Inject a fake for unit tests.
        digest_path: Where to write IMAGE_DIGEST.  Defaults to
            ``toolchain/IMAGE_DIGEST`` relative to the repo root.
    """
    runtime: str = getattr(a, "runtime", "podman")
    push: bool = getattr(a, "push", False)
    image: str = a.image

    if runner is None:
        _run = _default_runner
        _inspect_run = _capturing_runner
    else:
        _run = runner
        _inspect_run = runner

    # 1. Build
    build_cmd = [runtime, "build", "-t", image, "-f", str(TOOLCHAIN / "Containerfile")]
    if getattr(a, "ctng_version", None):
        build_cmd += ["--build-arg", f"CTNG_VERSION={a.ctng_version}"]
    build_cmd.append(str(TOOLCHAIN))
    _run(build_cmd)

    if not push:
        return

    # 2. Push
    push_cmd = [runtime, "push", image]
    _run(push_cmd)

    # 3. Inspect — capture RepoDigests[0] post-push
    inspect_cmd = [
        runtime, "inspect",
        "--format", "{{index .RepoDigests 0}}",
        image,
    ]
    raw = _inspect_run(inspect_cmd)

    digest = parse_repo_digest(raw or "")

    # 4. Write IMAGE_DIGEST
    if digest_path is None:
        digest_path = _DIGEST_FILE
    write_image_digest(digest_path, digest)
    print(f"IMAGE_DIGEST written: {digest_path} ({digest})", flush=True)
