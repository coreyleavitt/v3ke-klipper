"""Orchestration entry-point — assembles and runs all build steps.

This module is the single Python authority that produces all four device
artifacts (katapult.bin, klipper.bin, c_helper.so, klipper_mcu.elf) in one
sequenced, runner-injected pass.  It is callable from unit tests (via FakeRunner)
and from the container entrypoint (via subprocess_runner).

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
Both the ARM klipper build (→ klipper.bin) and the host klipper_mcu build
(→ klipper.elf) invoke ``make`` inside ``external/klipper/``.  The host build
opens with a ``make clean`` that would wipe the ARM klipper.bin from the
previous step.

The fix mirrors the original bash script: after the ARM klipper build completes,
a **capture step** (``cp``) copies ``klipper.bin`` from ``external/klipper/out/``
to ``mcu-firmware/`` before the host ``make clean`` runs.  This preserves the
artifact and is the correct sequencing:

  katapult_steps (3)        → external/katapult/out/katapult.bin
  klipper_steps (3)         → external/klipper/out/klipper.bin
  klipper-capture (1, cp)   → mcu-firmware/klipper.bin   [before host clean]
  host_steps (4)            → c_helper.so + klipper.elf
  ─────────────────────────────────────────────────────────────────────────
  Total: 11 steps, 4 final artifacts.

The klipper.dict file (Klipper's data-protocol dictionary, emitted alongside
klipper.bin) is not explicitly captured here because mcu-firmware/ already
tracks it and it is overwritten by each ARM klipper build; the manifest's
artifact list handles it separately in C2.  A future slice may add an
explicit dict-capture step.

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

def _klipper_capture_step(repo_root: Path) -> BuildStep:
    """Return a BuildStep that copies klipper.bin from the ARM build output to mcu-firmware/.

    This step sits between the ARM klipper build and the host klipper_mcu
    clean/build cycle.  The host klipper_mcu build's ``make clean`` wipes
    ``external/klipper/out/``, which would destroy klipper.bin if it were not
    captured first.

    The capture destination (mcu-firmware/) is the canonical location used by
    the bash script and tracked in the repository as the official build output.
    """
    src = repo_root / "external" / "klipper" / "out" / "klipper.bin"
    dst_dir = repo_root / "mcu-firmware"
    dst = dst_dir / "klipper.bin"

    return BuildStep(
        name="klipper-capture",
        cmd=["cp", str(src), str(dst_dir)],
        output_path=dst,
        kind=ArtifactKind.RAW_FIRMWARE,   # binary firmware — no ELF ABI check
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
        StepResults for all steps executed (11 total: 6 ARM + 1 capture + 4 host).
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
    #   1. ARM katapult steps  — external/katapult/ (no conflict)
    #   2. ARM klipper steps   — external/klipper/ → klipper.bin
    #   3. Capture step        — cp klipper.bin → mcu-firmware/ BEFORE host clean
    #   4. Host steps          — c_helper_steps + klipper_mcu_steps
    #      c_helper first (no make clean), then klipper_mcu (clean/olddefconfig/build)
    #      The klipper_mcu clean wipes external/klipper/out/ — safe because step 3 already
    #      preserved klipper.bin in mcu-firmware/.
    steps: list[BuildStep] = (
        arm_mcu_steps(repo_root, resolved_epoch)
        + [_klipper_capture_step(repo_root)]
        + host_steps(repo_root, resolved_epoch, toolchain_root=toolchain_root)
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
