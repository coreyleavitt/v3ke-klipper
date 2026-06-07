"""MIPS host artifacts boundary — chelper source extraction and build commands.

Public interface (A2)
─────────────────────
  chelper_sources(init_py: str | Path) -> list[str]
      Statically extract the ``SOURCE_FILES`` list from a Klipper
      ``chelper/__init__.py`` using the ``ast`` module.

      *init_py* is either:
        - a ``pathlib.Path`` (or anything path-like): the file is read and its
          text is parsed.  Raises ``ChelperParseError`` if the path does not exist.
        - a ``str``: treated as Python source text (pure — no I/O inside the
          function), enabling hermetic unit tests without touching the filesystem.

      Returns the ordered list of ``.c`` filenames exactly as declared in
      ``SOURCE_FILES``.  ``HC_SOURCE_FILES`` (hub-ctrl) is intentionally excluded.

      Raises ``ChelperParseError`` if:
        - the file/text cannot be parsed as valid Python,
        - ``SOURCE_FILES`` is absent,
        - ``SOURCE_FILES`` is not assigned a list literal, or
        - any element of the list is not a plain string literal (variable
          references, f-strings, integer literals, etc. all raise — no silent
          skipping, so callers are never surprised by a truncated list).

  ChelperParseError — raised by chelper_sources on any structural failure.

Public interface (A4)
─────────────────────
  c_helper_steps(repo_root, source_date_epoch, *, toolchain_root) -> list[BuildStep]
      Returns a one-element list containing the SHARED_LIBRARY BuildStep that
      compiles c_helper.so using the MIPS cross-gcc.  The command exactly mirrors
      build-chelper.sh, including the required ABI flags and determinism injections.

      The ABI flags ``-mips32r2 -mabi=32 -mhard-float -mfp64 -mnan=2008`` are
      REQUIRED (the A-spike confirmed -mfp64 is already the toolchain default;
      keeping it explicit here is belt-and-suspenders and harmless).

      SOURCE_DATE_EPOCH is injected via ``["env", "SOURCE_DATE_EPOCH=<epoch>", gcc, ...]``
      — the cleanest way to set env in a pure command list for a direct gcc invocation
      (make-variable overrides work for make; for gcc we use env(1) in the cmd list).

      Determinism flags ``-ffile-prefix-map`` and ``-fdebug-prefix-map`` are set
      proactively (RFC §3 determinism paragraph).

  klipper_mcu_steps(repo_root, source_date_epoch, *, toolchain_root) -> list[BuildStep]
      Returns a 3-step clean/olddefconfig/build triplet for the Linux host-MCU
      cross-build.  Mirrors build-klipper-host-mcu.sh.  Uses the same
      _make_steps helper as arm_mcu.py (via the shared _makesteps module) with
      CROSS_PREFIX added as an extra make variable.

      KCONFIG_CONFIG points at klipper/klipper_host_mcu/klipper-host-mcu.config.
      Output: external/klipper/out/klipper.elf (EXECUTABLE).
      The clean step is first — a clean tree prevents Klipper's buildcommands.py
      from embedding a live strftime/hostname timestamp.

  host_steps(repo_root, source_date_epoch, *, toolchain_root) -> list[BuildStep]
      Concatenation: c_helper_steps (1) + klipper_mcu_steps (3) = 4 steps.

Toolchain path derivation (for all A4 builders)
────────────────────────────────────────────────
  Given ``toolchain_root``:
    gcc    = toolchain_root / "bin" / "mipsel-buildroot-linux-gnu-gcc"
    sysroot = toolchain_root / "mipsel-buildroot-linux-gnu" / "sysroot"
    CROSS_PREFIX = str(toolchain_root / "bin" / "mipsel-buildroot-linux-gnu-")
"""

from __future__ import annotations

import ast
import pathlib
from pathlib import Path
from typing import Union

from abi.abi_spec import ArtifactKind
from build.artifacts import BuildStep
from build._makesteps import make_steps, determinism_vars, nproc

__all__ = [
    "ChelperParseError",
    "chelper_sources",
    "c_helper_steps",
    "klipper_mcu_steps",
    "host_steps",
]


# ──────────────────────────────────────────────────────────────────────────────
# Exception
# ──────────────────────────────────────────────────────────────────────────────

class ChelperParseError(Exception):
    """Raised when chelper_sources cannot extract a valid SOURCE_FILES list.

    Covers: file not found, syntax errors, missing assignment, non-list value,
    and any non-literal element (variable, f-string, integer, …).
    """


# ──────────────────────────────────────────────────────────────────────────────
# chelper_sources
# ──────────────────────────────────────────────────────────────────────────────

