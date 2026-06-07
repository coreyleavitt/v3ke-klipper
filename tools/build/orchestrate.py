"""Orchestration entry-point — assembles and runs all build steps.

This module is the single Python authority that produces all five device
artifacts (katapult.bin, klipper.bin, klipper.dict, c_helper.so, klipper_mcu.elf)
in one sequenced, runner-injected pass.  It is callable from unit tests (via
FakeRunner) and from the container entrypoint (via subprocess_runner).

Public interface
────────────────
  build_all_artifacts(
      repo_root, toolchain_root, *,
      runner=subprocess_runner,
      epoch=None,
      _resolve_epoch=resolve_source_date_epoch,
  ) -> list[StepResult]

      Orchestrate all build steps in canonical order.  If *epoch* is None
      (the default), SOURCE_DATE_EPOCH is resolved by calling *_resolve_epoch(repo_root)*
      (defaults to ``resolve_source_date_epoch`` from ``build.arm_mcu``).
      The *_resolve_epoch* parameter exists solely for unit-test injection;
      callers should rely on the default.

      Returns the list of StepResult for all steps executed.  Fail-fast:
      raises RuntimeError (with exc.results attached) on the first non-zero step.

Step ordering and the shared-tree sequencing hazard
───────────────────────────────────────────────────
Both the ARM klipper build (→ klipper.bin + klipper.dict) and the host klipper_mcu
build (→ klipper_mcu.elf) invoke ``make`` inside ``external/klipper/``.  The host
build opens with a ``make clean`` that would wipe everything produced by the ARM
build from ``external/klipper/out/``.

Three capture steps (all ``cp``) run between the ARM build and the host clean:

  katapult_steps (3)             → external/katapult/out/katapult.bin
  klipper_steps (3)              → external/klipper/out/{klipper.bin,klipper.dict}
  klipper-capture (1, cp)        → mcu-firmware/klipper.bin      [before host clean]
  klipper-dict-capture (1, cp)   → mcu-firmware/klipper.dict     [before host clean]
  host_steps (4)                 → c_helper.so + out/klipper.elf
  klipper-elf-capture (1, cp)    → mcu-firmware/klipper_mcu.elf  [canonical MIPS elf]
  ─────────────────────────────────────────────────────────────────────────────────
  Total: 13 steps, 5 final artifacts.

mcu-firmware/ is the canonical, durable location for all release artifacts.
external/klipper/out/ is transient and must never be used at packaging time.

Container entrypoint
────────────────────
Run from inside the v3ke-toolchain container (repo mounted at /work):

  python -m build.orchestrate

This invokes ``_main()`` which resolves CROSS_TOOLCHAIN from the environment,
calls build_all_artifacts with subprocess_runner, prints per-step results to
stdout, and exits non-zero on any failure or ABI violation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, Optional

from build.arm_mcu import arm_mcu_steps, resolve_source_date_epoch
from build.host import host_steps
from build.artifacts import (
    BuildStep,
    StepResult,
    subprocess_runner,
    run_steps,
    RunResult,
)
from abi.abi_spec import ArtifactKind

__all__ = ["build_all_artifacts"]


# ──────────────────────────────────────────────────────────────────────────────
# Capture step builder
# ──────────────────────────────────────────────────────────────────────────────

def _klipper_capture_step(
    repo_root: Path,
    *,
    src_name: str,
    dst_name: str,
    step_name: str,
) -> BuildStep:
    """Return a BuildStep that copies a Klipper build artifact to mcu-firmware/.

    All three capture steps (klipper.bin, klipper.dict, klipper.elf→klipper_mcu.elf)
    share this implementation.  Using the full *dst* file path — not just the parent
    directory — as the ``cp`` destination is what guarantees the file lands at
    ``output_path`` even when *dst_name* differs from *src_name* (the elf rename).

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.
    src_name:
        Filename inside ``external/klipper/out/`` (e.g. ``"klipper.elf"``).
    dst_name:
        Filename to create inside ``mcu-firmware/`` (e.g. ``"klipper_mcu.elf"``).
        May differ from *src_name* to rename on capture.
    step_name:
        Human-readable step label (e.g. ``"klipper-elf-capture"``).

    Returns
    -------
    BuildStep
        A ``cp src dst`` step whose ``output_path`` is the full destination file path.
        Using the full file path (not the directory) is critical: ``cp src DIR/``
        names the result after the source, silently defeating any rename.
    """
    src = repo_root / "external" / "klipper" / "out" / src_name
    dst = repo_root / "mcu-firmware" / dst_name

    return BuildStep(
        name=step_name,
        cmd=["cp", str(src), str(dst)],
        output_path=dst,
        kind=ArtifactKind.RAW_FIRMWARE,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Main orchestration function
# ──────────────────────────────────────────────────────────────────────────────

def build_all_artifacts(
    repo_root: Path,
    toolchain_root: Path,
    *,
    runner: Callable[[list[str]], RunResult] = subprocess_runner,
    epoch: Optional[int] = None,
    _resolve_epoch: Callable[[Path], int] = resolve_source_date_epoch,
) -> list[StepResult]:
    """Assemble and execute all build steps for the four device artifacts.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root (inside the container, typically /work).
    toolchain_root:
        Absolute path to the crosstool-ng toolchain installation.  The caller
        passes ``Path(os.environ["CROSS_TOOLCHAIN"])`` when running inside the
        container.
    runner:
        Command runner.  Defaults to ``subprocess_runner`` (real execution).
        Pass ``FakeRunner()`` for unit tests.
    epoch:
        SOURCE_DATE_EPOCH value.  When None (default), resolved by calling
        ``_resolve_epoch(repo_root)`` — which defaults to ``resolve_source_date_epoch``,
        the git-log-based resolver from ``build.arm_mcu``.
    _resolve_epoch:
        Injectable resolver for the epoch.  Only override in unit tests;
        production code relies on the default.

    Returns
    -------
    list[StepResult]
        StepResults for all steps executed (13 total: 6 ARM + 3 captures + 4 host).
        Fail-fast: raises RuntimeError with exc.results on the first non-zero step.
    """
    repo_root = Path(repo_root)
    toolchain_root = Path(toolchain_root)

    # Resolve epoch once — the same value threads through all make/gcc invocations
    # so every artifact in a single build shares an identical SOURCE_DATE_EPOCH.
    resolved_epoch: int = epoch if epoch is not None else _resolve_epoch(repo_root)

    # Assemble the full step list in canonical order.
    #
    # Sequencing rationale (shared external/klipper/ build tree):
    #   1. ARM katapult steps     — external/katapult/ (no conflict)
    #   2. ARM klipper steps      — external/klipper/ → klipper.bin + klipper.dict
    #   3. klipper-capture        — cp klipper.bin → mcu-firmware/ BEFORE host clean
    #   4. klipper-dict-capture   — cp klipper.dict → mcu-firmware/ BEFORE host clean
    #   5. Host steps             — c_helper_steps + klipper_mcu_steps
    #      c_helper first (no make clean), then klipper_mcu (clean/olddefconfig/build)
    #      The klipper_mcu clean wipes external/klipper/out/ — safe because steps 3+4
    #      already preserved klipper.bin and klipper.dict in mcu-firmware/.
    #   6. klipper-elf-capture    — cp klipper.elf → mcu-firmware/klipper_mcu.elf
    #      Captures the MIPS host-MCU ELF after the host build completes.
    steps: list[BuildStep] = (
        arm_mcu_steps(repo_root, resolved_epoch)
        + [
            _klipper_capture_step(repo_root, src_name="klipper.bin",  dst_name="klipper.bin",     step_name="klipper-capture"),
            _klipper_capture_step(repo_root, src_name="klipper.dict", dst_name="klipper.dict",    step_name="klipper-dict-capture"),
        ]
        + host_steps(repo_root, resolved_epoch, toolchain_root=toolchain_root)
        + [
            _klipper_capture_step(repo_root, src_name="klipper.elf",  dst_name="klipper_mcu.elf", step_name="klipper-elf-capture"),
        ]
    )

    return run_steps(steps, runner, repo_root=repo_root)


# ──────────────────────────────────────────────────────────────────────────────
# Container entrypoint
# ──────────────────────────────────────────────────────────────────────────────

def _main() -> None:
    """Container entrypoint: build all artifacts and report per-step results.

    Reads CROSS_TOOLCHAIN from the environment (set by the v3ke-toolchain image).
    Exits non-zero if any step fails or any ELF artifact fails the ABI check.
    """
    cross_toolchain = os.environ.get("CROSS_TOOLCHAIN")
    if not cross_toolchain:
        print("ERROR: CROSS_TOOLCHAIN environment variable is not set.", file=sys.stderr)
        print(
            "  Inside the v3ke-toolchain image this is set automatically.",
            file=sys.stderr,
        )
        sys.exit(1)

    repo_root = Path("/work")
    toolchain_root = Path(cross_toolchain)

    print(f"=== build_all_artifacts ===")
    print(f"  repo_root:      {repo_root}")
    print(f"  toolchain_root: {toolchain_root}")
    print()

    exit_code = 0
    try:
        results = build_all_artifacts(
            repo_root=repo_root,
            toolchain_root=toolchain_root,
        )
    except RuntimeError as exc:
        results = getattr(exc, "results", [])
        # The failure is included in results — fall through to the report.
        exit_code = 1

    # Per-step report
    abi_failures: list[StepResult] = []
    for sr in results:
        status = "OK" if sr.ok else "FAIL"
        print(f"  [{status}] {sr.name} ({sr.detail})")
        if sr.abi is not None and not sr.abi.ok:
            abi_failures.append(sr)
            from build.elf import AbiViolation, LoaderViolation
            for v in sr.abi.violations:
                if isinstance(v, AbiViolation):
                    print(
                        f"         ABI violation: {v.field} expected=0x{v.expected:x} actual=0x{v.actual:x}"
                    )
                elif isinstance(v, LoaderViolation):
                    print(
                        f"         ABI violation: loader expected suffix={v.expected_suffix!r} actual={v.actual!r}"
                    )

    print()
    if abi_failures:
        print(f"ABI VERIFICATION FAILED — {len(abi_failures)} step(s) with violations")
        exit_code = 1
    elif exit_code == 0:
        print("ABI VERIFICATION PASSED")

    sys.exit(exit_code)


if __name__ == "__main__":
    _main()
