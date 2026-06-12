"""Round-2 code-review regression tests.

Findings addressed: R2-C1+R2-M1, R2-H3, R2-M2, R2-L1, R2-L2.

TDD: each test class written RED against the current code, then the fix makes
it GREEN.  The full suite must remain green after all fixes.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tarfile
import zipfile
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

from build.artifacts import BuildStep, FakeRunner, RunResult, StepResult, run_steps
from build.orchestrate import build_all_artifacts
from build.release import (
    hash_artifact,
    release_members,
    submodule_provenance,
    write_release_zip,
)
from abi.abi_spec import ArtifactKind

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_EPOCH = 1_700_000_000
_FAKE_TC = Path("/fake/toolchain")

# ─────────────────────────────────────────────────────────────────────────────
# Shared fake-repo helper
# ─────────────────────────────────────────────────────────────────────────────

def _make_fake_repo(tmp_path: Path) -> Path:
    """Minimal fake repo that mirrors the real layout."""
    repo = tmp_path / "repo"
    (repo / "external" / "katapult" / "out").mkdir(parents=True)
    (repo / "external" / "klipper" / "out").mkdir(parents=True)
    (repo / "external" / "klipper" / "klippy" / "chelper").mkdir(parents=True)
    (repo / "external" / "mainsail-config").mkdir(parents=True)
    (repo / "mcu-firmware").mkdir(parents=True)
    (repo / "mcu-firmware" / "klipper.bin").write_bytes(b"klipper-captured-bin")
    (repo / "mcu-firmware" / "klipper.dict").write_bytes(b"klipper-captured-dict")
    (repo / "mcu-firmware" / "klipper_mcu.elf").write_bytes(b"klipper-mips-elf")
    (repo / "external" / "katapult" / "out" / "katapult.bin").write_bytes(b"katapult")
    (repo / "external" / "klipper" / "klippy" / "chelper" / "c_helper.so").write_bytes(b"so")
    (repo / "tools" / "v3ke").mkdir(parents=True)
    (repo / "tools" / "v3ke" / "v3ke").write_bytes(b"nim binary")
    (repo / "tools" / "v3ke" / "v3ke.exe").write_bytes(b"nim binary windows")
    (repo / "LICENSE").write_text("repo license")
    (repo / "external" / "katapult" / "LICENSE").write_text("katapult license")
    (repo / "external" / "klipper" / "COPYING").write_text("klipper license")
    (repo / "external" / "mainsail-config" / "LICENSE").write_text("mainsail license")
    import shutil
    assets_src = Path(__file__).resolve().parent.parent / "build" / "release_assets"
    if assets_src.exists():
        shutil.copytree(str(assets_src), str(repo / "release_assets"))
    else:
        (repo / "release_assets").mkdir()
        (repo / "release_assets" / "INSTALL.md").write_text("install")
        (repo / "release_assets" / "SOURCES.md").write_text("sources")
    return repo


_FAKE_SUBMODULES = {
    "klipper":         {"url": "https://github.com/Klipper3d/klipper.git",          "commit": "e60fe3d99b545d7e42ff2f5278efa5822668a57c"},
    "katapult":        {"url": "https://github.com/Arksine/katapult.git",           "commit": "b0bf421069e2aab810db43d6e15f38817d981451"},
    "mainsail-config": {"url": "https://github.com/mainsail-crew/mainsail-config.git", "commit": "ff3869a621db17ce3ef660adbbd3fa321995ac42"},
}

_SAMPLE_TOOLCHAIN = {
    "mips": {"glibc": "2.29", "gcc": "8.5.0", "binutils": "2.32",
             "linux": "4.14.329", "glibc_min_kernel": "4.4.0"},
    "arm":  {"gcc": "14.3.0"},
}


def _fake_submodule_provenance(_repo_root):
    return _FAKE_SUBMODULES


def _write_zip(tmp_path: Path):
    repo = _make_fake_repo(tmp_path)
    out_dir = tmp_path / "dist"
    out_dir.mkdir()
    zip_path = write_release_zip(
        repo_root=repo,
        out_dir=out_dir,
        version="v0.1.0",
        commit="deadbeef" * 5,
        source_date_epoch=_EPOCH,
        toolchain=_SAMPLE_TOOLCHAIN,
        reproducible=False,
        _submodule_provenance=_fake_submodule_provenance,
    )
    return zip_path, repo


# ─────────────────────────────────────────────────────────────────────────────
# R2-C1 + R2-M1: Behavioral capture-step test using REAL cp
# ─────────────────────────────────────────────────────────────────────────────

class TestR2C1CaptureStepBehavioral:
    """R2-C1: The capture steps must land the file at the DECLARED output_path.

    These tests do NOT stub run_steps — they run real cp via subprocess_runner
    so that the cmd→file invariant is exercised.  The critical case is
    klipper-elf-capture: `cp src DST_DIR` names the result after the source
    (klipper.elf), NOT after the declared output_path (klipper_mcu.elf).
    Fixing the cmd to `cp src DST_FILE` is what makes it green.
    """

    def _build_single_step(
        self, repo: Path, name: str, src_name: str, dst_name: str
    ) -> BuildStep:
        """Build a single capture step using the module's internal helper (post-M1)."""
        # Import the refactored function
        from build.orchestrate import _klipper_capture_step
        # We need to test via the real orchestrate helper; post-M1 there will be
        # one parameterized helper.  For now, build the step directly to test
        # the cmd structure:
        src = repo / "external" / "klipper" / "out" / src_name
        dst = repo / "mcu-firmware" / dst_name
        return BuildStep(
            name=name,
            cmd=["cp", str(src), str(dst)],
            output_path=dst,
            kind=ArtifactKind.RAW_FIRMWARE,
        )

    def test_klipper_elf_capture_lands_at_klipper_mcu_elf(self, tmp_path):
        """BEHAVIORAL: real cp must create mcu-firmware/klipper_mcu.elf, not klipper.elf.

        This is the core R2-C1 regression test.  It MUST FAIL against the
        current ``cmd=["cp", str(src), str(dst_dir)]`` code (which creates
        klipper.elf, not klipper_mcu.elf) and PASS after fixing the cmd to
        use str(dst) as the destination.

        Test name: test_klipper_elf_capture_lands_at_klipper_mcu_elf
        """
        # Set up a fake repo with the MIPS elf in external/klipper/out/
        repo = tmp_path / "repo"
        mcu_fw = repo / "mcu-firmware"
        klipper_out = repo / "external" / "klipper" / "out"
        mcu_fw.mkdir(parents=True)
        klipper_out.mkdir(parents=True)

        # Write known bytes to the source elf
        known_bytes = b"FAKE-MIPS-ELF-DATA-12345"
        (klipper_out / "klipper.elf").write_bytes(known_bytes)

        # Build the elf capture step as orchestrate.py would
        src = klipper_out / "klipper.elf"
        dst = mcu_fw / "klipper_mcu.elf"

        # Build step from orchestrate.py's _klipper_elf_capture_step logic
        # (pre-fix: cmd uses str(dst.parent) — the directory — as destination)
        # We retrieve the ACTUAL step from the module to test what it produces:
        from build import orchestrate as orch

        # Monkeypatch _REPO_ROOT-style call: call _klipper_elf_capture_step directly
        step = orch._klipper_capture_step(
            repo,
            src_name="klipper.elf",
            dst_name="klipper_mcu.elf",
            step_name="klipper-elf-capture",
        )

        # Run the step with subprocess_runner (real cp)
        from build.artifacts import subprocess_runner
        result = subprocess_runner(step.cmd)
        assert result.returncode == 0, (
            f"cp command failed: {step.cmd!r}\nstderr: {result.stderr}"
        )

        # The declared output_path must exist with the correct content
        assert dst.exists(), (
            f"mcu-firmware/klipper_mcu.elf must exist after capture; "
            f"files in mcu-firmware/: {list(mcu_fw.iterdir())}"
        )
        assert dst.read_bytes() == known_bytes, (
            f"klipper_mcu.elf content mismatch"
        )

        # The wrong file (klipper.elf) must NOT exist in mcu-firmware/
        wrong = mcu_fw / "klipper.elf"
        assert not wrong.exists(), (
            f"mcu-firmware/klipper.elf must NOT exist (cp was given a directory, "
            f"not the renamed destination)"
        )

    def test_klipper_elf_capture_run_steps_ok_true(self, tmp_path):
        """run_steps must report ok=True when the elf capture step runs correctly.

        Pre-fix: cp creates klipper.elf but output_path is klipper_mcu.elf →
        output_path.exists() is False → ok=False → RuntimeError raised.
        Post-fix: cp creates klipper_mcu.elf → ok=True.
        """
        repo = tmp_path / "repo"
        mcu_fw = repo / "mcu-firmware"
        klipper_out = repo / "external" / "klipper" / "out"
        mcu_fw.mkdir(parents=True)
        klipper_out.mkdir(parents=True)
        (klipper_out / "klipper.elf").write_bytes(b"MIPS-ELF")

        from build import orchestrate as orch
        from build.artifacts import subprocess_runner

        step = orch._klipper_capture_step(
            repo,
            src_name="klipper.elf",
            dst_name="klipper_mcu.elf",
            step_name="klipper-elf-capture",
        )

        # Must NOT raise RuntimeError (pre-fix it raises because output_path missing)
        results = run_steps([step], subprocess_runner)
        assert len(results) == 1
        assert results[0].ok is True, (
            f"elf capture step must report ok=True; detail: {results[0].detail!r}"
        )

    def test_klipper_bin_capture_lands_at_klipper_bin(self, tmp_path):
        """BEHAVIORAL: klipper.bin capture must create mcu-firmware/klipper.bin."""
        repo = tmp_path / "repo"
        mcu_fw = repo / "mcu-firmware"
        klipper_out = repo / "external" / "klipper" / "out"
        mcu_fw.mkdir(parents=True)
        klipper_out.mkdir(parents=True)
        known_bytes = b"ARM-KLIPPER-BIN"
        (klipper_out / "klipper.bin").write_bytes(known_bytes)

        from build import orchestrate as orch
        from build.artifacts import subprocess_runner

        step = orch._klipper_capture_step(
            repo,
            src_name="klipper.bin",
            dst_name="klipper.bin",
            step_name="klipper-capture",
        )

        results = run_steps([step], subprocess_runner)
        assert results[0].ok is True

        dst = mcu_fw / "klipper.bin"
        assert dst.exists()
        assert dst.read_bytes() == known_bytes

    def test_klipper_dict_capture_lands_at_klipper_dict(self, tmp_path):
        """BEHAVIORAL: klipper.dict capture must create mcu-firmware/klipper.dict."""
        repo = tmp_path / "repo"
        mcu_fw = repo / "mcu-firmware"
        klipper_out = repo / "external" / "klipper" / "out"
        mcu_fw.mkdir(parents=True)
        klipper_out.mkdir(parents=True)
        known_bytes = b"KLIPPER-DICT-DATA"
        (klipper_out / "klipper.dict").write_bytes(known_bytes)

        from build import orchestrate as orch
        from build.artifacts import subprocess_runner

        step = orch._klipper_capture_step(
            repo,
            src_name="klipper.dict",
            dst_name="klipper.dict",
            step_name="klipper-dict-capture",
        )

        results = run_steps([step], subprocess_runner)
        assert results[0].ok is True

        dst = mcu_fw / "klipper.dict"
        assert dst.exists()
        assert dst.read_bytes() == known_bytes