def chelper_sources(init_py: Union[str, pathlib.Path]) -> list[str]:
    """Extract the ``SOURCE_FILES`` list from a chelper ``__init__.py``.

    Parameters
    ----------
    init_py:
        Either a ``Path`` to the ``__init__.py`` file (read by this function)
        or a ``str`` containing the Python source text (pure — no I/O).

    Returns
    -------
    list[str]
        Ordered list of ``.c`` filenames from ``SOURCE_FILES``.

    Raises
    ------
    ChelperParseError
        On any structural failure (see module docstring for the full list).
    """
    source_text = _resolve_source(init_py)
    return _extract_source_files(source_text)


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _resolve_source(init_py: Union[str, pathlib.Path]) -> str:
    """Return source text.  Path → read file; str → return as-is."""
    if isinstance(init_py, str):
        return init_py

    p = pathlib.Path(init_py)
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ChelperParseError(
            f"chelper __init__.py not found: {p}"
        ) from None
    except OSError as exc:
        raise ChelperParseError(
            f"Cannot read chelper __init__.py ({p}): {exc}"
        ) from exc


def _extract_source_files(source_text: str) -> list[str]:
    """Parse *source_text* with the ``ast`` module and return SOURCE_FILES."""
    try:
        tree = ast.parse(source_text)
    except SyntaxError as exc:
        raise ChelperParseError(
            f"Cannot parse chelper __init__.py: {exc}"
        ) from exc

    # Walk top-level statements only — SOURCE_FILES is always a module-level
    # assignment in the Klipper chelper module; going deeper would risk picking
    # up unrelated local variables with the same name.
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not (isinstance(target, ast.Name) and target.id == "SOURCE_FILES"):
                continue
            # Found the assignment — validate and extract.
            return _extract_string_list(node.value)

    raise ChelperParseError(
        "SOURCE_FILES not found in chelper __init__.py"
    )


def _extract_string_list(node: ast.expr) -> list[str]:
    """Validate that *node* is a list of plain string literals and return them.

    Raises ``ChelperParseError`` if *node* is not a ``ast.List`` or if any
    element is not a plain ``ast.Constant`` string.  No silent skipping —
    a non-literal element is always an error so callers see a complete list
    or a clear failure, never a quietly truncated one.
    """
    if not isinstance(node, ast.List):
        raise ChelperParseError(
            f"SOURCE_FILES is not a list literal "
            f"(got {type(node).__name__}); chelper __init__.py may be malformed"
        )

    result: list[str] = []
    for i, elt in enumerate(node.elts):
        if not isinstance(elt, ast.Constant) or not isinstance(elt.value, str):
            kind = type(elt).__name__
            raise ChelperParseError(
                f"SOURCE_FILES[{i}] is not a plain string literal "
                f"(got AST node {kind}); only string constants are allowed"
            )
        result.append(elt.value)

    return result


# ──────────────────────────────────────────────────────────────────────────────
# A4: MIPS host build command builders
# ──────────────────────────────────────────────────────────────────────────────

# Canonical toolchain-relative paths for the MIPS cross-compiler.
# These constants encode the naming convention from crosstool-ng's MIPS build.
_CROSS_TRIPLE = "mipsel-buildroot-linux-gnu"
_GCC_STEM = f"{_CROSS_TRIPLE}-gcc"
_CROSS_PREFIX_STEM = f"{_CROSS_TRIPLE}-"  # Klipper Makefile appends "gcc", "ar", etc.
_SYSROOT_REL = Path(_CROSS_TRIPLE) / "sysroot"


def _gcc(toolchain_root: Path) -> str:
    """Absolute path to the MIPS gcc binary."""
    return str(toolchain_root / "bin" / _GCC_STEM)


def _sysroot(toolchain_root: Path) -> str:
    """Absolute path to the MIPS sysroot."""
    return str(toolchain_root / _SYSROOT_REL)


def _cross_prefix(toolchain_root: Path) -> str:
    """CROSS_PREFIX value (prefix for all cross-tool binaries in Klipper's Makefile)."""
    return str(toolchain_root / "bin" / _CROSS_PREFIX_STEM)


