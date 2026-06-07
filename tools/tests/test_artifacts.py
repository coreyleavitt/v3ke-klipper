"""A3 unit tests — ARM MCU builder + artifact runner seam.

Slice A3: pure Python, FakeRunner, no real make, no toolchain image.

Test structure
──────────────
A3-1: katapult/klipper kconfig paths referenced by steps exist on disk.
A3-2: Determinism flags (SOURCE_DATE_EPOCH, -ffile-prefix-map, -fdebug-prefix-map)
      are present in every step where compilation occurs (olddefconfig + build).
A3-3: check_abi is NOT invoked for RAW_FIRMWARE steps (StepResult.abi is None).
A3-4: Fail-fast — non-zero at step k raises, results list includes steps 1..k.
"""

from __future__ import annotations

import pathlib
from pathlib import Path
from typing import Optional

import pytest

from build.arm_mcu import arm_mcu_steps, katapult_steps, klipper_steps
from build.artifacts import (
    BuildStep,
    FakeRunner,
    RunResult,
    StepResult,
    run_steps,
)
from abi.abi_spec import ArtifactKind

# ──────────────────────────────────────────────────────────────────────────────
# Repo root fixture
# ──────────────────────────────────────────────────────────────────────────────

# klipper-mainline/ is three levels above tools/tests/
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

# A fixed SOURCE_DATE_EPOCH value for unit tests — no git required.
_EPOCH = 1_700_000_000


# ──────────────────────────────────────────────────────────────────────────────
# A3-1: kconfig paths exist on disk
# ──────────────────────────────────────────────────────────────────────────────

class TestKconfigPathsExist:
    """The kconfig path each build step references must exist on disk.

    This is the primary early-warning detector for wrong/renamed paths —
    a path typo in the builder produces an immediate, human-readable failure
    rather than a silent success followed by a confusing make error.
    """

    def _kconfig_args(self, steps: list[BuildStep]) -> list[str]:
        """Extract all KCONFIG_CONFIG=... values from a step list."""
        values = []
        for step in steps:
            for arg in step.cmd:
                if arg.startswith("KCONFIG_CONFIG="):
                    values.append(arg[len("KCONFIG_CONFIG="):])
        return values

    def test_katapult_kconfig_exists(self):
        steps = katapult_steps(_REPO_ROOT, _EPOCH)
        kconfig_values = self._kconfig_args(steps)
        assert kconfig_values, "No KCONFIG_CONFIG= found in katapult steps"
        # All steps that carry KCONFIG_CONFIG should reference the same file.
        unique = set(kconfig_values)
        assert len(unique) == 1, f"Expected one unique KCONFIG_CONFIG, got: {unique}"
        kconfig_path = Path(next(iter(unique)))
        assert kconfig_path.exists(), (
            f"katapult KCONFIG_CONFIG path does not exist: {kconfig_path}"
        )

    def test_klipper_kconfig_exists(self):
        steps = klipper_steps(_REPO_ROOT, _EPOCH)
        kconfig_values = self._kconfig_args(steps)
        assert kconfig_values, "No KCONFIG_CONFIG= found in klipper steps"
        unique = set(kconfig_values)
        assert len(unique) == 1, f"Expected one unique KCONFIG_CONFIG, got: {unique}"
        kconfig_path = Path(next(iter(unique)))
        assert kconfig_path.exists(), (
            f"klipper KCONFIG_CONFIG path does not exist: {kconfig_path}"
        )

    def test_katapult_kconfig_is_under_mcu_firmware(self):
        """KCONFIG_CONFIG must point into mcu-firmware/, not into external/."""
        steps = katapult_steps(_REPO_ROOT, _EPOCH)
        for step in steps:
            for arg in step.cmd:
                if arg.startswith("KCONFIG_CONFIG="):
                    p = Path(arg[len("KCONFIG_CONFIG="):])
                    assert "mcu-firmware" in p.parts, (
                        f"katapult KCONFIG_CONFIG should be under mcu-firmware/, got {p}"
                    )

    def test_klipper_kconfig_is_under_mcu_firmware(self):
        """KCONFIG_CONFIG must point into mcu-firmware/, not into external/."""
        steps = klipper_steps(_REPO_ROOT, _EPOCH)
        for step in steps:
            for arg in step.cmd:
                if arg.startswith("KCONFIG_CONFIG="):
                    p = Path(arg[len("KCONFIG_CONFIG="):])
                    assert "mcu-firmware" in p.parts, (
                        f"klipper KCONFIG_CONFIG should be under mcu-firmware/, got {p}"
                    )

    def test_katapult_subproject_dir_exists(self):
        """The make -C<dir> argument must point to an existing directory."""
        steps = katapult_steps(_REPO_ROOT, _EPOCH)
        for step in steps:
            for arg in step.cmd:
                if arg.startswith("-C"):
                    d = Path(arg[2:])
                    assert d.is_dir(), f"katapult make -C dir not found: {d}"

    def test_klipper_subproject_dir_exists(self):
        steps = klipper_steps(_REPO_ROOT, _EPOCH)
        for step in steps:
            for arg in step.cmd:
                if arg.startswith("-C"):
                    d = Path(arg[2:])
                    assert d.is_dir(), f"klipper make -C dir not found: {d}"

    def test_arm_mcu_steps_count(self):
        """arm_mcu_steps = 3 katapult + 3 klipper = 6 total."""
        steps = arm_mcu_steps(_REPO_ROOT, _EPOCH)
        assert len(steps) == 6

    def test_katapult_output_bin_path(self):
        """katapult build step output_path should be under external/katapult/out/."""
        steps = katapult_steps(_REPO_ROOT, _EPOCH)
        build_step = steps[-1]  # build is the last of the three
        assert build_step.output_path is not None, "build step must have an output_path"
        assert "katapult" in build_step.output_path.name
        assert build_step.output_path.suffix == ".bin"
        assert "out" in build_step.output_path.parts

    def test_klipper_output_bin_path(self):
        """klipper build step output_path should be under external/klipper/out/."""
        steps = klipper_steps(_REPO_ROOT, _EPOCH)
        build_step = steps[-1]
        assert build_step.output_path is not None, "build step must have an output_path"
        assert "klipper" in build_step.output_path.name
        assert build_step.output_path.suffix == ".bin"
        assert "out" in build_step.output_path.parts


