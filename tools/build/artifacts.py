"""Artifact runner seam — executes BuildStep sequences via an injectable runner.

Public interface
────────────────
  BuildStep(name, cmd, output_path, kind)
      Frozen dataclass: the pure handoff from builders to the runner.
      Carries the command list, the expected output path, and ArtifactKind so
      the runner knows whether to call check_abi.

  RunResult(returncode, stdout, stderr, elapsed)
      Thin result seam: "ran a command, here's exit/output/timing."

  StepResult(name, ok, duration, abi, detail)
      One completed step's outcome.  abi is None for RAW_FIRMWARE or failed
      steps; detail is a human-readable summary string.

  subprocess_runner(cmd) -> RunResult
      Default runner implementation: wraps subprocess.run.

  FakeRunner
      Test helper: returns RunResult(0, b"", b"", 0.0) unless configured to
      fail at a specific step index.

  run_steps(steps, runner, *, repo_root) -> list[StepResult]
      Execute steps in order via runner.  Fail-fast: raises RuntimeError on
      non-zero returncode; the returned list includes all steps up to and
      including the failure.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from abi.abi_spec import ArtifactKind
from build.elf import AbiResult

__all__ = [
    "BuildStep",
    "RunResult",
    "StepResult",
    "subprocess_runner",
    "FakeRunner",
    "run_steps",
]


# ──────────────────────────────────────────────────────────────────────────────
# Core types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BuildStep:
    """Pure description of one build command.

    Fields
    ------
    name        : Human-readable label (e.g. "katapult-clean").
    cmd         : The command + arguments to pass to the runner.
    output_path : Expected output artifact; None for side-effect-only steps
                  that produce no single output file (e.g. clean).
    kind        : ArtifactKind for the output.  RAW_FIRMWARE skips check_abi.
    """
    name:        str
    cmd:         list[str]
    output_path: Optional[Path]
    kind:        ArtifactKind


@dataclass(frozen=True)
class RunResult:
    """Thin result from running one command."""
    returncode: int
    stdout:     bytes
    stderr:     bytes
    elapsed:    float


@dataclass(frozen=True)
class StepResult:
    """Outcome of one executed BuildStep.

    abi is None when:
    - the step failed (ok=False), OR
    - kind is RAW_FIRMWARE (ABI check not applicable), OR
    - output_path is None (no declared artifact).
    """
    name:     str
    ok:       bool
    duration: float
    abi:      Optional[AbiResult]
    detail:   str


# ──────────────────────────────────────────────────────────────────────────────
# Runner implementations
# ──────────────────────────────────────────────────────────────────────────────

def subprocess_runner(cmd: list[str]) -> RunResult:
    """Default runner: execute *cmd* via subprocess and return a RunResult."""
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True)
    elapsed = time.monotonic() - t0
    return RunResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        elapsed=elapsed,
    )


class FakeRunner:
    """Test helper that returns success for every step unless told otherwise.

    Parameters
    ----------
    fail_at : int, optional
        Zero-based index of the step that should return returncode=1.
        All other steps succeed (returncode=0, empty output, zero elapsed).
    """

    def __init__(self, *, fail_at: Optional[int] = None) -> None:
        self._fail_at = fail_at
        self._call_count = 0

    def __call__(self, cmd: list[str]) -> RunResult:
        idx = self._call_count
        self._call_count += 1
        rc = 1 if idx == self._fail_at else 0
        return RunResult(returncode=rc, stdout=b"", stderr=b"", elapsed=0.0)


# ──────────────────────────────────────────────────────────────────────────────
# run_steps
# ──────────────────────────────────────────────────────────────────────────────

def run_steps(
    steps: list[BuildStep],
    runner: Callable[[list[str]], RunResult],
    *,
    repo_root: Optional[Path] = None,
) -> list[StepResult]:
    """Execute *steps* in order via *runner*.

    Returns a list of StepResult for all steps executed, including the failing
    step if one occurs.

    Fail-fast: raises RuntimeError on the first non-zero returncode.  The
    partial list of results (up to and including the failure) is attached to
    the exception as ``exc.results``.

    The *repo_root* parameter is accepted for symmetry with callers that may
    want to pass it through, but is not used here (output_path on each
    BuildStep is already absolute or resolved by the builder).
    """
    results: list[StepResult] = []

    for step in steps:
        result_val = runner(step.cmd)
        ok = result_val.returncode == 0
        detail = f"exit {result_val.returncode}" if not ok else f"ok ({result_val.elapsed:.3f}s)"

        abi: Optional[AbiResult] = None
        if ok and step.output_path is not None:
            # Check that the declared output file actually exists after the step.
            if not step.output_path.exists():
                ok = False
                detail = f"output_path missing after step: {step.output_path}"
            elif step.kind is not ArtifactKind.RAW_FIRMWARE:
                # ELF-bearing steps: parse the artifact and check ABI.
                from build.elf import check_abi, inspect_elf, MalformedElfError
                data = step.output_path.read_bytes()
                try:
                    info = inspect_elf(data)
                    abi = check_abi(info, step.kind)
                except MalformedElfError as exc_elf:
                    ok = False
                    detail = f"malformed ELF in output_path {step.output_path}: {exc_elf}"

        sr = StepResult(
            name=step.name,
            ok=ok,
            duration=result_val.elapsed,
            abi=abi,
            detail=detail,
        )
        results.append(sr)

        if not ok:
            exc = RuntimeError(
                f"Step '{step.name}' failed: {detail}"
            )
            exc.results = results  # type: ignore[attr-defined]
            raise exc

    return results