def c_helper_steps(
    repo_root: Path,
    source_date_epoch: int,
    *,
    toolchain_root: Path,
) -> list[BuildStep]:
    """Return the single BuildStep that compiles c_helper.so.

    Mirrors ``klipper/c_helper/build-chelper.sh``:
      $CC --sysroot=$SYSROOT -shared -fPIC -O2 -Wall \\
          -mips32r2 -mabi=32 -mhard-float -mfp64 -mnan=2008 -Wa,-mnan=2008 \\
          -o <chelper_dir>/c_helper.so <abs source paths…>

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.
    source_date_epoch:
        Unix timestamp injected as SOURCE_DATE_EPOCH (for reproducibility).
        Injected via ``["env", "SOURCE_DATE_EPOCH=<epoch>", gcc, …]`` — the
        standard way to set env in a pure command list for a direct gcc call.
    toolchain_root:
        Root of the crosstool-ng toolchain installation.  gcc and sysroot are
        derived from it; the caller (A5a / build.py) passes
        ``Path(os.environ["CROSS_TOOLCHAIN"])``.

    Returns
    -------
    list[BuildStep]
        One-element list (for API symmetry with klipper_mcu_steps).
    """
    repo_root = Path(repo_root)
    toolchain_root = Path(toolchain_root)

    chelper_dir = repo_root / "external" / "klipper" / "klippy" / "chelper"
    sources = [
        str(chelper_dir / f)
        for f in chelper_sources(chelper_dir / "__init__.py")
    ]
    output = chelper_dir / "c_helper.so"

    # Determinism: prefix-map flags strip the repo root from embedded paths.
    prefix_map = f"{repo_root}/=/"
    ffile = f"-ffile-prefix-map={prefix_map}"
    fdebug = f"-fdebug-prefix-map={prefix_map}"

    # SOURCE_DATE_EPOCH injected via env(1) in the cmd list — cleaner than
    # mutating the runner's environment and keeps the BuildStep fully self-contained.
    cmd: list[str] = [
        "env",
        f"SOURCE_DATE_EPOCH={source_date_epoch}",
        _gcc(toolchain_root),
        f"--sysroot={_sysroot(toolchain_root)}",
        "-shared",
        "-fPIC",
        "-O2",
        "-Wall",
        "-mips32r2",
        "-mabi=32",
        "-mhard-float",
        "-mfp64",
        "-mnan=2008",
        "-Wa,-mnan=2008",
        ffile,
        fdebug,
        "-o",
        str(output),
        *sources,
    ]

    step = BuildStep(
        name="c-helper-build",
        cmd=cmd,
        output_path=output,
        kind=ArtifactKind.SHARED_LIBRARY,
    )
    return [step]


def klipper_mcu_steps(
    repo_root: Path,
    source_date_epoch: int,
    *,
    toolchain_root: Path,
) -> list[BuildStep]:
    """Return the 3-step clean/olddefconfig/build triplet for klipper_mcu.elf.

    Mirrors ``klipper/klipper_host_mcu/build-klipper-host-mcu.sh``:
      cd external/klipper
      make clean        KCONFIG_CONFIG=<repo>/klipper/klipper_host_mcu/klipper-host-mcu.config
      make olddefconfig KCONFIG_CONFIG=<...>
      make -j$(nproc)   KCONFIG_CONFIG=<...>

    CROSS_PREFIX is set so Klipper's Makefile uses the MIPS cross-compiler.
    The clean step is first — a clean tree prevents Klipper's buildcommands.py
    from embedding a live strftime/hostname timestamp (determinism precondition).

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.
    source_date_epoch:
        Unix timestamp for SOURCE_DATE_EPOCH.
    toolchain_root:
        Root of the crosstool-ng installation; CROSS_PREFIX is derived from it.

    Returns
    -------
    list[BuildStep]
        [clean, olddefconfig, build]  — 3 steps total.
    """
    repo_root = Path(repo_root)
    toolchain_root = Path(toolchain_root)

    kconfig_path = repo_root / "klipper" / "klipper_host_mcu" / "klipper-host-mcu.config"
    subproject_dir = repo_root / "external" / "klipper"
    output_elf = subproject_dir / "out" / "klipper.elf"

    cross_prefix_arg = f"CROSS_PREFIX={_cross_prefix(toolchain_root)}"

    return make_steps(
        name_prefix="klipper-mcu",
        subproject_dir=subproject_dir,
        kconfig_path=kconfig_path,
        output_path=output_elf,
        kind=ArtifactKind.EXECUTABLE,
        repo_root=repo_root,
        epoch=source_date_epoch,
        extra_vars=[cross_prefix_arg],
    )


def host_steps(
    repo_root: Path,
    source_date_epoch: int,
    *,
    toolchain_root: Path,
) -> list[BuildStep]:
    """Return all MIPS host BuildSteps: c_helper_steps (1) + klipper_mcu_steps (3) = 4 total.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.
    source_date_epoch:
        Unix timestamp for SOURCE_DATE_EPOCH.
    toolchain_root:
        Root of the crosstool-ng installation.

    Returns
    -------
    list[BuildStep]
        4 steps: [c-helper-build, klipper-mcu-clean, klipper-mcu-olddefconfig, klipper-mcu-build].
    """
    repo_root = Path(repo_root)
    toolchain_root = Path(toolchain_root)
    return (
        c_helper_steps(repo_root, source_date_epoch, toolchain_root=toolchain_root)
        + klipper_mcu_steps(repo_root, source_date_epoch, toolchain_root=toolchain_root)
    )
