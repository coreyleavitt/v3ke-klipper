"""A5a unit tests — end-to-end wiring of build.py artifacts via build_all_artifacts.

Slice A5a: the orchestration function assembles all BuildSteps (ARM MCU + capture +
host), runs them via an injectable runner, and build.py cmd_artifacts invokes it in
one container run without shelling out to the bash scripts.

Test structure
──────────────
A5a-1: build_all_artifacts with FakeRunner produces exactly 11 steps in the
       canonical order (6 ARM + 1 capture + 4 host) and returns 11 StepResults,
       all ok=True.
A5a-2: Step ordering — klipper-build (ARM) precedes klipper-capture, which precedes
       klipper-mcu-clean (host); this guarantees klipper.bin survives the host clean.
A5a-3: The capture step is a cp command that copies klipper.bin from external/klipper/out/
       to mcu-firmware/.  It is kind=RAW_FIRMWARE so ABI check is skipped.
A5a-4: build_all_artifacts does NOT reference the bash scripts anywhere in the step
       cmd lists (build-bootloader-mcu-and-host-firmware.sh, verify-artifacts.sh,
       build-chelper.sh, build-klipper-host-mcu.sh).
A5a-5: build_all_artifacts with epoch=None resolves SOURCE_DATE_EPOCH via
       resolve_source_date_epoch (injected for the test); with epoch supplied the
       injected resolver is never called.
A5a-6: cmd_artifacts (from build.py) does not reference the bash scripts in its podman
       command; it invokes a Python entrypoint instead (python -m ... or uv run ...).
A5a-7: The orchestration entrypoint module (build.orchestrate) is importable and
       exposes build_all_artifacts.
A5a-8: FP64 ABI check skipped for RAW_FIRMWARE artifacts; EXECUTABLE/SHARED_LIBRARY
       steps have abi=None only because FakeRunner produces no real output files
       (the guard logic path is tested by A3-3 / A4-6; this test confirms the overall
       result count under FakeRunner).
"""

from __future__ import annotations

import importlib
import pathlib
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

from build.artifacts import BuildStep, FakeRunner, run_steps, StepResult
from build.orchestrate import build_all_artifacts
from abi.abi_spec import ArtifactKind

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_EPOCH = 1_700_000_000
_FAKE_TC = Path("/fake/toolchain")

# Canonical bash script filenames that must NOT appear in any step cmd.
_BANNED_SCRIPTS = {
    "build-bootloader-mcu-and-host-firmware.sh",
    "verify-artifacts.sh",
    "build-chelper.sh",
    "build-klipper-host-mcu.sh",
}

# Expected step names in canonical order.
# 6 ARM + 1 capture + 4 host = 11 total.
_EXPECTED_STEP_NAMES = [
    "katapult-clean",
    "katapult-olddefconfig",
    "katapult-build",
    "klipper-clean",
    "klipper-olddefconfig",
    "klipper-build",
    "klipper-capture",          # captures klipper.bin before host clean wipes it
    "c-helper-build",
    "klipper-mcu-clean",
    "klipper-mcu-olddefconfig",
    "klipper-mcu-build",
]


def _all_steps() -> list[BuildStep]:
    """Return steps from build_all_artifacts without running them."""
    recorded: list[list[BuildStep]] = []

    def capturing_run_steps(steps, runner, *, repo_root=None):
        recorded.append(list(steps))
        return [StepResult(name=s.name, ok=True, duration=0.0, abi=None, detail="ok") for s in steps]

    with patch("build.orchestrate.run_steps", capturing_run_steps):
        build_all_artifacts(
            repo_root=_REPO_ROOT,
            toolchain_root=_FAKE_TC,
            runner=FakeRunner(),
            epoch=_EPOCH,
        )

    return recorded[0]


# ──────────────────────────────────────────────────────────────────────────────
# A5a-7: module is importable and exposes build_all_artifacts
# ──────────────────────────────────────────────────────────────────────────────

class TestModuleImportable:
    def test_build_orchestrate_importable(self):
        mod = importlib.import_module("build.orchestrate")
        assert hasattr(mod, "build_all_artifacts"), (
            "build.orchestrate must expose build_all_artifacts"
        )

    def test_build_all_artifacts_callable(self):
        from build.orchestrate import build_all_artifacts
        assert callable(build_all_artifacts)