class TestR2M1CaptureStepTriplication:
    """R2-M1: The three capture builders must be one parameterized helper."""

    def test_single_capture_helper_exists(self):
        """orchestrate must expose _klipper_capture_step(repo, *, src_name, dst_name, step_name)."""
        from build import orchestrate as orch
        import inspect
        assert hasattr(orch, "_klipper_capture_step"), (
            "orchestrate must expose _klipper_capture_step"
        )
        sig = inspect.signature(orch._klipper_capture_step)
        params = sig.parameters
        assert "src_name" in params, "must have src_name parameter"
        assert "dst_name" in params, "must have dst_name parameter"
        assert "step_name" in params, "must have step_name parameter"

    def test_no_separate_elf_capture_function(self):
        """_klipper_elf_capture_step and _klipper_dict_capture_step must not exist (collapsed)."""
        from build import orchestrate as orch
        assert not hasattr(orch, "_klipper_elf_capture_step"), (
            "_klipper_elf_capture_step must be removed after M1 collapse"
        )
        assert not hasattr(orch, "_klipper_dict_capture_step"), (
            "_klipper_dict_capture_step must be removed after M1 collapse"
        )

    def test_capture_step_cmd_uses_full_dst_path(self):
        """The cmd must use the full dst file path, not the parent directory."""
        from build import orchestrate as orch
        repo = Path("/fake/repo")
        step = orch._klipper_capture_step(
            repo,
            src_name="klipper.elf",
            dst_name="klipper_mcu.elf",
            step_name="klipper-elf-capture",
        )
        # The dst argument to cp must be the full file path, not the directory
        dst_arg = step.cmd[-1]
        assert dst_arg.endswith("klipper_mcu.elf"), (
            f"cmd dst must be the full file path ending in klipper_mcu.elf; "
            f"got: {dst_arg!r}"
        )
        assert not dst_arg.endswith("/mcu-firmware"), (
            f"cmd dst must NOT be just the directory; got: {dst_arg!r}"
        )
        assert not dst_arg.endswith("/mcu-firmware/"), (
            f"cmd dst must NOT be just the directory with trailing slash; got: {dst_arg!r}"
        )

    def test_capture_step_output_path_matches_cmd_dst(self):
        """output_path must equal the actual cp destination (the full file path)."""
        from build import orchestrate as orch
        repo = Path("/fake/repo")
        step = orch._klipper_capture_step(
            repo,
            src_name="klipper.elf",
            dst_name="klipper_mcu.elf",
            step_name="klipper-elf-capture",
        )
        # The last arg of cmd IS the destination; output_path must match it
        cmd_dst = Path(step.cmd[-1])
        assert step.output_path == cmd_dst, (
            f"output_path {step.output_path!r} must match cmd dst {cmd_dst!r}"
        )

    def test_orchestrate_still_produces_13_steps(self):
        """build_all_artifacts must still produce 13 steps after M1 collapse."""
        recorded: list = []

        def capturing_run_steps(steps, runner, *, repo_root=None):
            recorded.extend(steps)
            return [StepResult(name=s.name, ok=True, duration=0.0, abi=None, detail="ok") for s in steps]

        with patch("build.orchestrate.run_steps", capturing_run_steps):
            build_all_artifacts(
                repo_root=_REPO_ROOT,
                toolchain_root=_FAKE_TC,
                runner=FakeRunner(),
                epoch=_EPOCH,
            )
        assert len(recorded) == 13, (
            f"Expected 13 steps after M1 collapse; got {len(recorded)}"
        )

    def test_orchestrate_step_names_unchanged(self):
        """Step names must be identical after M1 collapse."""
        recorded: list = []

        def capturing_run_steps(steps, runner, *, repo_root=None):
            recorded.extend(steps)
            return [StepResult(name=s.name, ok=True, duration=0.0, abi=None, detail="ok") for s in steps]

        with patch("build.orchestrate.run_steps", capturing_run_steps):
            build_all_artifacts(
                repo_root=_REPO_ROOT,
                toolchain_root=_FAKE_TC,
                runner=FakeRunner(),
                epoch=_EPOCH,
            )
        names = [s.name for s in recorded]
        assert "klipper-capture" in names
        assert "klipper-dict-capture" in names
        assert "klipper-elf-capture" in names


