"""Shared make-triplet helpers — extracted to eliminate arm_mcu / host duplication.

Both ``arm_mcu.py`` (ARM firmware) and ``host.py`` (MIPS host artifacts) need the
same clean/olddefconfig/build triplet pattern, the same determinism variable set, and
the same nproc helper.  Extracting them here lets each module focus on its
domain-specific parameters (kconfig path, output path, kind, extra make vars like
CROSS_PREFIX) rather than re-implementing the same structure.

The two triplets share the make invocation shape but differ in:
  - ArtifactKind (RAW_FIRMWARE for ARM, EXECUTABLE for host MCU)
  - extra make variables (CROSS_PREFIX for host MCU, nothing extra for ARM)
  - output file name and kind

This module is internal (``_`` prefix) — callers are ``arm_mcu.py`` and ``host.py``
only; do not import from user code.

Public API
──────────
  nproc() -> int
      CPU count; falls back to 1 on platforms where os.cpu_count() is None.

  determinism_vars(repo_root, epoch) -> list[str]
      Return make KEY=VALUE overrides for a deterministic step:
      SOURCE_DATE_EPOCH=<epoch> and CFLAGS_EXTRA=-ffile-prefix-map…/-fdebug-prefix-map….

  make_steps(*, name_prefix, subproject_dir, kconfig_path, output_path, kind,
             repo_root, epoch, extra_vars) -> list[BuildStep]
      Build the clean / olddefconfig / build triplet for one make subproject.
      ``extra_vars`` is a list of additional ``KEY=VALUE`` strings inserted after
      KCONFIG_CONFIG and determinism vars (e.g. ``["CROSS_PREFIX=…"]``).
"""

from __future__ import annotations

import os
from pathlib import Path

from abi.abi_spec import ArtifactKind
from build.artifacts import BuildStep

__all__ = [
    "nproc",
    "determinism_vars",
    "make_steps",
]


def nproc() -> int:
    """Return the CPU count (falls back to 1 when os.cpu_count() returns None)."""
    return os.cpu_count() or 1


def determinism_vars(repo_root: Path, epoch: int) -> list[str]:
    """Return the make variable overrides for a deterministic build step.

    These are passed as extra arguments to make (``KEY=VALUE`` form) so they
    override anything in the Makefile/environment without modifying the
    process environment — cleaner and more auditable.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.  Used to construct the
        ``-ffile-prefix-map`` / ``-fdebug-prefix-map`` values that strip the
        local path prefix from debug info.
    epoch:
        Unix timestamp (seconds) to set as SOURCE_DATE_EPOCH.
    """
    prefix_map = f"{repo_root}/=/"
    cflags = f"-ffile-prefix-map={prefix_map} -fdebug-prefix-map={prefix_map}"
    return [
        f"SOURCE_DATE_EPOCH={epoch}",
        f"CFLAGS_EXTRA={cflags}",
    ]


def make_steps(
    *,
    name_prefix: str,
    subproject_dir: Path,
    kconfig_path: Path,
    output_path: Path,
    kind: ArtifactKind,
    repo_root: Path,
    epoch: int,
    extra_vars: list[str] | None = None,
) -> list[BuildStep]:
    """Build the clean / olddefconfig / build triplet for one make subproject.

    Parameters
    ----------
    name_prefix:
        Human-readable prefix for step names (e.g. "katapult", "klipper-mcu").
    subproject_dir:
        Directory passed to ``make -C``.
    kconfig_path:
        Absolute path to the ``.config`` / ``.config``-style kconfig file.
    output_path:
        Absolute path to the expected build artifact (used for ABI checking
        and the release manifest).  Pass ``None`` for side-effect-only steps.
    kind:
        ``ArtifactKind`` for the output artifact.
    repo_root:
        Repository root; forwarded to ``determinism_vars``.
    epoch:
        SOURCE_DATE_EPOCH value; forwarded to ``determinism_vars``.
    extra_vars:
        Additional ``KEY=VALUE`` make variable overrides inserted after
        KCONFIG_CONFIG and the determinism vars (e.g. ``["CROSS_PREFIX=…"]``).
        Defaults to an empty list.

    Returns
    -------
    list[BuildStep]
        A 3-element list: [clean, olddefconfig, build].
    """
    extra = extra_vars or []
    kconfig_arg = f"KCONFIG_CONFIG={kconfig_path}"
    det = determinism_vars(repo_root, epoch)
    dir_arg = f"-C{subproject_dir}"   # make -C<dir> — no space, portable

    clean = BuildStep(
        name=f"{name_prefix}-clean",
        cmd=["make", dir_arg, kconfig_arg, "clean"],
        output_path=None,               # clean is side-effect-only
        kind=kind,
    )
    olddefconfig = BuildStep(
        name=f"{name_prefix}-olddefconfig",
        cmd=["make", dir_arg, kconfig_arg, *det, *extra, "olddefconfig"],
        output_path=None,               # no artifact produced
        kind=kind,
    )
    build = BuildStep(
        name=f"{name_prefix}-build",
        cmd=["make", dir_arg, f"-j{nproc()}", kconfig_arg, *det, *extra],
        output_path=output_path,
        kind=kind,
    )
    return [clean, olddefconfig, build]
