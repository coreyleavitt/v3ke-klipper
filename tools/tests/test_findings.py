"""Regression tests for code-review findings C1, C2, C-elf, H1, M5, M6, M8.

TDD: each test class written RED first, then the fix makes it GREEN.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
import zipfile
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

from build.artifacts import BuildStep, FakeRunner, RunResult, StepResult, run_steps
from build.orchestrate import build_all_artifacts
from build.release import (
    release_members,
    submodule_provenance,
    write_release_zip,
)
from abi.abi_spec import ArtifactKind

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_EPOCH = 1_700_000_000
_FAKE_TC = Path("/fake/toolchain")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers shared across tests
# ─────────────────────────────────────────────────────────────────────────────

def _make_fake_repo(tmp_path: Path) -> Path:
    """Minimal fake repo that mirrors the real layout, including mcu-firmware/."""
    repo = tmp_path / "repo"

    # ARM firmware out/ (simulates post-ARM-build, pre-host-clean state)
    (repo / "external" / "katapult" / "out").mkdir(parents=True)
    (repo / "external" / "klipper" / "out").mkdir(parents=True)
    (repo / "external" / "klipper" / "klippy" / "chelper").mkdir(parents=True)
    (repo / "external" / "mainsail-config").mkdir(parents=True)

    # Canonical captured locations (post-capture)
    (repo / "mcu-firmware").mkdir(parents=True)
    (repo / "mcu-firmware" / "klipper.bin").write_bytes(b"klipper-captured-bin")
    (repo / "mcu-firmware" / "klipper.dict").write_bytes(b"klipper-captured-dict")
    (repo / "mcu-firmware" / "klipper_mcu.elf").write_bytes(b"klipper-mips-elf")

    # external/klipper/out/ is left EMPTY to simulate post-host-clean state
    # (this is the key scenario these tests validate)

    # katapult stays in its out/ (not wiped by host build)
    (repo / "external" / "katapult" / "out" / "katapult.bin").write_bytes(b"katapult")

    # c_helper.so lives in chelper/ (not in out/, not wiped)
    (repo / "external" / "klipper" / "klippy" / "chelper" / "c_helper.so").write_bytes(b"so")

    # v3ke CLI binaries — both platforms
    (repo / "tools" / "v3ke").mkdir(parents=True)
    (repo / "tools" / "v3ke" / "v3ke").write_bytes(b"nim binary")
    (repo / "tools" / "v3ke" / "v3ke.exe").write_bytes(b"nim binary windows")

    # License files
    (repo / "LICENSE").write_text("repo license")
    (repo / "external" / "katapult" / "LICENSE").write_text("katapult license")
    (repo / "external" / "klipper" / "COPYING").write_text("klipper license")
    (repo / "external" / "mainsail-config" / "LICENSE").write_text("mainsail license")

    # Static release assets
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
# C1: firmware/klipper.bin must come from mcu-firmware/, not external/klipper/out/
# ─────────────────────────────────────────────────────────────────────────────

class TestC1KlipperBinSourcedFromCapture:
    """C1: release_members must source firmware/klipper.bin from mcu-firmware/klipper.bin.

    The canonical captured copy is in mcu-firmware/; external/klipper/out/ is wiped
    by the host klipper_mcu make clean and must never be used as a release source.
    """

    def test_klipper_bin_source_path_is_mcu_firmware(self, tmp_path):
        """release_members() must point firmware/klipper.bin at mcu-firmware/, not out/."""
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="v0.1.0")
        klipper_bin_srcs = [src for src, arc in members if arc == "firmware/klipper.bin"]
        assert klipper_bin_srcs, "firmware/klipper.bin not found in release_members"
        src = klipper_bin_srcs[0]
        assert "mcu-firmware" in src.parts, (
            f"firmware/klipper.bin source must be under mcu-firmware/, got: {src}"
        )
        assert "external" not in src.parts or "out" not in src.parts, (
            f"firmware/klipper.bin must NOT come from external/klipper/out/: {src}"
        )

    def test_release_zip_klipper_bin_from_captured_location(self, tmp_path):
        """write_release_zip must package klipper.bin from mcu-firmware/ even when out/ is empty."""
        bundle_path, repo = _write_zip(tmp_path)
        # Verify out/ is empty (no klipper.bin there)
        assert not (repo / "external" / "klipper" / "out" / "klipper.bin").exists()
        # The bundle must still contain firmware/klipper.bin
        with tarfile.open(bundle_path, "r:xz") as tf:
            assert "firmware/klipper.bin" in tf.getnames(), (
                "firmware/klipper.bin must be present in bundle even when out/ is empty"
            )
            content = tf.extractfile("firmware/klipper.bin").read()
        assert content == b"klipper-captured-bin", (
            f"firmware/klipper.bin content should come from mcu-firmware/, got: {content!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# C2: klipper.dict must be captured to mcu-firmware/ and sourced from there
# ─────────────────────────────────────────────────────────────────────────────

class TestC2KlipperDictCaptureAndSource:
    """C2: klipper.dict needs a capture step in orchestrate.py and must be sourced
    from mcu-firmware/ in release_members(), not from the wiped external/klipper/out/.
    """

    def test_klipper_dict_source_path_is_mcu_firmware(self, tmp_path):
        """release_members() must point host/klipper.dict at mcu-firmware/, not out/."""
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="v0.1.0")
        dict_srcs = [src for src, arc in members if arc == "host/klipper.dict"]
        assert dict_srcs, "host/klipper.dict not found in release_members"
        src = dict_srcs[0]
        assert "mcu-firmware" in src.parts, (
            f"host/klipper.dict source must be under mcu-firmware/, got: {src}"
        )

    def test_release_zip_klipper_dict_from_captured_location(self, tmp_path):
        """write_release_zip must package klipper.dict from mcu-firmware/ even when out/ is empty."""
        bundle_path, repo = _write_zip(tmp_path)
        assert not (repo / "external" / "klipper" / "out" / "klipper.dict").exists()
        with tarfile.open(bundle_path, "r:xz") as tf:
            assert "host/klipper.dict" in tf.getnames(), (
                "host/klipper.dict must be present in bundle even when out/ is empty"
            )
            content = tf.extractfile("host/klipper.dict").read()
        assert content == b"klipper-captured-dict", (
            f"host/klipper.dict content should come from mcu-firmware/, got: {content!r}"
        )

    def test_dict_capture_step_exists_in_orchestrate(self):
        """build_all_artifacts must include a dict-capture step (klipper-dict-capture)."""
        from unittest.mock import patch
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

        steps = recorded[0]
        capture_names = [s.name for s in steps]
        assert "klipper-dict-capture" in capture_names, (
            f"Expected 'klipper-dict-capture' step in orchestrate output; got: {capture_names}"
        )

    def test_dict_capture_before_klipper_mcu_clean(self):
        """klipper-dict-capture must precede klipper-mcu-clean (host clean wipes out/)."""
        from unittest.mock import patch
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

        steps = recorded[0]
        names = [s.name for s in steps]
        assert "klipper-dict-capture" in names, "klipper-dict-capture step missing"
        assert "klipper-mcu-clean" in names, "klipper-mcu-clean step missing"
        assert names.index("klipper-dict-capture") < names.index("klipper-mcu-clean"), (
            "klipper-dict-capture must precede klipper-mcu-clean"
        )

    def test_dict_capture_copies_from_out_to_mcu_firmware(self):
        """The dict-capture step must cp klipper.dict from out/ to mcu-firmware/."""
        from unittest.mock import patch
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

        steps = recorded[0]
        capture = next((s for s in steps if s.name == "klipper-dict-capture"), None)
        assert capture is not None, "klipper-dict-capture step not found"
        assert capture.cmd[0] == "cp", f"dict-capture must use cp, got: {capture.cmd[0]!r}"
        src = capture.cmd[-2]
        dst = capture.cmd[-1]
        assert "klipper.dict" in src, f"cp source must be klipper.dict, got: {src!r}"
        assert "mcu-firmware" in dst, f"cp destination must be mcu-firmware/, got: {dst!r}"


# ─────────────────────────────────────────────────────────────────────────────
# C-elf: host/klipper.elf must be MIPS host ELF from mcu-firmware/klipper_mcu.elf
# ─────────────────────────────────────────────────────────────────────────────

class TestCElfHostKlipperElfIsMips:
    """C-elf: release_members() must source host/klipper.elf from a captured location
    under mcu-firmware/ (klipper_mcu.elf), not from external/klipper/out/ which
    may be absent at packaging time. The MIPS elf is the host-MCU binary produced
    by klipper_mcu_steps, NOT the ARM klipper.elf.
    """

    def test_klipper_elf_source_is_mcu_firmware(self, tmp_path):
        """release_members() must point host/klipper.elf at mcu-firmware/, not out/."""
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="v0.1.0")
        elf_srcs = [src for src, arc in members if arc == "host/klipper.elf"]
        assert elf_srcs, "host/klipper.elf not found in release_members"
        src = elf_srcs[0]
        assert "mcu-firmware" in src.parts, (
            f"host/klipper.elf source must be under mcu-firmware/, got: {src}"
        )
        assert "external" not in src.parts, (
            f"host/klipper.elf must NOT come from external/ (stale out/ path), got: {src}"
        )

    def test_klipper_elf_filename_reflects_mips_origin(self, tmp_path):
        """The canonical captured MIPS elf should be named klipper_mcu.elf (not klipper.elf)."""
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="v0.1.0")
        elf_srcs = [src for src, arc in members if arc == "host/klipper.elf"]
        assert elf_srcs
        src = elf_srcs[0]
        # The source filename in mcu-firmware/ should be klipper_mcu.elf
        assert src.name == "klipper_mcu.elf", (
            f"The MIPS host elf canonical source should be klipper_mcu.elf, got: {src.name}"
        )

    def test_release_zip_klipper_elf_from_captured_location(self, tmp_path):
        """write_release_zip must package host/klipper.elf from mcu-firmware/ even when out/ is empty."""
        bundle_path, repo = _write_zip(tmp_path)
        assert not (repo / "external" / "klipper" / "out" / "klipper.elf").exists()
        with tarfile.open(bundle_path, "r:xz") as tf:
            assert "host/klipper.elf" in tf.getnames(), (
                "host/klipper.elf must be present in bundle even when out/ is empty"
            )
            content = tf.extractfile("host/klipper.elf").read()
        assert content == b"klipper-mips-elf", (
            f"host/klipper.elf content should come from mcu-firmware/klipper_mcu.elf, got: {content!r}"
        )

    def test_elf_capture_step_exists_in_orchestrate(self):
        """build_all_artifacts must include a klipper-elf-capture step."""
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

        steps = recorded[0]
        names = [s.name for s in steps]
        assert "klipper-elf-capture" in names, (
            f"Expected 'klipper-elf-capture' step; got: {names}"
        )

    def test_elf_capture_after_klipper_mcu_build(self):
        """klipper-elf-capture must come AFTER klipper-mcu-build (capture the output)."""
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

        steps = recorded[0]
        names = [s.name for s in steps]
        assert "klipper-elf-capture" in names
        assert "klipper-mcu-build" in names
        assert names.index("klipper-mcu-build") < names.index("klipper-elf-capture"), (
            "klipper-elf-capture must come after klipper-mcu-build"
        )


# ─────────────────────────────────────────────────────────────────────────────
# H1: run_steps must fail (ok=False) when output_path missing or ELF malformed
# ─────────────────────────────────────────────────────────────────────────────

class TestH1RunStepsFailsOnMissingOrMalformedArtifact:
    """H1: run_steps reports ok=False when a step's expected output_path is absent
    or when ELF parsing raised MalformedElfError (currently both are silent-green).
    """

    def test_missing_output_path_causes_failure(self, tmp_path):
        """A step with returncode=0 but missing output_path must report ok=False."""
        missing = tmp_path / "nonexistent.elf"
        step = BuildStep(
            name="test-step",
            cmd=["true"],
            output_path=missing,
            kind=ArtifactKind.EXECUTABLE,
        )
        runner = FakeRunner()  # always returns rc=0
        with pytest.raises(RuntimeError) as exc_info:
            run_steps([step], runner)
        results = exc_info.value.results
        assert len(results) == 1
        assert results[0].ok is False, (
            "Step with missing output_path must have ok=False even if returncode=0"
        )
        assert "missing" in results[0].detail.lower() or "not found" in results[0].detail.lower() or "absent" in results[0].detail.lower(), (
            f"detail should mention missing/absent/not found, got: {results[0].detail!r}"
        )

    def test_malformed_elf_causes_failure(self, tmp_path):
        """A step with returncode=0 but a malformed ELF output must report ok=False."""
        bad_elf = tmp_path / "bad.elf"
        bad_elf.write_bytes(b"this is not a valid ELF file")
        step = BuildStep(
            name="test-step",
            cmd=["true"],
            output_path=bad_elf,
            kind=ArtifactKind.EXECUTABLE,
        )
        runner = FakeRunner()
        with pytest.raises(RuntimeError) as exc_info:
            run_steps([step], runner)
        results = exc_info.value.results
        assert len(results) == 1
        assert results[0].ok is False, (
            "Step with malformed ELF output must have ok=False even if returncode=0"
        )

    def test_raw_firmware_missing_output_path_still_fails(self, tmp_path):
        """A RAW_FIRMWARE step that declares an output_path must fail if the file is absent."""
        missing = tmp_path / "nonexistent.bin"
        step = BuildStep(
            name="fw-step",
            cmd=["true"],
            output_path=missing,
            kind=ArtifactKind.RAW_FIRMWARE,
        )
        runner = FakeRunner()
        with pytest.raises(RuntimeError) as exc_info:
            run_steps([step], runner)
        results = exc_info.value.results
        assert results[0].ok is False, (
            "RAW_FIRMWARE step with missing output_path must also fail"
        )

    def test_none_output_path_skips_missing_check(self, tmp_path):
        """A step with output_path=None (no declared output) must succeed even with FakeRunner."""
        step = BuildStep(
            name="side-effect-step",
            cmd=["true"],
            output_path=None,  # type: ignore[arg-type]
            kind=ArtifactKind.RAW_FIRMWARE,
        )
        runner = FakeRunner()
        results = run_steps([step], runner)
        assert len(results) == 1
        assert results[0].ok is True

    def test_present_elf_with_valid_content_passes(self, tmp_path):
        """If we somehow had a valid ELF (hard to generate), rc=0 + file exists = ok.
        We test the negative: rc=0 + file exists but is garbage = fail (see above).
        And rc=0 + file missing = fail (see above).
        For RAW_FIRMWARE: rc=0 + file exists = ok (no ABI check).
        """
        real_bin = tmp_path / "present.bin"
        real_bin.write_bytes(b"firmware")
        step = BuildStep(
            name="fw-step",
            cmd=["true"],
            output_path=real_bin,
            kind=ArtifactKind.RAW_FIRMWARE,
        )
        runner = FakeRunner()
        results = run_steps([step], runner)
        assert results[0].ok is True


# ─────────────────────────────────────────────────────────────────────────────
# M5: manifest not smuggled through release_members as a sentinel path
# ─────────────────────────────────────────────────────────────────────────────

class TestM5ManifestNotInReleaseMembers:
    """M5: release_members() should not include manifest.json as a sentinel entry.
    The manifest must be appended directly in write_release_zip instead.
    """

    def test_release_members_does_not_include_manifest_sentinel(self, tmp_path):
        """release_members() must NOT include manifest.json in its returned list."""
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="v0.1.0")
        manifest_entries = [(src, arc) for src, arc in members if arc == "manifest.json"]
        assert not manifest_entries, (
            f"release_members() must not include manifest.json sentinel; got: {manifest_entries}"
        )

    def test_write_release_zip_still_contains_manifest(self, tmp_path):
        """write_release_zip must still write manifest.json to the linux bundle."""
        bundle_path, _ = _write_zip(tmp_path)
        with tarfile.open(bundle_path, "r:xz") as tf:
            assert "manifest.json" in tf.getnames(), (
                "manifest.json must still be in the bundle after M5 refactor"
            )

    def test_manifest_in_zip_is_valid_json(self, tmp_path):
        """manifest.json in the bundle must be valid JSON with _type field."""
        bundle_path, _ = _write_zip(tmp_path)
        with tarfile.open(bundle_path, "r:xz") as tf:
            manifest = json.loads(tf.extractfile("manifest.json").read())
        assert manifest["_type"] == "v3ke-build"

    def test_manifest_validates_against_schema(self, tmp_path):
        """manifest.json in the bundle must pass schema validation."""
        from build.release import validate_manifest
        bundle_path, _ = _write_zip(tmp_path)
        with tarfile.open(bundle_path, "r:xz") as tf:
            manifest = json.loads(tf.extractfile("manifest.json").read())
        validate_manifest(manifest)  # must not raise


# ─────────────────────────────────────────────────────────────────────────────
# M6: submodule_provenance must accept an injectable runner
# ─────────────────────────────────────────────────────────────────────────────

class TestM6SubmoduleProvenanceInjectableRunner:
    """M6: submodule_provenance must accept a runner= parameter so it can be tested
    without a real git repo.
    """

    def test_fake_runner_returns_expected_provenance(self):
        """A fake runner can supply canned git output without a real repo."""
        fake_responses = {
            "submodule.external/klipper.url":         "https://github.com/Klipper3d/klipper.git",
            "submodule.external/katapult.url":        "https://github.com/Arksine/katapult.git",
            "submodule.external/mainsail-config.url": "https://github.com/mainsail-crew/mainsail-config.git",
            "HEAD:external/klipper":         "e60fe3d99b545d7e42ff2f5278efa5822668a57c",
            "HEAD:external/katapult":        "b0bf421069e2aab810db43d6e15f38817d981451",
            "HEAD:external/mainsail-config": "ff3869a621db17ce3ef660adbbd3fa321995ac42",
        }

        def fake_runner(cmd: list[str]) -> str:
            # git config --file .gitmodules submodule.<path>.url
            if "config" in cmd and "--file" in cmd:
                key = cmd[-1]  # last arg is the key
                return fake_responses[key] + "\n"
            # git rev-parse HEAD:<path>
            if "rev-parse" in cmd:
                ref = cmd[-1]  # e.g. "HEAD:external/klipper"
                return fake_responses[ref] + "\n"
            raise ValueError(f"Unexpected cmd in fake_runner: {cmd}")

        result = submodule_provenance(Path("/fake/repo"), runner=fake_runner)
        assert set(result.keys()) == {"klipper", "katapult", "mainsail-config"}
        assert result["klipper"]["commit"] == "e60fe3d99b545d7e42ff2f5278efa5822668a57c"
        assert result["katapult"]["url"] == "https://github.com/Arksine/katapult.git"
        assert result["mainsail-config"]["commit"] == "ff3869a621db17ce3ef660adbbd3fa321995ac42"

    def test_default_runner_uses_real_repo(self):
        """Without a runner arg, submodule_provenance still reads the real repo."""
        result = submodule_provenance(_REPO_ROOT)
        assert result["klipper"]["commit"] == "e60fe3d99b545d7e42ff2f5278efa5822668a57c"


# ─────────────────────────────────────────────────────────────────────────────
# M8: BuildStep.output_path uses Optional[Path] = None, not Path("")
# ─────────────────────────────────────────────────────────────────────────────

class TestM8BuildStepOutputPathOptional:
    """M8: BuildStep.output_path must be Optional[Path] = None, not Path("") sentinel."""

    def test_build_step_output_path_default_is_none(self):
        """A BuildStep constructed without output_path should default to None."""
        # If the dataclass has a default, this works; otherwise it raises TypeError.
        # The point is the field type is Optional[Path] and None is the sentinel.
        step = BuildStep(
            name="no-output",
            cmd=["make", "clean"],
            output_path=None,
            kind=ArtifactKind.RAW_FIRMWARE,
        )
        assert step.output_path is None, (
            f"output_path=None must be accepted; got {step.output_path!r}"
        )

    def test_no_output_step_is_none_not_empty_path(self):
        """Side-effect-only steps (clean) must use output_path=None, not Path('')."""
        step = BuildStep(
            name="clean",
            cmd=["make", "clean"],
            output_path=None,
            kind=ArtifactKind.RAW_FIRMWARE,
        )
        assert step.output_path is None
        assert step.output_path != Path(""), (
            "output_path=None must not equal Path('') — they are distinct sentinels"
        )

    def test_clean_steps_use_none_not_empty_path(self):
        """All clean steps in arm_mcu_steps must have output_path=None."""
        from build.arm_mcu import arm_mcu_steps
        steps = arm_mcu_steps(_REPO_ROOT, _EPOCH)
        clean_steps = [s for s in steps if s.name.endswith("-clean")]
        assert clean_steps, "No clean steps found"
        for s in clean_steps:
            assert s.output_path is None, (
                f"Clean step '{s.name}' must have output_path=None, got {s.output_path!r}"
            )

    def test_olddefconfig_steps_use_none(self):
        """All olddefconfig steps must have output_path=None."""
        from build.arm_mcu import arm_mcu_steps
        steps = arm_mcu_steps(_REPO_ROOT, _EPOCH)
        olddefconfig_steps = [s for s in steps if "olddefconfig" in s.name]
        assert olddefconfig_steps, "No olddefconfig steps found"
        for s in olddefconfig_steps:
            assert s.output_path is None, (
                f"olddefconfig step '{s.name}' must have output_path=None, got {s.output_path!r}"
            )

    def test_run_steps_handles_none_output_path(self, tmp_path):
        """run_steps must handle output_path=None without errors (no missing-file check)."""
        step = BuildStep(
            name="side-effect",
            cmd=["true"],
            output_path=None,
            kind=ArtifactKind.RAW_FIRMWARE,
        )
        results = run_steps([step], FakeRunner())
        assert results[0].ok is True

    def test_empty_path_no_longer_used_in_makesteps(self):
        """_makesteps.py must use None, not Path(''), for side-effect steps."""
        from build._makesteps import make_steps
        from pathlib import Path
        steps = make_steps(
            name_prefix="test",
            subproject_dir=Path("/fake"),
            kconfig_path=Path("/fake/config"),
            output_path=Path("/fake/out.elf"),
            kind=ArtifactKind.EXECUTABLE,
            repo_root=Path("/fake"),
            epoch=_EPOCH,
        )
        clean, olddefconfig, build = steps
        assert clean.output_path is None, f"clean step output_path must be None, got {clean.output_path!r}"
        assert olddefconfig.output_path is None, f"olddefconfig step output_path must be None, got {olddefconfig.output_path!r}"
        assert build.output_path == Path("/fake/out.elf")