# ─────────────────────────────────────────────────────────────────────────────
# R2-H3: Fail-fast dummy steps must use output_path=None, not Path("")
# ─────────────────────────────────────────────────────────────────────────────

class TestR2H3FailFastDummySteps:
    """R2-H3: _make_dummy_steps in TestFailFast uses output_path=None so the
    H1 existence check is genuinely bypassed (not accidentally satisfied by
    Path("") resolving to cwd which exists).
    """

    def test_none_output_path_bypasses_h1_check(self):
        """A step with output_path=None must succeed even when no file is created."""
        step = BuildStep(
            name="side-effect-only",
            cmd=["true"],
            output_path=None,
            kind=ArtifactKind.RAW_FIRMWARE,
        )
        results = run_steps([step], FakeRunner())
        assert results[0].ok is True, (
            "output_path=None must bypass H1 existence check → ok=True"
        )

    def test_empty_path_would_accidentally_exist(self, tmp_path):
        """Demonstrate why Path('') is wrong: it resolves to cwd which EXISTS.

        This test is intentionally written to DOCUMENT the bug, not assert it.
        The real fix is to use output_path=None for side-effect-only steps.
        """
        # Path("").resolve() == cwd, which exists — this is the bug
        assert Path("").exists(), (
            "Path('') resolves to cwd which exists — this is why Path('') is wrong"
        )

    def test_rc0_step_with_missing_nonnone_output_path_fails(self, tmp_path):
        """A step with rc=0 but a declared non-None output_path that is absent → ok=False.

        This is the H1 contract.  The dummy steps in the fail-fast suite must use
        output_path=None so they genuinely exercise the None bypass, not accidentally
        pass H1 via Path('') → cwd → exists.
        """
        missing = tmp_path / "nonexistent_artifact.bin"
        assert not missing.exists()

        step = BuildStep(
            name="rc0-but-missing-output",
            cmd=["true"],
            output_path=missing,
            kind=ArtifactKind.RAW_FIRMWARE,
        )
        with pytest.raises(RuntimeError) as exc_info:
            run_steps([step], FakeRunner())
        results = exc_info.value.results
        assert results[0].ok is False, (
            "rc=0 + missing non-None output_path must produce ok=False (H1 contract)"
        )

    def test_fail_fast_with_none_output_path_still_raises_on_rc1(self):
        """Fail-fast behavior works correctly with output_path=None steps."""
        steps = [
            BuildStep(name=f"step-{i}", cmd=["true"], output_path=None,
                      kind=ArtifactKind.RAW_FIRMWARE)
            for i in range(3)
        ]
        # Fail at step 1
        runner = FakeRunner(fail_at=1)
        with pytest.raises(RuntimeError) as exc_info:
            run_steps(steps, runner)
        results = exc_info.value.results
        assert len(results) == 2
        assert results[0].ok is True
        assert results[1].ok is False

    def test_all_none_output_path_steps_succeed(self):
        """All steps with output_path=None succeed (no H1 check, no false failure)."""
        steps = [
            BuildStep(name=f"step-{i}", cmd=["true"], output_path=None,
                      kind=ArtifactKind.RAW_FIRMWARE)
            for i in range(5)
        ]
        results = run_steps(steps, FakeRunner())
        assert len(results) == 5
        assert all(sr.ok for sr in results)


