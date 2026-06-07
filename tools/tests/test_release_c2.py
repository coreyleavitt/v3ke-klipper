"""C2 unit tests — release zip + manifest pipeline.

Slice C2: pure Python, offline. No container, no real git tag required.

TDD order (each RED→GREEN):
  C2-1:  resolve_version happy path
  C2-2:  resolve_version loud failure (empty / non-v* / nonzero)
  C2-3:  release_zip_name
  C2-4:  build_manifest shape
  C2-5:  validate_manifest valid passes
  C2-6:  validate_manifest invalid fails (missing field / bad sha256)
  C2-7:  submodule_provenance (real repo)
  C2-8:  hash_artifact
  C2-9:  release_members plan
  C2-10: emit_versions (ct_build)
  C2-11: write_release_zip (temp fake repo — hermetic, offline)
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from build.release import (
    ReleaseError,
    build_manifest,
    hash_artifact,
    release_members,
    release_zip_name,
    resolve_version,
    submodule_provenance,
    validate_manifest,
    write_release_zip,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_EPOCH = 1_700_000_000  # fixed, no git required


# ──────────────────────────────────────────────────────────────────────────────
# C2-1: resolve_version — happy path
# ──────────────────────────────────────────────────────────────────────────────

class TestResolveVersionHappy:
    """resolve_version returns the stripped tag string when git describe succeeds."""

    def test_returns_version_string(self):
        def fake_runner(cmd):
            return "v0.1.0-3-gabcdef012345\n"

        result = resolve_version(Path("/fake/repo"), runner=fake_runner)
        assert result == "v0.1.0-3-gabcdef012345"

    def test_strips_trailing_newline(self):
        def fake_runner(cmd):
            return "v1.2.3\n"

        result = resolve_version(Path("/fake/repo"), runner=fake_runner)
        assert result == "v1.2.3"

    def test_passes_correct_git_describe_cmd(self):
        captured = []

        def fake_runner(cmd):
            captured.append(cmd)
            return "v0.1.0\n"

        resolve_version(Path("/the/repo"), runner=fake_runner)
        assert captured[0] == [
            "git", "-C", "/the/repo", "describe", "--match", "v*", "--abbrev=12"
        ]


# ──────────────────────────────────────────────────────────────────────────────
# C2-2: resolve_version — loud failure
# ──────────────────────────────────────────────────────────────────────────────

class TestResolveVersionFailure:
    """resolve_version raises ReleaseError for empty / non-v* / runner error."""

    def test_raises_on_empty_output(self):
        def fake_runner(cmd):
            return ""

        with pytest.raises(ReleaseError, match="git tag"):
            resolve_version(Path("/repo"), runner=fake_runner)

    def test_raises_on_whitespace_only(self):
        def fake_runner(cmd):
            return "  \n"

        with pytest.raises(ReleaseError):
            resolve_version(Path("/repo"), runner=fake_runner)

    def test_raises_on_non_v_prefix(self):
        def fake_runner(cmd):
            return "0.1.0\n"

        with pytest.raises(ReleaseError, match="v\\*"):
            resolve_version(Path("/repo"), runner=fake_runner)

    def test_raises_on_runner_exception(self):
        def fake_runner(cmd):
            raise RuntimeError("git not found")

        with pytest.raises(ReleaseError):
            resolve_version(Path("/repo"), runner=fake_runner)

    def test_error_message_mentions_bootstrap(self):
        """The error message must name the one-time bootstrap step."""
        def fake_runner(cmd):
            return ""

        with pytest.raises(ReleaseError) as exc_info:
            resolve_version(Path("/repo"), runner=fake_runner)
        msg = str(exc_info.value)
        assert "git tag" in msg


# ──────────────────────────────────────────────────────────────────────────────
# C2-3: release_zip_name
# ──────────────────────────────────────────────────────────────────────────────

class TestReleaseZipName:
    def test_basic_version(self):
        assert release_zip_name("v0.1.0") == "v3ke-v0.1.0-linux-amd64.zip"

    def test_with_git_describe_suffix(self):
        assert release_zip_name("v0.1.0-3-gabcdef012345") == "v3ke-v0.1.0-3-gabcdef012345-linux-amd64.zip"


# ──────────────────────────────────────────────────────────────────────────────
# C2-4: build_manifest shape
# ──────────────────────────────────────────────────────────────────────────────

_SAMPLE_TOOLCHAIN = {
    "mips": {"glibc": "2.29", "gcc": "8.5.0", "binutils": "2.32", "linux": "4.14.329", "glibc_min_kernel": "4.4.0"},
    "arm":  {"gcc": "14.3.0"},
}

_SAMPLE_SUBMODULES = {
    "klipper":       {"url": "https://github.com/Klipper3d/klipper.git",          "commit": "e60fe3d99b545d7e42ff2f5278efa5822668a57c"},
    "katapult":      {"url": "https://github.com/Arksine/katapult.git",           "commit": "b0bf421069e2aab810db43d6e15f38817d981451"},
    "mainsail-config": {"url": "https://github.com/mainsail-crew/mainsail-config.git", "commit": "ff3869a621db17ce3ef660adbbd3fa321995ac42"},
}

_SAMPLE_ARTIFACT = {
    "name": "katapult.bin",
    "path": "firmware/katapult.bin",
    "sha256": "a" * 64,
    "size": 1024,
}


class TestBuildManifestShape:
    """build_manifest assembles the correct structure from pure inputs."""

    def _make(self, **overrides):
        kwargs = dict(
            version="v0.1.0",
            commit="deadbeef" * 5,
            source_date_epoch=_EPOCH,
            toolchain=_SAMPLE_TOOLCHAIN,
            submodules=_SAMPLE_SUBMODULES,
            artifacts=[_SAMPLE_ARTIFACT],
            reproducible=False,
        )
        kwargs.update(overrides)
        return build_manifest(**kwargs)

    def test_type_field(self):
        m = self._make()
        assert m["_type"] == "v3ke-build"

    def test_schema_version(self):
        m = self._make()
        assert m["schema_version"] == "1"

    def test_build_id_is_version(self):
        m = self._make(version="v0.2.0")
        assert m["build"]["id"] == "v0.2.0"

    def test_build_commit(self):
        m = self._make(commit="abc123" * 6 + "ab")
        assert m["build"]["commit"] == "abc123" * 6 + "ab"

    def test_timestamp_from_epoch_not_wall_clock(self):
        m = self._make(source_date_epoch=_EPOCH)
        # Must contain ISO-8601 UTC derived from _EPOCH, not wall-clock
        ts = m["build"]["timestamp"]
        assert "2023" in ts  # epoch 1_700_000_000 is in Nov 2023
        assert "T" in ts     # ISO-8601 separator

    def test_reproducible_bool(self):
        m_false = self._make(reproducible=False)
        m_true  = self._make(reproducible=True)
        assert m_false["build"]["reproducible"] is False
        assert m_true["build"]["reproducible"] is True

    def test_toolchain_nested(self):
        m = self._make()
        assert m["build"]["toolchain"]["mips"]["glibc"] == "2.29"
        assert m["build"]["toolchain"]["arm"]["gcc"] == "14.3.0"

    def test_sources_keys(self):
        m = self._make()
        assert set(m["sources"].keys()) == {"klipper", "katapult", "mainsail-config"}

    def test_sources_have_url_and_commit(self):
        m = self._make()
        for name, entry in m["sources"].items():
            assert "url" in entry
            assert "commit" in entry

    def test_artifacts_list(self):
        m = self._make()
        assert isinstance(m["artifacts"], list)
        assert len(m["artifacts"]) == 1
        assert m["artifacts"][0]["name"] == "katapult.bin"


# ──────────────────────────────────────────────────────────────────────────────
# C2-5: validate_manifest — valid passes
# ──────────────────────────────────────────────────────────────────────────────

def _valid_manifest() -> dict:
    return {
        "_type": "v3ke-build",
        "schema_version": "1",
        "build": {
            "id": "v0.1.0",
            "commit": "abc" * 13 + "a",
            "timestamp": "2023-11-14T22:13:20+00:00",
            "reproducible": False,
            "toolchain": {
                "mips": {"glibc": "2.29", "gcc": "8.5.0", "binutils": "2.32",
                         "linux": "4.14.329", "glibc_min_kernel": "4.4.0"},
                "arm":  {"gcc": "14.3.0"},
            },
        },
        "sources": {
            "klipper":         {"url": "https://github.com/Klipper3d/klipper.git",          "commit": "e60fe3d"},
            "katapult":        {"url": "https://github.com/Arksine/katapult.git",           "commit": "b0bf421"},
            "mainsail-config": {"url": "https://github.com/mainsail-crew/mainsail-config.git", "commit": "ff3869a"},
        },
        "artifacts": [
            {"name": "katapult.bin", "path": "firmware/katapult.bin",
             "sha256": "a" * 64, "size": 1024},
        ],
    }


class TestValidateManifestValid:
    def test_valid_manifest_passes(self):
        validate_manifest(_valid_manifest())  # must not raise

    def test_returns_none(self):
        result = validate_manifest(_valid_manifest())
        assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# C2-6: validate_manifest — invalid fails
# ──────────────────────────────────────────────────────────────────────────────

class TestValidateManifestInvalid:
    def test_missing_type_field_raises(self):
        m = _valid_manifest()
        del m["_type"]
        with pytest.raises(Exception):
            validate_manifest(m)

    def test_wrong_type_const_raises(self):
        m = _valid_manifest()
        m["_type"] = "wrong"
        with pytest.raises(Exception):
            validate_manifest(m)

    def test_missing_build_raises(self):
        m = _valid_manifest()
        del m["build"]
        with pytest.raises(Exception):
            validate_manifest(m)

    def test_bad_sha256_pattern_raises(self):
        m = _valid_manifest()
        m["artifacts"][0]["sha256"] = "NOTAGOODHASH"
        with pytest.raises(Exception):
            validate_manifest(m)

    def test_sha256_too_short_raises(self):
        m = _valid_manifest()
        m["artifacts"][0]["sha256"] = "a" * 63
        with pytest.raises(Exception):
            validate_manifest(m)

    def test_missing_sources_commit_raises(self):
        m = _valid_manifest()
        del m["sources"]["klipper"]["commit"]
        with pytest.raises(Exception):
            validate_manifest(m)

    def test_artifact_negative_size_raises(self):
        m = _valid_manifest()
        m["artifacts"][0]["size"] = -1
        with pytest.raises(Exception):
            validate_manifest(m)


# ──────────────────────────────────────────────────────────────────────────────
# C2-7: submodule_provenance (real repo)
# ──────────────────────────────────────────────────────────────────────────────

class TestSubmoduleProvenance:
    """submodule_provenance reads the real repo — deterministic from pinned submodules."""

    def test_returns_three_submodules(self):
        result = submodule_provenance(_REPO_ROOT)
        assert set(result.keys()) == {"klipper", "katapult", "mainsail-config"}

    def test_each_has_url_and_commit(self):
        result = submodule_provenance(_REPO_ROOT)
        for name, entry in result.items():
            assert "url" in entry, f"{name} missing url"
            assert "commit" in entry, f"{name} missing commit"

    def test_klipper_pinned_commit(self):
        result = submodule_provenance(_REPO_ROOT)
        assert result["klipper"]["commit"] == "e60fe3d99b545d7e42ff2f5278efa5822668a57c"

    def test_katapult_pinned_commit(self):
        result = submodule_provenance(_REPO_ROOT)
        assert result["katapult"]["commit"] == "b0bf421069e2aab810db43d6e15f38817d981451"

    def test_mainsail_config_pinned_commit(self):
        result = submodule_provenance(_REPO_ROOT)
        assert result["mainsail-config"]["commit"] == "ff3869a621db17ce3ef660adbbd3fa321995ac42"

    def test_klipper_url(self):
        result = submodule_provenance(_REPO_ROOT)
        assert "Klipper3d/klipper" in result["klipper"]["url"]


# ──────────────────────────────────────────────────────────────────────────────
# C2-8: hash_artifact
# ──────────────────────────────────────────────────────────────────────────────

class TestHashArtifact:
    def test_sha256_correct(self, tmp_path):
        p = tmp_path / "test.bin"
        data = b"hello world"
        p.write_bytes(data)
        expected_sha = hashlib.sha256(data).hexdigest()
        result = hash_artifact(p, "firmware/test.bin")
        assert result["sha256"] == expected_sha

    def test_size_correct(self, tmp_path):
        p = tmp_path / "test.bin"
        data = b"x" * 512
        p.write_bytes(data)
        result = hash_artifact(p, "firmware/test.bin")
        assert result["size"] == 512

    def test_name_is_basename(self, tmp_path):
        p = tmp_path / "katapult.bin"
        p.write_bytes(b"fw")
        result = hash_artifact(p, "firmware/katapult.bin")
        assert result["name"] == "katapult.bin"

    def test_path_is_arcname(self, tmp_path):
        p = tmp_path / "katapult.bin"
        p.write_bytes(b"fw")
        result = hash_artifact(p, "firmware/katapult.bin")
        assert result["path"] == "firmware/katapult.bin"

    def test_sha256_is_64_hex_chars(self, tmp_path):
        p = tmp_path / "f.bin"
        p.write_bytes(b"data")
        result = hash_artifact(p, "f.bin")
        assert len(result["sha256"]) == 64
        assert all(c in "0123456789abcdef" for c in result["sha256"])


# ──────────────────────────────────────────────────────────────────────────────
# C2-9: release_members plan
# ──────────────────────────────────────────────────────────────────────────────

def _make_fake_repo(tmp_path: Path) -> Path:
    """Build a minimal fake repo tree that mirrors the real layout.

    All release artifact source paths now point to mcu-firmware/ (the canonical
    captured locations), not external/klipper/out/ (which is wiped by the host
    make clean at build time).
    """
    repo = tmp_path / "repo"

    # Directories for artifacts that live outside mcu-firmware/
    (repo / "external" / "katapult" / "out").mkdir(parents=True)
    (repo / "external" / "klipper" / "out").mkdir(parents=True)   # normally wiped; empty here
    (repo / "external" / "klipper" / "klippy" / "chelper").mkdir(parents=True)
    (repo / "external" / "mainsail-config").mkdir(parents=True)

    # Canonical captured locations in mcu-firmware/
    (repo / "mcu-firmware").mkdir(parents=True)
    (repo / "mcu-firmware" / "klipper.bin").write_bytes(b"klipper")
    (repo / "mcu-firmware" / "klipper.dict").write_bytes(b"dict")
    (repo / "mcu-firmware" / "klipper_mcu.elf").write_bytes(b"elf")

    # Artifacts that are NOT in mcu-firmware/
    (repo / "external" / "katapult" / "out" / "katapult.bin").write_bytes(b"katapult")
    (repo / "external" / "klipper" / "klippy" / "chelper" / "c_helper.so").write_bytes(b"so")

    # v3ke binary
    (repo / "tools" / "v3ke").mkdir(parents=True)
    (repo / "tools" / "v3ke" / "v3ke").write_bytes(b"nim binary")

    # License files
    (repo / "LICENSE").write_text("repo license")
    (repo / "external" / "katapult" / "LICENSE").write_text("katapult license")
    (repo / "external" / "klipper" / "COPYING").write_text("klipper license")
    (repo / "external" / "mainsail-config" / "LICENSE").write_text("mainsail license")

    # Static release assets (would normally be in tools/build/release_assets)
    assets_src = Path(__file__).resolve().parent.parent / "build" / "release_assets"
    if assets_src.exists():
        import shutil
        shutil.copytree(str(assets_src), str(repo / "release_assets"))
    else:
        (repo / "release_assets").mkdir()
        (repo / "release_assets" / "INSTALL.md").write_text("install")
        (repo / "release_assets" / "SOURCES.md").write_text("sources")

    return repo


class TestReleaseMembers:
    """release_members returns the correct (source_path, arcname) plan."""

    def test_returns_list_of_tuples(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="v0.1.0")
        assert isinstance(members, list)
        for item in members:
            assert len(item) == 2, f"expected 2-tuple, got {item!r}"

    def test_contains_katapult_bin(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="v0.1.0")
        arcnames = [arcname for _, arcname in members]
        assert "firmware/katapult.bin" in arcnames

    def test_contains_klipper_bin(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="v0.1.0")
        arcnames = [arcname for _, arcname in members]
        assert "firmware/klipper.bin" in arcnames

    def test_contains_klipper_elf(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="v0.1.0")
        arcnames = [arcname for _, arcname in members]
        assert "host/klipper.elf" in arcnames

    def test_contains_klipper_dict(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="v0.1.0")
        arcnames = [arcname for _, arcname in members]
        assert "host/klipper.dict" in arcnames

    def test_contains_c_helper_so(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="v0.1.0")
        arcnames = [arcname for _, arcname in members]
        assert "host/c_helper.so" in arcnames

    def test_contains_v3ke_binary(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="v0.1.0")
        arcnames = [arcname for _, arcname in members]
        assert "v3ke" in arcnames

    def test_contains_install_md(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="v0.1.0")
        arcnames = [arcname for _, arcname in members]
        assert "INSTALL.md" in arcnames

    def test_contains_sources_md(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="v0.1.0")
        arcnames = [arcname for _, arcname in members]
        assert "SOURCES.md" in arcnames

    def test_contains_license_files(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="v0.1.0")
        arcnames = [arcname for _, arcname in members]
        # At least the repo root license and one external license
        license_arcnames = [a for a in arcnames if a.startswith("LICENSES/")]
        assert len(license_arcnames) >= 2

    def test_manifest_json_not_in_plan(self, tmp_path):
        """manifest.json must NOT be in release_members() — it is appended by write_release_zip."""
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="v0.1.0")
        arcnames = [arcname for _, arcname in members]
        assert "manifest.json" not in arcnames, (
            "manifest.json must not appear in release_members() — appended directly by write_release_zip"
        )

    def test_source_paths_are_path_objects(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="v0.1.0")
        for src, _ in members:
            assert isinstance(src, Path), f"expected Path, got {type(src)}"


# ──────────────────────────────────────────────────────────────────────────────
# C2-10: emit_versions (ct_build)
# ──────────────────────────────────────────────────────────────────────────────

class TestEmitVersions:
    """emit_versions() returns the correct pinned toolchain versions."""

    def test_import_and_call(self):
        mod = self._load_ct_build()
        result = mod.emit_versions()
        assert isinstance(result, dict)

    def _load_ct_build(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "ct_build",
            _REPO_ROOT / "toolchain" / "ct_build.py",
        )
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

    def test_mips_glibc(self):
        mod = self._load_ct_build()
        result = mod.emit_versions()
        assert result["mips"]["glibc"] == "2.29"

    def test_mips_gcc(self):
        mod = self._load_ct_build()
        result = mod.emit_versions()
        assert result["mips"]["gcc"] == "8.5.0"

    def test_mips_binutils(self):
        mod = self._load_ct_build()
        result = mod.emit_versions()
        assert result["mips"]["binutils"] == "2.32"

    def test_arm_gcc(self):
        mod = self._load_ct_build()
        result = mod.emit_versions()
        assert result["arm"]["gcc"] == "14.3.0"

    def test_both_targets_present(self):
        mod = self._load_ct_build()
        result = mod.emit_versions()
        assert "mips" in result
        assert "arm" in result


# ──────────────────────────────────────────────────────────────────────────────
# C2-11: write_release_zip (fake repo, hermetic, offline)
# ──────────────────────────────────────────────────────────────────────────────

_SAMPLE_TOOLCHAIN_VERSIONS = {
    "mips": {"glibc": "2.29", "gcc": "8.5.0", "binutils": "2.32",
             "linux": "4.14.329", "glibc_min_kernel": "4.4.0"},
    "arm":  {"gcc": "14.3.0"},
}

_FAKE_SUBMODULES = {
    "klipper":         {"url": "https://github.com/Klipper3d/klipper.git",          "commit": "e60fe3d99b545d7e42ff2f5278efa5822668a57c"},
    "katapult":        {"url": "https://github.com/Arksine/katapult.git",           "commit": "b0bf421069e2aab810db43d6e15f38817d981451"},
    "mainsail-config": {"url": "https://github.com/mainsail-crew/mainsail-config.git", "commit": "ff3869a621db17ce3ef660adbbd3fa321995ac42"},
}


def _fake_submodule_provenance(_repo_root):
    """Stub: returns hardcoded provenance, no git calls needed."""
    return _FAKE_SUBMODULES


class TestWriteReleaseZip:
    """write_release_zip writes a zip + manifest.json validated against the schema."""

    def _write_zip(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        out_dir = tmp_path / "dist"
        out_dir.mkdir()
        zip_path = write_release_zip(
            repo_root=repo,
            out_dir=out_dir,
            version="v0.1.0",
            commit="deadbeef" * 5,
            source_date_epoch=_EPOCH,
            toolchain=_SAMPLE_TOOLCHAIN_VERSIONS,
            reproducible=False,
            _submodule_provenance=_fake_submodule_provenance,
        )
        return zip_path, repo

    def test_returns_path(self, tmp_path):
        zip_path, _ = self._write_zip(tmp_path)
        assert isinstance(zip_path, Path)

    def test_zip_file_exists(self, tmp_path):
        zip_path, _ = self._write_zip(tmp_path)
        assert zip_path.exists()

    def test_zip_name_matches_convention(self, tmp_path):
        zip_path, _ = self._write_zip(tmp_path)
        assert zip_path.name == "v3ke-v0.1.0-linux-amd64.zip"

    def test_zip_contains_install_md(self, tmp_path):
        zip_path, _ = self._write_zip(tmp_path)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        assert "INSTALL.md" in names

    def test_zip_contains_sources_md(self, tmp_path):
        zip_path, _ = self._write_zip(tmp_path)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        assert "SOURCES.md" in names

    def test_zip_contains_manifest(self, tmp_path):
        zip_path, _ = self._write_zip(tmp_path)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        assert "manifest.json" in names

    def test_zip_contains_firmware_artifacts(self, tmp_path):
        zip_path, _ = self._write_zip(tmp_path)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        assert "firmware/katapult.bin" in names
        assert "firmware/klipper.bin" in names

    def test_zip_contains_host_artifacts(self, tmp_path):
        zip_path, _ = self._write_zip(tmp_path)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        assert "host/c_helper.so" in names
        assert "host/klipper.elf" in names
        assert "host/klipper.dict" in names

    def test_zip_contains_v3ke(self, tmp_path):
        zip_path, _ = self._write_zip(tmp_path)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        assert "v3ke" in names

    def test_manifest_is_valid_json(self, tmp_path):
        zip_path, _ = self._write_zip(tmp_path)
        with zipfile.ZipFile(zip_path) as zf:
            manifest_bytes = zf.read("manifest.json")
        manifest = json.loads(manifest_bytes)
        assert manifest["_type"] == "v3ke-build"

    def test_manifest_validates_against_schema(self, tmp_path):
        zip_path, _ = self._write_zip(tmp_path)
        with zipfile.ZipFile(zip_path) as zf:
            manifest = json.loads(zf.read("manifest.json"))
        # Must not raise
        validate_manifest(manifest)

    def test_manifest_artifact_sha256_correct(self, tmp_path):
        zip_path, repo = self._write_zip(tmp_path)
        with zipfile.ZipFile(zip_path) as zf:
            manifest = json.loads(zf.read("manifest.json"))

        # Verify katapult.bin sha256
        real_katapult = repo / "external" / "katapult" / "out" / "katapult.bin"
        expected_sha = hashlib.sha256(real_katapult.read_bytes()).hexdigest()
        katapult_entry = next(
            a for a in manifest["artifacts"] if a["name"] == "katapult.bin"
        )
        assert katapult_entry["sha256"] == expected_sha

    def test_manifest_has_all_expected_artifact_names(self, tmp_path):
        zip_path, _ = self._write_zip(tmp_path)
        with zipfile.ZipFile(zip_path) as zf:
            manifest = json.loads(zf.read("manifest.json"))
        names = {a["name"] for a in manifest["artifacts"]}
        paths = {a["path"] for a in manifest["artifacts"]}
        assert "katapult.bin" in names
        assert "klipper.bin" in names
        assert "c_helper.so" in names
        # The MIPS host elf is sourced from klipper_mcu.elf (canonical name)
        # but packaged as host/klipper.elf in the archive.
        assert "klipper_mcu.elf" in names, (
            f"Manifest must reference klipper_mcu.elf as the host ELF artifact name; got: {names}"
        )
        assert "host/klipper.elf" in paths, (
            f"Manifest must have host/klipper.elf as archive path; got: {paths}"
        )
        assert "klipper.dict" in names

    def test_manifest_provenance_toolchain(self, tmp_path):
        zip_path, _ = self._write_zip(tmp_path)
        with zipfile.ZipFile(zip_path) as zf:
            manifest = json.loads(zf.read("manifest.json"))
        assert manifest["build"]["toolchain"]["mips"]["glibc"] == "2.29"
        assert manifest["build"]["toolchain"]["arm"]["gcc"] == "14.3.0"

    def test_zip_contains_license_files(self, tmp_path):
        zip_path, _ = self._write_zip(tmp_path)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        license_files = [n for n in names if n.startswith("LICENSES/")]
        assert len(license_files) >= 2
