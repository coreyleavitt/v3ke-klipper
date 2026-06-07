"""ARM MCU firmware builders — pure command construction (no I/O).

Produces the sequence of BuildStep descriptors that replicate the ARM half of
``build-bootloader-mcu-and-host-firmware.sh``:

  katapult_steps(repo_root, source_date_epoch) -> list[BuildStep]
      Three steps: clean / olddefconfig / build.
      KCONFIG_CONFIG = <repo_root>/mcu-firmware/katapult.config
      Working directory = <repo_root>/external/katapult (passed in cmd env via
      make's directory argument — make is run with -C so no chdir in Python).
      Output: <repo_root>/external/katapult/out/katapult.bin  (RAW_FIRMWARE)

  klipper_steps(repo_root, source_date_epoch) -> list[BuildStep]
      Three steps: clean / olddefconfig / build.
      KCONFIG_CONFIG = <repo_root>/mcu-firmware/klipper.config
      Output: <repo_root>/external/klipper/out/klipper.bin    (RAW_FIRMWARE)

  arm_mcu_steps(repo_root, source_date_epoch) -> list[BuildStep]
      Concatenation: katapult_steps + klipper_steps.

  resolve_source_date_epoch(repo_root) -> int
      Shell out to ``git -C <repo_root> log -1 --format=%ct HEAD`` and return
      the integer commit timestamp.  Injectable as a parameter so unit tests
      can pass a fixed integer without git.

Determinism flags
─────────────────
Every compilation step (olddefconfig + build) sets:
  - SOURCE_DATE_EPOCH=<epoch>  in the make environment override
  - CFLAGS_EXTRA=-ffile-prefix-map=<repo_root>/=/ -fdebug-prefix-map=<repo_root>/=/

The clean step does not need determinism flags (it deletes artefacts, not builds them).

make invocation
───────────────
Each step's cmd is a full make command list including:
  make [-j<nproc>] KCONFIG_CONFIG=<path> SOURCE_DATE_EPOCH=<epoch> [CFLAGS_EXTRA=…]
run in the subproject directory via ``make -C <dir>``.  This mirrors the
subshell ``cd "$DIR"; make …`` pattern in the shell script while keeping each
BuildStep's cmd fully self-contained (no need for the caller to chdir).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from abi.abi_spec import ArtifactKind
from build.artifacts import BuildStep
from build._makesteps import make_steps

__all__ = [
    "katapult_steps",
    "klipper_steps",
    "arm_mcu_steps",
    "resolve_source_date_epoch",
]

# ──────────────────────────────────────────────────────────────────────────────
# SOURCE_DATE_EPOCH resolution
# ──────────────────────────────────────────────────────────────────────────────

def resolve_source_date_epoch(repo_root: Path) -> int:
    """Return the git HEAD commit timestamp for *repo_root* as a Unix integer.

    Raises RuntimeError if git is unavailable or the repo has no commits.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_root), "log", "-1", "--format=%ct", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    ts = result.stdout.strip()
    if not ts:
        raise RuntimeError(
            f"git log returned empty timestamp for repo {repo_root!r} — no commits?"
        )
    return int(ts)


# ──────────────────────────────────────────────────────────────────────────────
# Public builders
# ──────────────────────────────────────────────────────────────────────────────

def katapult_steps(repo_root: Path, source_date_epoch: int) -> list[BuildStep]:
    """Return the BuildStep sequence for the katapult bootloader firmware.

    Mirrors the katapult subshell in build-bootloader-mcu-and-host-firmware.sh:
      cd external/katapult
      make clean        KCONFIG_CONFIG=<repo>/mcu-firmware/katapult.config
      make olddefconfig KCONFIG_CONFIG=<repo>/mcu-firmware/katapult.config
      make -j$(nproc)   KCONFIG_CONFIG=<repo>/mcu-firmware/katapult.config

    Output artifact: external/katapult/out/katapult.bin (RAW_FIRMWARE).
    """
    repo_root = Path(repo_root)
    return make_steps(
        name_prefix="katapult",
        subproject_dir=repo_root / "external" / "katapult",
        kconfig_path=repo_root / "mcu-firmware" / "katapult.config",
        output_path=repo_root / "external" / "katapult" / "out" / "katapult.bin",
        kind=ArtifactKind.RAW_FIRMWARE,
        repo_root=repo_root,
        epoch=source_date_epoch,
    )


def klipper_steps(repo_root: Path, source_date_epoch: int) -> list[BuildStep]:
    """Return the BuildStep sequence for the Klipper MCU firmware.

    Mirrors the klipper subshell in build-bootloader-mcu-and-host-firmware.sh:
      cd external/klipper
      make clean        KCONFIG_CONFIG=<repo>/mcu-firmware/klipper.config
      make olddefconfig KCONFIG_CONFIG=<repo>/mcu-firmware/klipper.config
      make -j$(nproc)   KCONFIG_CONFIG=<repo>/mcu-firmware/klipper.config

    Output artifact: external/klipper/out/klipper.bin (RAW_FIRMWARE).
    Note: klipper.dict is also emitted to external/klipper/out/ by the build
    but is captured separately (it is not a binary firmware blob).
    """
    repo_root = Path(repo_root)
    return make_steps(
        name_prefix="klipper",
        subproject_dir=repo_root / "external" / "klipper",
        kconfig_path=repo_root / "mcu-firmware" / "klipper.config",
        output_path=repo_root / "external" / "klipper" / "out" / "klipper.bin",
        kind=ArtifactKind.RAW_FIRMWARE,
        repo_root=repo_root,
        epoch=source_date_epoch,
    )


def arm_mcu_steps(repo_root: Path, source_date_epoch: int) -> list[BuildStep]:
    """Return all ARM MCU BuildSteps: katapult (3) + klipper (3) = 6 steps total."""
    repo_root = Path(repo_root)
    return katapult_steps(repo_root, source_date_epoch) + klipper_steps(repo_root, source_date_epoch)