# ─────────────────────────────────────────────────────────────────────────────
# R2-M2: submodule_provenance must use concrete default runner, not None sentinel
# ─────────────────────────────────────────────────────────────────────────────

class TestR2M2SubmoduleProvenanceRunner:
    """R2-M2: submodule_provenance must use a concrete default runner parameter,
    not Optional[Callable]=None with an internal _run closure.
    """

    def test_runner_param_has_concrete_default(self):
        """runner parameter must have a concrete callable default (not None)."""
        import inspect
        sig = inspect.signature(submodule_provenance)
        runner_param = sig.parameters.get("runner")
        assert runner_param is not None, "submodule_provenance must have a runner param"
        default = runner_param.default
        assert default is not inspect.Parameter.empty, "runner must have a default"
        assert default is not None, (
            "runner default must be a concrete callable, not None; "
            "use _subprocess_runner_text as the default"
        )
        assert callable(default), (
            f"runner default must be callable; got {default!r}"
        )

    def test_no_internal_run_closure(self):
        """submodule_provenance must not use an internal _run closure (None-sentinel pattern)."""
        import ast
        import inspect
        src = inspect.getsource(submodule_provenance)
        tree = ast.parse(src)
        # Look for inner function definitions named _run
        inner_funcs = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        ]
        assert "_run" not in inner_funcs, (
            "submodule_provenance must not have an inner _run closure; "
            "use the concrete default runner parameter directly"
        )

    def test_fake_runner_still_works(self):
        """The existing fake-runner behavioral test must still pass post-M2."""
        fake_responses = {
            "submodule.external/klipper.url":         "https://github.com/Klipper3d/klipper.git",
            "submodule.external/katapult.url":        "https://github.com/Arksine/katapult.git",
            "submodule.external/mainsail-config.url": "https://github.com/mainsail-crew/mainsail-config.git",
            "HEAD:external/klipper":         "e60fe3d99b545d7e42ff2f5278efa5822668a57c",
            "HEAD:external/katapult":        "b0bf421069e2aab810db43d6e15f38817d981451",
            "HEAD:external/mainsail-config": "ff3869a621db17ce3ef660adbbd3fa321995ac42",
        }

        def fake_runner(cmd: list[str]) -> str:
            if "config" in cmd and "--file" in cmd:
                key = cmd[-1]
                return fake_responses[key] + "\n"
            if "rev-parse" in cmd:
                ref = cmd[-1]
                return fake_responses[ref] + "\n"
            raise ValueError(f"Unexpected cmd in fake_runner: {cmd}")

        result = submodule_provenance(Path("/fake/repo"), runner=fake_runner)
        assert set(result.keys()) == {"klipper", "katapult", "mainsail-config"}
        assert result["klipper"]["commit"] == "e60fe3d99b545d7e42ff2f5278efa5822668a57c"
        assert result["katapult"]["url"] == "https://github.com/Arksine/katapult.git"
        assert result["mainsail-config"]["commit"] == "ff3869a621db17ce3ef660adbbd3fa321995ac42"