# ──────────────────────────────────────────────────────────────────────────────
# A3-2: Determinism flags are present on compilation steps
# ──────────────────────────────────────────────────────────────────────────────

class TestDeterminismFlags:
    """Steps that invoke compilation must carry SOURCE_DATE_EPOCH and prefix-map flags.

    The clean step is excluded — it deletes artifacts, not builds them.
    olddefconfig + build are the compilation steps.
    """

    def _compilation_steps(self, steps: list[BuildStep]) -> list[BuildStep]:
        """Return steps that are not clean (i.e., olddefconfig and build)."""
        return [s for s in steps if not s.name.endswith("-clean")]

    def _assert_determinism(self, step: BuildStep, epoch: int) -> None:
        cmd_str = " ".join(step.cmd)

        # SOURCE_DATE_EPOCH must be present
        assert f"SOURCE_DATE_EPOCH={epoch}" in step.cmd, (
            f"Step '{step.name}' missing SOURCE_DATE_EPOCH={epoch} in cmd: {step.cmd}"
        )

        # -ffile-prefix-map must be present somewhere in the command
        assert any("-ffile-prefix-map=" in arg for arg in step.cmd), (
            f"Step '{step.name}' missing -ffile-prefix-map in cmd: {step.cmd}"
        )

        # -fdebug-prefix-map must be present somewhere in the command
        assert any("-fdebug-prefix-map=" in arg for arg in step.cmd), (
            f"Step '{step.name}' missing -fdebug-prefix-map in cmd: {step.cmd}"
        )

    def test_katapult_compilation_steps_have_determinism_flags(self):
        steps = katapult_steps(_REPO_ROOT, _EPOCH)
        for step in self._compilation_steps(steps):
            self._assert_determinism(step, _EPOCH)

    def test_klipper_compilation_steps_have_determinism_flags(self):
        steps = klipper_steps(_REPO_ROOT, _EPOCH)
        for step in self._compilation_steps(steps):
            self._assert_determinism(step, _EPOCH)

    def test_epoch_value_is_reflected_in_cmd(self):
        """A different epoch value must propagate into the generated commands."""
        other_epoch = 1_600_000_000
        steps = katapult_steps(_REPO_ROOT, other_epoch)
        for step in self._compilation_steps(steps):
            assert f"SOURCE_DATE_EPOCH={other_epoch}" in step.cmd, (
                f"Step '{step.name}' did not reflect epoch={other_epoch}: {step.cmd}"
            )

    def test_clean_step_does_not_carry_cflags(self):
        """The clean step should not carry CFLAGS_EXTRA — it never compiles."""
        steps = katapult_steps(_REPO_ROOT, _EPOCH)
        clean_step = steps[0]
        assert clean_step.name.endswith("-clean")
        assert not any("CFLAGS_EXTRA" in arg for arg in clean_step.cmd), (
            f"Clean step should not have CFLAGS_EXTRA: {clean_step.cmd}"
        )

    def test_prefix_map_contains_repo_root(self):
        """The prefix-map flags must map the real repo root away (not a bogus path)."""
        steps = katapult_steps(_REPO_ROOT, _EPOCH)
        build_step = steps[-1]  # build step
        cflags_arg = next(
            (arg for arg in build_step.cmd if arg.startswith("CFLAGS_EXTRA=")), None
        )
        assert cflags_arg is not None, "build step must have CFLAGS_EXTRA"
        # The repo root path must appear so the map is meaningful
        assert str(_REPO_ROOT) in cflags_arg, (
            f"CFLAGS_EXTRA does not contain repo_root path: {cflags_arg}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# A3-3: check_abi is NOT invoked for RAW_FIRMWARE steps
# ──────────────────────────────────────────────────────────────────────────────

class TestRawFirmwareSkipsAbiCheck:
    """RAW_FIRMWARE steps must never produce a StepResult with a non-None abi.

    We use a FakeRunner so no real output files are created, and verify that
    run_steps leaves abi=None for every RAW_FIRMWARE step even when ok=True.
    This is a behavioral assertion — not testing the internals of run_steps.
    """

    def test_all_arm_steps_have_kind_raw_firmware(self):
        """All arm_mcu_steps carry ArtifactKind.RAW_FIRMWARE."""
        steps = arm_mcu_steps(_REPO_ROOT, _EPOCH)
        for step in steps:
            assert step.kind is ArtifactKind.RAW_FIRMWARE, (
                f"Step '{step.name}' has kind={step.kind!r}, expected RAW_FIRMWARE"
            )

    def test_step_results_have_none_abi_for_raw_firmware(self, tmp_path):
        """With FakeRunner, run_steps must leave abi=None for every RAW_FIRMWARE step.

        Uses dummy steps with output_path pointing to existing files so H1's
        missing-output-path check does not fire.  The RAW_FIRMWARE guard must
        prevent check_abi from being called regardless of the file contents.
        """
        # Create dummy output files for each step so the missing-file check passes.
        dummy_bin = tmp_path / "dummy.bin"
        dummy_bin.write_bytes(b"not-a-real-firmware")
        steps = [
            BuildStep(
                name=f"fw-step-{i}",
                cmd=["true"],
                output_path=dummy_bin,
                kind=ArtifactKind.RAW_FIRMWARE,
            )
            for i in range(3)
        ]
        results = run_steps(steps, FakeRunner())
        assert len(results) == len(steps)
        for sr in results:
            assert sr.abi is None, (
                f"StepResult '{sr.name}' has abi={sr.abi!r}; "
                "expected None for RAW_FIRMWARE steps"
            )

    def test_spy_check_abi_never_called_for_raw_firmware(self, tmp_path):
        """Inject a spy check_abi; confirm it is never called for RAW_FIRMWARE steps.

        We monkeypatch build.elf.check_abi with a spy that records calls.
        Even if FakeRunner returns success and the step has an output_path that
        exists, the kind=RAW_FIRMWARE guard must short-circuit before check_abi.
        """
        calls: list[tuple] = []

        def spy_check_abi(info, kind):
            calls.append((info, kind))
            from build.elf import AbiResult
            return AbiResult.not_applicable()

        # Create a dummy file so the missing-output-path check doesn't fire.
        dummy_bin = tmp_path / "dummy.bin"
        dummy_bin.write_bytes(b"firmware")
        steps = [
            BuildStep(
                name="fw-step",
                cmd=["true"],
                output_path=dummy_bin,
                kind=ArtifactKind.RAW_FIRMWARE,
            )
        ]

        import build.elf as elf_mod
        original_check_abi = elf_mod.check_abi
        elf_mod.check_abi = spy_check_abi  # type: ignore[assignment]
        try:
            run_steps(steps, FakeRunner())
        finally:
            elf_mod.check_abi = original_check_abi  # type: ignore[assignment]

        # check_abi should never have been called (RAW_FIRMWARE guard fires first)
        assert calls == [], (
            f"check_abi was called {len(calls)} time(s) for RAW_FIRMWARE steps; expected 0"
        )


# ──────────────────────────────────────────────────────────────────────────────
# A3-4: Fail-fast — non-zero at step k raises; results include steps 1..k
# ──────────────────────────────────────────────────────────────────────────────

class TestFailFast:
    """A non-zero returncode from the runner must raise RuntimeError immediately.

    The partial list of StepResults (including the failing step) is attached
    to the exception as exc.results.
    """

    def _make_dummy_steps(self, n: int) -> list[BuildStep]:
        """Produce n trivial side-effect-only RAW_FIRMWARE BuildSteps for testing.

        Uses output_path=None (not Path('')) so that H1's existence check is
        genuinely bypassed — Path('') resolves to cwd which exists, so it would
        accidentally satisfy the check rather than exercising the None-bypass.
        """
        return [
            BuildStep(
                name=f"step-{i}",
                cmd=["true"],
                output_path=None,
                kind=ArtifactKind.RAW_FIRMWARE,
            )
            for i in range(n)
        ]

    def test_fail_at_first_step_raises(self):
        steps = self._make_dummy_steps(3)
        runner = FakeRunner(fail_at=0)
        with pytest.raises(RuntimeError) as exc_info:
            run_steps(steps, runner)
        exc = exc_info.value
        assert hasattr(exc, "results"), "RuntimeError must carry exc.results"
        assert len(exc.results) == 1, (
            f"Expected 1 result (the failing step), got {len(exc.results)}"
        )
        assert exc.results[0].ok is False
        assert exc.results[0].name == "step-0"

    def test_fail_at_middle_step(self):
        steps = self._make_dummy_steps(5)
        runner = FakeRunner(fail_at=2)
        with pytest.raises(RuntimeError) as exc_info:
            run_steps(steps, runner)
        exc = exc_info.value
        results = exc.results
        assert len(results) == 3, (
            f"Expected 3 results (steps 0,1,2), got {len(results)}"
        )
        assert results[0].ok is True
        assert results[1].ok is True
        assert results[2].ok is False
        assert results[2].name == "step-2"

    def test_fail_at_last_step(self):
        steps = self._make_dummy_steps(4)
        runner = FakeRunner(fail_at=3)
        with pytest.raises(RuntimeError) as exc_info:
            run_steps(steps, runner)
        exc = exc_info.value
        results = exc.results
        assert len(results) == 4
        for sr in results[:3]:
            assert sr.ok is True
        assert results[3].ok is False

    def test_no_steps_after_failure(self):
        """Steps after the failing step must NOT be executed."""
        steps = self._make_dummy_steps(5)
        runner = FakeRunner(fail_at=1)
        with pytest.raises(RuntimeError) as exc_info:
            run_steps(steps, runner)
        results = exc_info.value.results
        # Only steps 0 and 1 run; steps 2-4 are never executed
        assert len(results) == 2
        assert runner._call_count == 2, (
            f"Runner was called {runner._call_count} times; should have stopped at step 1"
        )

    def test_all_steps_succeed_returns_full_list(self):
        """With no failures, run_steps returns a StepResult for every step."""
        steps = self._make_dummy_steps(6)
        runner = FakeRunner()
        results = run_steps(steps, runner)
        assert len(results) == 6
        assert all(sr.ok for sr in results)

    def test_arm_steps_step_count(self):
        """arm_mcu_steps produces exactly 6 steps (structural, no runner required)."""
        steps = arm_mcu_steps(_REPO_ROOT, _EPOCH)
        assert len(steps) == 6


# ──────────────────────────────────────────────────────────────────────────────
# A3-5: BuildStep and RunResult are frozen dataclasses (immutability contract)
# ──────────────────────────────────────────────────────────────────────────────

class TestDataclassImmutability:
    def test_build_step_is_frozen(self):
        step = BuildStep(
            name="x", cmd=["make"], output_path=Path("/tmp/x.bin"),
            kind=ArtifactKind.RAW_FIRMWARE,
        )
        with pytest.raises((AttributeError, TypeError)):
            step.name = "y"  # type: ignore[misc]

    def test_run_result_is_frozen(self):
        rr = RunResult(returncode=0, stdout=b"", stderr=b"", elapsed=0.0)
        with pytest.raises((AttributeError, TypeError)):
            rr.returncode = 1  # type: ignore[misc]

    def test_step_result_is_frozen(self):
        sr = StepResult(name="x", ok=True, duration=0.0, abi=None, detail="ok")
        with pytest.raises((AttributeError, TypeError)):
            sr.ok = False  # type: ignore[misc]