# ──────────────────────────────────────────────────────────────────────────────
# A5a-1: step count and all-ok under FakeRunner
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildAllArtifactsStepCount:
    """build_all_artifacts must produce exactly 11 steps (6 ARM + 1 capture + 4 host)."""

    def test_returns_11_step_results(self):
        results = build_all_artifacts(
            repo_root=_REPO_ROOT,
            toolchain_root=_FAKE_TC,
            runner=FakeRunner(),
            epoch=_EPOCH,
        )
        assert len(results) == 11, (
            f"Expected 11 StepResults, got {len(results)}: "
            f"{[r.name for r in results]}"
        )

    def test_all_steps_ok_with_fake_runner(self):
        results = build_all_artifacts(
            repo_root=_REPO_ROOT,
            toolchain_root=_FAKE_TC,
            runner=FakeRunner(),
            epoch=_EPOCH,
        )
        for r in results:
            assert r.ok, f"Step '{r.name}' unexpectedly failed"

    def test_step_names_in_canonical_order(self):
        results = build_all_artifacts(
            repo_root=_REPO_ROOT,
            toolchain_root=_FAKE_TC,
            runner=FakeRunner(),
            epoch=_EPOCH,
        )
        actual_names = [r.name for r in results]
        assert actual_names == _EXPECTED_STEP_NAMES, (
            f"Step order mismatch:\n  expected: {_EXPECTED_STEP_NAMES}\n"
            f"  actual:   {actual_names}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# A5a-2: sequencing — capture precedes host klipper_mcu clean
# ──────────────────────────────────────────────────────────────────────────────

class TestStepOrdering:
    """The capture step must sit between ARM klipper-build and host klipper-mcu-clean."""

    def test_klipper_build_before_capture(self):
        steps = _all_steps()
        names = [s.name for s in steps]
        assert names.index("klipper-build") < names.index("klipper-capture"), (
            "klipper-build must precede klipper-capture"
        )

    def test_capture_before_klipper_mcu_clean(self):
        steps = _all_steps()
        names = [s.name for s in steps]
        assert names.index("klipper-capture") < names.index("klipper-mcu-clean"), (
            "klipper-capture must precede klipper-mcu-clean (host clean would wipe klipper.bin)"
        )

    def test_c_helper_before_klipper_mcu(self):
        """c-helper-build must precede klipper-mcu-clean (host ordering from host_steps)."""
        steps = _all_steps()
        names = [s.name for s in steps]
        assert names.index("c-helper-build") < names.index("klipper-mcu-clean"), (
            "c-helper-build must precede klipper-mcu-clean"
        )

    def test_arm_steps_before_host_steps(self):
        """All 6 ARM steps must precede all 4 host steps (with capture in between)."""
        steps = _all_steps()
        names = [s.name for s in steps]
        arm_indices = [i for i, n in enumerate(names)
                       if n.startswith(("katapult-", "klipper-clean", "klipper-olddefconfig", "klipper-build"))]
        host_indices = [i for i, n in enumerate(names)
                        if n.startswith(("c-helper-", "klipper-mcu-"))]
        assert arm_indices, "No ARM step names found"
        assert host_indices, "No host step names found"
        assert max(arm_indices) < min(host_indices), (
            "All ARM steps must come before all host steps"
        )


# ──────────────────────────────────────────────────────────────────────────────
# A5a-3: capture step structure
# ──────────────────────────────────────────────────────────────────────────────

class TestCaptureStep:
    """The klipper-capture step must be a cp that moves klipper.bin to mcu-firmware/."""

    def _capture_step(self) -> BuildStep:
        steps = _all_steps()
        capture = next((s for s in steps if s.name == "klipper-capture"), None)
        assert capture is not None, "klipper-capture step not found in step list"
        return capture

    def test_capture_step_is_cp_command(self):
        step = self._capture_step()
        assert step.cmd[0] == "cp", (
            f"klipper-capture step must use 'cp', got: {step.cmd[0]!r}"
        )

    def test_capture_step_copies_klipper_bin(self):
        """The source of the cp must be external/klipper/out/klipper.bin."""
        step = self._capture_step()
        src = step.cmd[-2]  # cp <src> <dst>
        assert src.endswith("klipper.bin"), (
            f"cp source must be klipper.bin, got: {src!r}"
        )
        assert "external/klipper/out" in src or str(
            _REPO_ROOT / "external" / "klipper" / "out" / "klipper.bin"
        ) == src, f"cp source should be under external/klipper/out/: {src!r}"

    def test_capture_step_destination_is_mcu_firmware(self):
        """The destination of the cp must be mcu-firmware/."""
        step = self._capture_step()
        dst = step.cmd[-1]
        assert "mcu-firmware" in dst, (
            f"cp destination must be under mcu-firmware/, got: {dst!r}"
        )

    def test_capture_step_kind_is_raw_firmware(self):
        """Capture step must be RAW_FIRMWARE (skips ABI check)."""
        step = self._capture_step()
        assert step.kind is ArtifactKind.RAW_FIRMWARE, (
            f"klipper-capture must be RAW_FIRMWARE, got {step.kind!r}"
        )

    def test_capture_output_path_in_mcu_firmware(self):
        """output_path for the capture step is in mcu-firmware/ (the captured location)."""
        step = self._capture_step()
        assert step.output_path != Path(""), "capture step must have an output_path"
        assert "mcu-firmware" in step.output_path.parts, (
            f"capture output_path must be under mcu-firmware/, got: {step.output_path}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# A5a-4: no bash script references in any step cmd
# ──────────────────────────────────────────────────────────────────────────────

class TestNoBashScripts:
    """No step cmd in build_all_artifacts may reference the legacy bash scripts."""

    def test_no_bash_scripts_in_step_cmds(self):
        steps = _all_steps()
        for step in steps:
            cmd_str = " ".join(step.cmd)
            for script in _BANNED_SCRIPTS:
                assert script not in cmd_str, (
                    f"Step '{step.name}' references banned script '{script}': {step.cmd}"
                )

    def test_no_bash_invocation_in_step_cmds(self):
        """No step should invoke bash (as opposed to a direct tool command)."""
        steps = _all_steps()
        # 'bash' as the first element of a cmd is the tell for a shell-out
        bash_steps = [s for s in steps if s.cmd and s.cmd[0] == "bash"]
        assert not bash_steps, (
            f"Steps should not invoke 'bash' directly: {[s.name for s in bash_steps]}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# A5a-5: epoch resolution — None defers to resolve_source_date_epoch
# ──────────────────────────────────────────────────────────────────────────────

class TestEpochResolution:
    """epoch=None must call resolve_source_date_epoch; epoch supplied must skip it."""

    def test_none_epoch_calls_resolver(self):
        """When epoch=None, the resolver is called exactly once."""
        call_count = [0]

        def fake_resolver(repo_root: Path) -> int:
            call_count[0] += 1
            return _EPOCH

        build_all_artifacts(
            repo_root=_REPO_ROOT,
            toolchain_root=_FAKE_TC,
            runner=FakeRunner(),
            epoch=None,
            _resolve_epoch=fake_resolver,
        )
        assert call_count[0] == 1, (
            f"Resolver called {call_count[0]} times; expected exactly 1"
        )

    def test_supplied_epoch_skips_resolver(self):
        """When epoch is supplied, the resolver must never be called."""
        call_count = [0]

        def should_not_be_called(repo_root: Path) -> int:
            call_count[0] += 1
            return 0

        build_all_artifacts(
            repo_root=_REPO_ROOT,
            toolchain_root=_FAKE_TC,
            runner=FakeRunner(),
            epoch=_EPOCH,
            _resolve_epoch=should_not_be_called,
        )
        assert call_count[0] == 0, (
            f"Resolver called {call_count[0]} times; should have been skipped (epoch supplied)"
        )

    def test_resolver_receives_repo_root(self):
        """The resolver receives the same repo_root passed to build_all_artifacts."""
        received_roots: list[Path] = []

        def recording_resolver(repo_root: Path) -> int:
            received_roots.append(repo_root)
            return _EPOCH

        build_all_artifacts(
            repo_root=_REPO_ROOT,
            toolchain_root=_FAKE_TC,
            runner=FakeRunner(),
            epoch=None,
            _resolve_epoch=recording_resolver,
        )
        assert received_roots == [_REPO_ROOT], (
            f"Resolver received wrong repo_root: {received_roots}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# A5a-6: cmd_artifacts invokes Python, not bash scripts
# ──────────────────────────────────────────────────────────────────────────────

class TestCmdArtifactsNoBash:
    """build.py cmd_artifacts must not reference the bash scripts in its podman invocation."""

    def _capture_podman_cmds(self) -> list[list[str]]:
        """Import build.py as a module and capture the commands it would run."""
        import importlib.util, sys

        # Import build.py (it lives at tools/build.py, not in a package)
        build_py = _REPO_ROOT / "tools" / "build.py"
        spec = importlib.util.spec_from_file_location("build_cli", str(build_py))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        captured: list[list[str]] = []

        def fake_run(cmd):
            captured.append(list(cmd))

        def fake_require_image(image, **_kwargs):
            pass  # skip the image-exists check

        original_run = mod.run
        original_require = mod.require_image
        mod.run = fake_run
        mod.require_image = fake_require_image
        try:
            # Construct a minimal namespace mimicking the parsed args
            class FakeArgs:
                image = "v3ke-toolchain"
                xtools_vol = None
            mod.cmd_artifacts(FakeArgs())
        finally:
            mod.run = original_run
            mod.require_image = original_require

        return captured

    def test_cmd_artifacts_issues_exactly_one_podman_run(self):
        """cmd_artifacts must issue exactly one podman run (not two)."""
        cmds = self._capture_podman_cmds()
        podman_runs = [c for c in cmds if c[:2] == ["podman", "run"]]
        assert len(podman_runs) == 1, (
            f"Expected exactly 1 podman run, got {len(podman_runs)}: {podman_runs}"
        )

    def test_cmd_artifacts_invokes_python_not_bash(self):
        """The podman run must invoke python/uv, not bash."""
        cmds = self._capture_podman_cmds()
        podman_runs = [c for c in cmds if c[:2] == ["podman", "run"]]
        assert podman_runs, "No podman run found"
        cmd_str = " ".join(podman_runs[0])
        # Must NOT use bash as the entrypoint
        assert "bash" not in cmd_str, (
            f"cmd_artifacts must not invoke bash; got: {cmd_str}"
        )

    def test_cmd_artifacts_no_bash_scripts_in_cmd(self):
        """The podman command must not reference any of the banned bash scripts."""
        cmds = self._capture_podman_cmds()
        for cmd in cmds:
            cmd_str = " ".join(cmd)
            for script in _BANNED_SCRIPTS:
                assert script not in cmd_str, (
                    f"cmd_artifacts references banned script '{script}': {cmd}"
                )


# ──────────────────────────────────────────────────────────────────────────────
# B3 addition: --runtime flag wires the correct container runtime
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildPyRuntimeFlag:
    """build.py --runtime docker must replace 'podman' with 'docker' in cmd_artifacts."""

    def _load_build_mod(self):
        import importlib.util
        build_py = _REPO_ROOT / "tools" / "build.py"
        spec = importlib.util.spec_from_file_location("build_cli_rt", str(build_py))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _capture_cmds_with_runtime(self, runtime: str) -> list[list[str]]:
        mod = self._load_build_mod()
        captured: list[list[str]] = []

        mod.run = lambda cmd: captured.append(list(cmd))
        mod.require_image = lambda image, **_kw: None

        class FakeArgs:
            image = "v3ke-toolchain"
            xtools_vol = None

        args = FakeArgs()
        args.runtime = runtime
        mod.cmd_artifacts(args)
        return captured

    def test_default_runtime_is_podman(self):
        """Without a runtime attribute, cmd_artifacts defaults to podman."""
        mod = self._load_build_mod()
        captured: list[list[str]] = []
        mod.run = lambda cmd: captured.append(list(cmd))
        mod.require_image = lambda image, **_kw: None

        class FakeArgsNoRuntime:
            image = "v3ke-toolchain"
            xtools_vol = None

        mod.cmd_artifacts(FakeArgsNoRuntime())
        assert captured, "No command captured"
        assert captured[0][0] == "podman", (
            f"Default runtime must be podman; first command: {captured[0]}"
        )

    def test_runtime_docker_uses_docker(self):
        """--runtime docker substitutes 'docker' as the container executable."""
        cmds = self._capture_cmds_with_runtime("docker")
        assert cmds, "No command captured"
        assert cmds[0][0] == "docker", (
            f"Expected 'docker' as first token; got: {cmds[0]}"
        )

    def test_runtime_docker_run_has_correct_structure(self):
        """docker run command must have the standard structure (docker run --rm …)."""
        cmds = self._capture_cmds_with_runtime("docker")
        docker_runs = [c for c in cmds if len(c) >= 2 and c[0] == "docker" and c[1] == "run"]
        assert len(docker_runs) == 1, (
            f"Expected exactly 1 docker run; got: {docker_runs}"
        )
        assert "--rm" in docker_runs[0], (
            f"docker run must include --rm; got: {docker_runs[0]}"
        )

    def test_runtime_docker_invokes_orchestrate(self):
        """The docker run command must invoke python3 -m build.orchestrate."""
        cmds = self._capture_cmds_with_runtime("docker")
        docker_runs = [c for c in cmds if len(c) >= 2 and c[0] == "docker" and c[1] == "run"]
        assert docker_runs, "No docker run found"
        cmd_str = " ".join(docker_runs[0])
        assert "build.orchestrate" in cmd_str, (
            f"docker run must invoke python3 -m build.orchestrate; got: {cmd_str}"
        )