# ─────────────────────────────────────────────────────────────────────────────
# R2-L1: write_release_zip must read each artifact once (manifest sha256 integrity)
# ─────────────────────────────────────────────────────────────────────────────

class TestR2L1ManifestIntegrity:
    """R2-L1: The manifest sha256 for each artifact must match the bytes actually
    packed into the zip (read-once guarantee).
    """

    def test_manifest_sha256_matches_packed_bytes(self, tmp_path):
        """For every artifact in the manifest, sha256 must equal sha256(bundle_bytes)."""
        bundle_path, repo = _write_zip(tmp_path)
        with tarfile.open(bundle_path, "r:xz") as tf:
            manifest = json.loads(tf.extractfile("manifest.json").read())
            for entry in manifest["artifacts"]:
                arc_path = entry["path"]
                packed_bytes = tf.extractfile(arc_path).read()
                actual_sha = hashlib.sha256(packed_bytes).hexdigest()
                assert actual_sha == entry["sha256"], (
                    f"Manifest sha256 for {arc_path!r} does not match packed bytes:\n"
                    f"  manifest: {entry['sha256']}\n"
                    f"  actual:   {actual_sha}"
                )

    def test_manifest_size_matches_packed_bytes(self, tmp_path):
        """For every artifact in the manifest, size must equal len(bundle_bytes)."""
        bundle_path, repo = _write_zip(tmp_path)
        with tarfile.open(bundle_path, "r:xz") as tf:
            manifest = json.loads(tf.extractfile("manifest.json").read())
            for entry in manifest["artifacts"]:
                arc_path = entry["path"]
                packed_bytes = tf.extractfile(arc_path).read()
                assert entry["size"] == len(packed_bytes), (
                    f"Manifest size for {arc_path!r} does not match packed bytes: "
                    f"manifest={entry['size']}, actual={len(packed_bytes)}"
                )

    def test_manifest_sha256_for_known_content(self, tmp_path):
        """Specific known-content artifact: klipper.bin sha256 must match bytes in bundle."""
        bundle_path, repo = _write_zip(tmp_path)
        # The fake repo writes b"klipper-captured-bin" to mcu-firmware/klipper.bin
        expected_sha = hashlib.sha256(b"klipper-captured-bin").hexdigest()

        with tarfile.open(bundle_path, "r:xz") as tf:
            manifest = json.loads(tf.extractfile("manifest.json").read())
            packed = tf.extractfile("firmware/klipper.bin").read()

        actual_sha = hashlib.sha256(packed).hexdigest()
        assert actual_sha == expected_sha, "klipper.bin sha256 mismatch"

        klipper_entry = next(
            (a for a in manifest["artifacts"] if a["path"] == "firmware/klipper.bin"), None
        )
        assert klipper_entry is not None, "firmware/klipper.bin must be in manifest artifacts"
        assert klipper_entry["sha256"] == expected_sha, (
            f"Manifest sha256 for firmware/klipper.bin must match known bytes; "
            f"got {klipper_entry['sha256']!r}, expected {expected_sha!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# R2-L2: Stale assertions and redundant test
# ─────────────────────────────────────────────────────────────────────────────

class TestR2L2StaleAssertions:
    """R2-L2: Verify stale != Path("") assertions have been changed to is not None,
    and redundant test_injectable_runner_is_accepted is gone.
    """

    def test_katapult_output_path_is_not_none(self):
        """The katapult build step output_path sentinel is None, not Path('').

        Replaces: assert build_step.output_path != Path("")
        With:     assert build_step.output_path is not None
        """
        from build.arm_mcu import katapult_steps
        steps = katapult_steps(_REPO_ROOT, _EPOCH)
        build_step = steps[-1]
        assert build_step.output_path is not None, (
            "katapult build step output_path must not be None"
        )

    def test_klipper_output_path_is_not_none(self):
        """The klipper build step output_path sentinel is None, not Path('').

        Replaces: assert build_step.output_path != Path("")
        With:     assert build_step.output_path is not None
        """
        from build.arm_mcu import klipper_steps
        steps = klipper_steps(_REPO_ROOT, _EPOCH)
        build_step = steps[-1]
        assert build_step.output_path is not None, (
            "klipper build step output_path must not be None"
        )

    def test_capture_output_path_is_not_none(self):
        """The capture step output_path must not be None.

        Replaces stale != Path("") assertion in test_orchestrate_a5a.py.
        """
        from build import orchestrate as orch
        step = orch._klipper_capture_step(
            Path("/fake/repo"),
            src_name="klipper.bin",
            dst_name="klipper.bin",
            step_name="klipper-capture",
        )
        assert step.output_path is not None, (
            "capture step output_path must not be None"
        )

    def test_injectable_runner_inspect_test_removed(self):
        """test_injectable_runner_is_accepted (signature inspect) must be removed.

        The behavioral fake-runner test (test_fake_runner_returns_expected_provenance)
        provides full coverage.  The inspect test is redundant.

        We verify it does not exist in test_findings.py.
        """
        import ast
        test_findings_path = Path(__file__).parent / "test_findings.py"
        src = test_findings_path.read_text()
        tree = ast.parse(src)
        # Find all test methods in TestM6SubmoduleProvenanceInjectableRunner
        method_names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and "TestM6" in node.name:
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        method_names.append(item.name)
        assert "test_injectable_runner_is_accepted" not in method_names, (
            "test_injectable_runner_is_accepted (redundant inspect test) must be deleted"
        )
