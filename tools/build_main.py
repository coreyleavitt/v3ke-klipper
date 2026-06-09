"""Pure, unit-testable helpers for build.py image operations.

Extracted so tests can import without side-effects from the CLI entry-point.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
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
# Helper: extract_pushed_digest
# ---------------------------------------------------------------------------


def extract_pushed_digest(
    push_stdout: str,
    digestfile_text: str | None = None,
) -> str:
    """Return the bare ``sha256:<64-hex>`` digest captured from a push operation.

    Two sources are tried in priority order:

    1. **digestfile_text** (podman path) — podman writes the exact registry
       digest to a file via ``--digestfile <path>``.  Pass the file's contents
       here.  If non-empty after stripping, it is validated and returned
       directly without consulting *push_stdout*.

    2. **push_stdout** (docker path) — docker prints a line of the form
       ``<tag>: digest: sha256:<64hex> size: <n>`` on stdout.  Any
       ``sha256:<64hex>`` token found in the output is accepted.

    Args:
        push_stdout: Captured stdout from the push command.
        digestfile_text: Contents of the ``--digestfile`` output file, or
            ``None`` / empty string if no digestfile was used.

    Returns:
        Bare ``sha256:<64-hex>`` token, e.g. ``sha256:abc123...`` (70 chars).

    Raises:
        ValueError: If the chosen source does not yield a valid
            ``sha256:[0-9a-f]{64}`` token.
    """
    # --- Digestfile path (podman) ---
    if digestfile_text:
        candidate = digestfile_text.strip()
        if not _BARE_DIGEST_RE.match(candidate):
            raise ValueError(
                f"Invalid digest in digestfile: {candidate!r}. "
                "Expected exactly 'sha256:<64 lowercase hex chars>'."
            )
        return candidate

    # --- Stdout path (docker, or podman fallback) ---
    match = _DIGEST_RE.search(push_stdout)
    if match is None:
        raise ValueError(
            f"No valid sha256:<64-hex> digest found in push stdout: {push_stdout!r}. "
            "Expected output containing 'sha256:<64 lowercase hex chars>'."
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
#   grep -E '^sha256:[0-9a-f]{64}$' toolchain/IMAGE_DIGEST | head -1
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


def _capturing_runner(cmd: list[str]) -> str:
    """Real runner: prints, executes, captures and returns stdout."""
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
              the registry digest from the push output (podman: via
              ``--digestfile``; docker: from push stdout), and write it
              to *digest_path* (or the default ``toolchain/IMAGE_DIGEST``).

        runner: Callable ``(cmd: list[str]) -> str | None``.  Defaults to the
            real subprocess runner.  Inject a fake for unit tests.
        digest_path: Where to write IMAGE_DIGEST.  Defaults to
            ``toolchain/IMAGE_DIGEST`` relative to the repo root.
    """
    runtime: str = getattr(a, "runtime", "podman")
    push: bool = getattr(a, "push", False)
    image: str = a.image

    if runner is None:
        _run = _capturing_runner
    else:
        _run = runner

    # 1. Build
    build_cmd = [runtime, "build", "-t", image, "-f", str(TOOLCHAIN / "Containerfile")]
    if getattr(a, "ctng_version", None):
        build_cmd += ["--build-arg", f"CTNG_VERSION={a.ctng_version}"]
    build_cmd.append(str(TOOLCHAIN))
    _run(build_cmd)

    if not push:
        return

    # 2. Push — capture the registry digest from the push itself.
    #
    #   podman: ``--digestfile <path>`` writes the exact manifest digest that
    #           the registry accepted.  We read it back after the push.
    #   docker: ``docker push`` prints "digest: sha256:<64hex> size: …" on
    #           stdout; we parse that instead (docker has no --digestfile).
    #
    # In both cases the digest comes from the push operation itself, NOT from
    # a post-push ``inspect`` call.  inspect returns a locally-cached
    # RepoDigests value that may differ from what the registry actually served.

    digestfile_text: str | None = None

    if runtime == "podman":
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".digest", delete=False
        ) as _tf:
            digestfile_path = _tf.name
        push_cmd = [runtime, "push", "--digestfile", digestfile_path, image]
        push_stdout = _run(push_cmd) or ""
        try:
            digestfile_text = Path(digestfile_path).read_text(encoding="utf-8")
        except OSError:
            digestfile_text = ""
    else:
        # docker (and any unrecognised runtime): rely on stdout
        push_cmd = [runtime, "push", image]
        push_stdout = _run(push_cmd) or ""

    digest = extract_pushed_digest(push_stdout, digestfile_text=digestfile_text)

    # 3. Write IMAGE_DIGEST
    if digest_path is None:
        digest_path = _DIGEST_FILE
    write_image_digest(digest_path, digest)
    print(f"IMAGE_DIGEST written: {digest_path} ({digest})", flush=True)
