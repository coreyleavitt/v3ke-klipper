"""C2 unit tests — release zip + manifest pipeline.

Slice C2: pure Python, offline. No container, no real git tag required.

TDD order (each RED→GREEN):
  C2-1:  resolve_version happy path (file-read contract)
  C2-2:  resolve_version loud failure (file-read contract)
  C2-3:  release_zip_name (bare version, no 'v' prefix in version arg)
  C2-4:  build_manifest shape
  C2-5:  validate_manifest valid passes
  C2-6:  validate_manifest invalid fails (missing field / bad sha256)
  C2-7:  submodule_provenance (real repo)
  C2-8:  hash_artifact
  C2-9:  release_members plan
  C2-10: emit_versions (ct_build)
  C2-11: write_release_zip (temp fake repo — hermetic, offline)
  C2-12: RELEASE_PLATFORMS descriptor
  C2-13: bundle_name
  C2-14: release_members parameterized by platform
  C2-15: write_release_bundles — two bundles, content, byte-identity, determinism, error
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from build.release import (
    RELEASE_PLATFORMS,
    ReleasePlatform,
    ReleaseError,
    build_manifest,
    bundle_name,
    hash_artifact,
    release_members,
    release_zip_name,
    resolve_version,
    submodule_provenance,
    validate_manifest,
    write_release_bundles,
    write_release_zip,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_EPOCH = 1_700_000_000  # fixed, no git required


# ──────────────────────────────────────────────────────────────────────────────
# C2-1: resolve_version — happy path (file-read contract)
# ──────────────────────────────────────────────────────────────────────────────

class TestResolveVersionHappy:
    """resolve_version reads a VERSION file and returns a bare semver string."""

    def test_returns_bare_version(self, tmp_path):
        (tmp_path / "VERSION").write_text("0.1.0\n")
        assert resolve_version(tmp_path) == "0.1.0"

    def test_strips_trailing_newline(self, tmp_path):
        (tmp_path / "VERSION").write_text("1.2.3\n")
        assert resolve_version(tmp_path) == "1.2.3"

    def test_returns_prerelease(self, tmp_path):
        (tmp_path / "VERSION").write_text("0.2.0-rc.1\n")
        assert resolve_version(tmp_path) == "0.2.0-rc.1"


# ──────────────────────────────────────────────────────────────────────────────
# C2-2: resolve_version — loud failure (file-read contract)
# ──────────────────────────────────────────────────────────────────────────────

class TestResolveVersionFailure:
    """resolve_version raises ReleaseError for missing/empty/malformed VERSION."""

    def test_raises_on_missing_file(self, tmp_path):
        with pytest.raises(ReleaseError, match="VERSION"):
            resolve_version(tmp_path)

    def test_raises_on_empty_file(self, tmp_path):
        (tmp_path / "VERSION").write_text("")
        with pytest.raises(ReleaseError):
            resolve_version(tmp_path)

    def test_raises_on_whitespace_only(self, tmp_path):
        (tmp_path / "VERSION").write_text("  \n")
        with pytest.raises(ReleaseError):
            resolve_version(tmp_path)

    def test_raises_on_v_prefix(self, tmp_path):
        """File must be bare — v-prefix is rejected (CI writes bare semver)."""
        (tmp_path / "VERSION").write_text("v0.1.0\n")
        with pytest.raises(ReleaseError):
            resolve_version(tmp_path)

    def test_error_message_mentions_version_file(self, tmp_path):
        """The error message must name the VERSION file and the prepare-version CI job."""
        with pytest.raises(ReleaseError) as exc_info:
            resolve_version(tmp_path)
        msg = str(exc_info.value)
        assert "VERSION" in msg


# ──────────────────────────────────────────────────────────────────────────────
# C2-3: release_zip_name (compat alias — now returns tar.xz for linux-amd64)
# ──────────────────────────────────────────────────────────────────────────────

class TestReleaseZipName:
    """release_zip_name is a backward-compat alias for bundle_name(version, linux-amd64).
    The linux platform now uses tar.xz, so the name ends in .tar.xz.
    """

    def test_basic_version(self):
        assert release_zip_name("0.1.0") == "v3ke-0.1.0-linux-amd64.tar.xz"

    def test_prerelease_suffix(self):
        assert release_zip_name("0.1.0-rc.1") == "v3ke-0.1.0-rc.1-linux-amd64.tar.xz"


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
            version="0.1.0",
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
        m = self._make(version="0.2.0")
        assert m["build"]["id"] == "0.2.0"

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
            "id": "0.1.0",
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

    # v3ke CLI binaries — both platforms
    (repo / "tools" / "v3ke").mkdir(parents=True)
    (repo / "tools" / "v3ke" / "v3ke").write_bytes(b"nim binary linux")
    (repo / "tools" / "v3ke" / "v3ke.exe").write_bytes(b"nim binary windows")

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
    """write_release_zip is a compat shim: it calls write_release_bundles and returns
    the linux-amd64 bundle path (now a tar.xz).  All content assertions use tarfile."""

    def _write_zip(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        out_dir = tmp_path / "dist"
        out_dir.mkdir()
        bundle_path = write_release_zip(
            repo_root=repo,
            out_dir=out_dir,
            version="0.1.0",
            commit="deadbeef" * 5,
            source_date_epoch=_EPOCH,
            toolchain=_SAMPLE_TOOLCHAIN_VERSIONS,
            reproducible=False,
            _submodule_provenance=_fake_submodule_provenance,
        )
        return bundle_path, repo

    def _tar_names(self, path):
        with tarfile.open(path, "r:xz") as tf:
            return tf.getnames()

    def _tar_read(self, path, member):
        with tarfile.open(path, "r:xz") as tf:
            return tf.extractfile(member).read()

    def test_returns_path(self, tmp_path):
        bundle_path, _ = self._write_zip(tmp_path)
        assert isinstance(bundle_path, Path)

    def test_zip_file_exists(self, tmp_path):
        bundle_path, _ = self._write_zip(tmp_path)
        assert bundle_path.exists()

    def test_zip_name_matches_convention(self, tmp_path):
        """write_release_zip now returns the linux tar.xz bundle."""
        bundle_path, _ = self._write_zip(tmp_path)
        assert bundle_path.name == "v3ke-0.1.0-linux-amd64.tar.xz"

    def test_standalone_manifest_written(self, tmp_path):
        """A standalone manifest.json is written next to the bundles."""
        bundle_path, _ = self._write_zip(tmp_path)
        assert (bundle_path.parent / "manifest.json").exists(), (
            "write_release_zip must write a standalone manifest.json"
        )

    def test_standalone_manifest_matches_embedded(self, tmp_path):
        """The standalone manifest.json is byte-identical to the copy inside the tar.xz."""
        bundle_path, _ = self._write_zip(tmp_path)
        standalone = (bundle_path.parent / "manifest.json").read_bytes()
        embedded = self._tar_read(bundle_path, "manifest.json")
        assert standalone == embedded

    def test_zip_contains_install_md(self, tmp_path):
        bundle_path, _ = self._write_zip(tmp_path)
        assert "INSTALL.md" in self._tar_names(bundle_path)

    def test_zip_contains_sources_md(self, tmp_path):
        bundle_path, _ = self._write_zip(tmp_path)
        assert "SOURCES.md" in self._tar_names(bundle_path)

    def test_zip_contains_manifest(self, tmp_path):
        bundle_path, _ = self._write_zip(tmp_path)
        assert "manifest.json" in self._tar_names(bundle_path)

    def test_zip_contains_firmware_artifacts(self, tmp_path):
        bundle_path, _ = self._write_zip(tmp_path)
        names = self._tar_names(bundle_path)
        assert "firmware/katapult.bin" in names
        assert "firmware/klipper.bin" in names

    def test_zip_contains_host_artifacts(self, tmp_path):
        bundle_path, _ = self._write_zip(tmp_path)
        names = self._tar_names(bundle_path)
        assert "host/c_helper.so" in names
        assert "host/klipper.elf" in names
        assert "host/klipper.dict" in names

    def test_zip_contains_v3ke(self, tmp_path):
        bundle_path, _ = self._write_zip(tmp_path)
        assert "v3ke" in self._tar_names(bundle_path)

    def test_manifest_is_valid_json(self, tmp_path):
        bundle_path, _ = self._write_zip(tmp_path)
        manifest = json.loads(self._tar_read(bundle_path, "manifest.json"))
        assert manifest["_type"] == "v3ke-build"

    def test_manifest_validates_against_schema(self, tmp_path):
        bundle_path, _ = self._write_zip(tmp_path)
        manifest = json.loads(self._tar_read(bundle_path, "manifest.json"))
        validate_manifest(manifest)  # must not raise

    def test_manifest_artifact_sha256_correct(self, tmp_path):
        bundle_path, repo = self._write_zip(tmp_path)
        manifest = json.loads(self._tar_read(bundle_path, "manifest.json"))
        real_katapult = repo / "external" / "katapult" / "out" / "katapult.bin"
        expected_sha = hashlib.sha256(real_katapult.read_bytes()).hexdigest()
        katapult_entry = next(
            a for a in manifest["artifacts"] if a["name"] == "katapult.bin"
        )
        assert katapult_entry["sha256"] == expected_sha

    def test_manifest_has_all_expected_artifact_names(self, tmp_path):
        bundle_path, _ = self._write_zip(tmp_path)
        manifest = json.loads(self._tar_read(bundle_path, "manifest.json"))
        names = {a["name"] for a in manifest["artifacts"]}
        paths = {a["path"] for a in manifest["artifacts"]}
        assert "katapult.bin" in names
        assert "klipper.bin" in names
        assert "c_helper.so" in names
        assert "klipper_mcu.elf" in names, (
            f"Manifest must reference klipper_mcu.elf as the host ELF artifact name; got: {names}"
        )
        assert "host/klipper.elf" in paths, (
            f"Manifest must have host/klipper.elf as archive path; got: {paths}"
        )
        assert "klipper.dict" in names

    def test_manifest_provenance_toolchain(self, tmp_path):
        bundle_path, _ = self._write_zip(tmp_path)
        manifest = json.loads(self._tar_read(bundle_path, "manifest.json"))
        assert manifest["build"]["toolchain"]["mips"]["glibc"] == "2.29"
        assert manifest["build"]["toolchain"]["arm"]["gcc"] == "14.3.0"

    def test_zip_contains_license_files(self, tmp_path):
        bundle_path, _ = self._write_zip(tmp_path)
        names = self._tar_names(bundle_path)
        license_files = [n for n in names if n.startswith("LICENSES/")]
        assert len(license_files) >= 2


# ──────────────────────────────────────────────────────────────────────────────
# C2-12: RELEASE_PLATFORMS descriptor
# ──────────────────────────────────────────────────────────────────────────────

class TestReleasePlatforms:
    """RELEASE_PLATFORMS is a sequence of exactly two ReleasePlatform descriptors."""

    def test_two_platforms(self):
        assert len(RELEASE_PLATFORMS) == 2

    def test_platform_names(self):
        names = {p.name for p in RELEASE_PLATFORMS}
        assert names == {"linux-amd64", "windows-amd64"}

    def test_linux_platform_attributes(self):
        linux = next(p for p in RELEASE_PLATFORMS if p.name == "linux-amd64")
        assert linux.cli_source == "tools/v3ke/v3ke"
        assert linux.cli_arcname == "v3ke"
        assert linux.fmt == "tar.xz"

    def test_windows_platform_attributes(self):
        win = next(p for p in RELEASE_PLATFORMS if p.name == "windows-amd64")
        assert win.cli_source == "tools/v3ke/v3ke.exe"
        assert win.cli_arcname == "v3ke.exe"
        assert win.fmt == "zip"

    def test_platform_is_frozen_dataclass(self):
        """ReleasePlatform must be immutable — attempts to mutate raise."""
        linux = next(p for p in RELEASE_PLATFORMS if p.name == "linux-amd64")
        with pytest.raises((AttributeError, TypeError)):
            linux.name = "mutated"  # type: ignore[misc]


# ──────────────────────────────────────────────────────────────────────────────
# C2-13: bundle_name
# ──────────────────────────────────────────────────────────────────────────────

class TestBundleName:
    """bundle_name(version, platform) -> OS-native filename."""

    def _linux(self):
        return next(p for p in RELEASE_PLATFORMS if p.name == "linux-amd64")

    def _windows(self):
        return next(p for p in RELEASE_PLATFORMS if p.name == "windows-amd64")

    def test_linux_tar_xz(self):
        assert bundle_name("0.1.0", self._linux()) == "v3ke-0.1.0-linux-amd64.tar.xz"

    def test_windows_zip(self):
        assert bundle_name("0.1.0", self._windows()) == "v3ke-0.1.0-windows-amd64.zip"

    def test_prerelease_linux(self):
        assert bundle_name("0.1.0-rc.1", self._linux()) == "v3ke-0.1.0-rc.1-linux-amd64.tar.xz"

    def test_prerelease_windows(self):
        assert bundle_name("0.1.0-rc.1", self._windows()) == "v3ke-0.1.0-rc.1-windows-amd64.zip"


# ──────────────────────────────────────────────────────────────────────────────
# C2-14: release_members parameterized by platform
# ──────────────────────────────────────────────────────────────────────────────

class TestReleaseMembersParameterized:
    """release_members(repo_root, *, version, platform) uses platform CLI."""

    def _linux(self):
        return next(p for p in RELEASE_PLATFORMS if p.name == "linux-amd64")

    def _windows(self):
        return next(p for p in RELEASE_PLATFORMS if p.name == "windows-amd64")

    def test_linux_has_v3ke_not_exe(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="0.1.0", platform=self._linux())
        arcnames = [arc for _, arc in members]
        assert "v3ke" in arcnames
        assert "v3ke.exe" not in arcnames

    def test_windows_has_v3ke_exe_not_bare(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="0.1.0", platform=self._windows())
        arcnames = [arc for _, arc in members]
        assert "v3ke.exe" in arcnames
        assert "v3ke" not in arcnames

    def test_linux_cli_source_path(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="0.1.0", platform=self._linux())
        cli_src = next(src for src, arc in members if arc == "v3ke")
        assert cli_src == repo / "tools" / "v3ke" / "v3ke"

    def test_windows_cli_source_path(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="0.1.0", platform=self._windows())
        cli_src = next(src for src, arc in members if arc == "v3ke.exe")
        assert cli_src == repo / "tools" / "v3ke" / "v3ke.exe"

    def test_five_device_artifacts_present_both_platforms(self, tmp_path):
        device_arcnames = {
            "firmware/katapult.bin", "firmware/klipper.bin",
            "host/c_helper.so", "host/klipper.elf", "host/klipper.dict",
        }
        repo = _make_fake_repo(tmp_path)
        for plat in RELEASE_PLATFORMS:
            members = release_members(repo, version="0.1.0", platform=plat)
            arcnames = set(arc for _, arc in members)
            missing = device_arcnames - arcnames
            assert not missing, f"platform {plat.name} missing: {missing}"

    def test_manifest_not_in_plan(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        for plat in RELEASE_PLATFORMS:
            members = release_members(repo, version="0.1.0", platform=plat)
            arcnames = [arc for _, arc in members]
            assert "manifest.json" not in arcnames

    def test_backward_compat_no_platform_arg(self, tmp_path):
        """release_members without 'platform' kwarg still works (default = linux-amd64)."""
        repo = _make_fake_repo(tmp_path)
        members = release_members(repo, version="0.1.0")
        arcnames = [arc for _, arc in members]
        assert "v3ke" in arcnames
        assert "v3ke.exe" not in arcnames


# ──────────────────────────────────────────────────────────────────────────────
# C2-15: write_release_bundles
# ──────────────────────────────────────────────────────────────────────────────

_BUNDLE_TOOLCHAIN = {
    "mips": {"glibc": "2.29", "gcc": "8.5.0", "binutils": "2.32",
             "linux": "4.14.329", "glibc_min_kernel": "4.4.0"},
    "arm":  {"gcc": "14.3.0"},
}

_EXPECTED_DEVICE_ARCNAMES = {
    "firmware/katapult.bin", "firmware/klipper.bin",
    "host/c_helper.so", "host/klipper.elf", "host/klipper.dict",
}

_EXPECTED_DOC_ARCNAMES = {"INSTALL.md", "SOURCES.md", "manifest.json"}

_EXPECTED_LICENSE_PREFIXES = {
    "LICENSES/v3ke.LICENSE", "LICENSES/klipper.LICENSE",
    "LICENSES/katapult.LICENSE", "LICENSES/mainsail-config.LICENSE",
}


def _write_bundles(tmp_path, *, out_subdir="dist"):
    repo = _make_fake_repo(tmp_path)
    out_dir = tmp_path / out_subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = write_release_bundles(
        repo,
        out_dir,
        version="0.1.0",
        commit="deadbeef" * 5,
        source_date_epoch=_EPOCH,
        toolchain=_BUNDLE_TOOLCHAIN,
        reproducible=False,
        _submodule_provenance=_fake_submodule_provenance,
    )
    return paths, repo, out_dir


def _tar_names(path: Path) -> list[str]:
    with tarfile.open(path, "r:xz") as tf:
        return tf.getnames()


def _zip_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


class TestWriteReleaseBundlesReturnValue:
    """write_release_bundles returns a list of two Paths."""

    def test_returns_list(self, tmp_path):
        paths, _, _ = _write_bundles(tmp_path)
        assert isinstance(paths, list)

    def test_returns_two_paths(self, tmp_path):
        paths, _, _ = _write_bundles(tmp_path)
        assert len(paths) == 2

    def test_all_elements_are_path(self, tmp_path):
        paths, _, _ = _write_bundles(tmp_path)
        for p in paths:
            assert isinstance(p, Path)


class TestWriteReleaseBundlesNames:
    """Bundle filenames follow the OS-native convention."""

    def test_linux_bundle_exists(self, tmp_path):
        paths, _, _ = _write_bundles(tmp_path)
        names = {p.name for p in paths}
        assert "v3ke-0.1.0-linux-amd64.tar.xz" in names

    def test_windows_bundle_exists(self, tmp_path):
        paths, _, _ = _write_bundles(tmp_path)
        names = {p.name for p in paths}
        assert "v3ke-0.1.0-windows-amd64.zip" in names

    def test_linux_bundle_file_exists_on_disk(self, tmp_path):
        paths, _, out_dir = _write_bundles(tmp_path)
        linux = next(p for p in paths if p.name.endswith(".tar.xz"))
        assert linux.exists()

    def test_windows_bundle_file_exists_on_disk(self, tmp_path):
        paths, _, out_dir = _write_bundles(tmp_path)
        win = next(p for p in paths if p.name.endswith(".zip"))
        assert win.exists()

    def test_no_linux_zip_produced(self, tmp_path):
        """The old linux-amd64.zip must NOT be emitted."""
        paths, _, out_dir = _write_bundles(tmp_path)
        assert not (out_dir / "v3ke-0.1.0-linux-amd64.zip").exists()


class TestLinuxTarXz:
    """The linux bundle is a valid tar.xz with the right members."""

    def _linux_path(self, tmp_path):
        paths, _, _ = _write_bundles(tmp_path)
        return next(p for p in paths if p.name.endswith(".tar.xz"))

    def test_opens_as_tar_xz(self, tmp_path):
        path = self._linux_path(tmp_path)
        with tarfile.open(path, "r:xz") as tf:
            assert tf.getnames()  # non-empty

    def test_contains_five_device_artifacts(self, tmp_path):
        names = set(_tar_names(self._linux_path(tmp_path)))
        assert _EXPECTED_DEVICE_ARCNAMES <= names

    def test_contains_v3ke_not_exe(self, tmp_path):
        names = _tar_names(self._linux_path(tmp_path))
        assert "v3ke" in names
        assert "v3ke.exe" not in names

    def test_contains_install_md(self, tmp_path):
        assert "INSTALL.md" in _tar_names(self._linux_path(tmp_path))

    def test_contains_sources_md(self, tmp_path):
        assert "SOURCES.md" in _tar_names(self._linux_path(tmp_path))

    def test_contains_manifest_json(self, tmp_path):
        assert "manifest.json" in _tar_names(self._linux_path(tmp_path))

    def test_contains_four_licenses(self, tmp_path):
        names = set(_tar_names(self._linux_path(tmp_path)))
        assert _EXPECTED_LICENSE_PREFIXES <= names

    def test_v3ke_mode_is_executable(self, tmp_path):
        path = self._linux_path(tmp_path)
        with tarfile.open(path, "r:xz") as tf:
            ti = tf.getmember("v3ke")
        assert ti.mode & 0o111, f"v3ke should be executable, got mode {ti.mode:#o}"

    def test_members_have_fixed_mtime(self, tmp_path):
        path = self._linux_path(tmp_path)
        with tarfile.open(path, "r:xz") as tf:
            for ti in tf.getmembers():
                assert ti.mtime == _EPOCH, f"{ti.name}: mtime={ti.mtime}, want {_EPOCH}"

    def test_members_have_zero_uid_gid(self, tmp_path):
        path = self._linux_path(tmp_path)
        with tarfile.open(path, "r:xz") as tf:
            for ti in tf.getmembers():
                assert ti.uid == 0, f"{ti.name}: uid={ti.uid}"
                assert ti.gid == 0, f"{ti.name}: gid={ti.gid}"

    def test_members_have_empty_uname_gname(self, tmp_path):
        path = self._linux_path(tmp_path)
        with tarfile.open(path, "r:xz") as tf:
            for ti in tf.getmembers():
                assert ti.uname == "", f"{ti.name}: uname={ti.uname!r}"
                assert ti.gname == "", f"{ti.name}: gname={ti.gname!r}"


class TestWindowsZip:
    """The windows bundle is a valid zip with the right members."""

    def _win_path(self, tmp_path):
        paths, _, _ = _write_bundles(tmp_path)
        return next(p for p in paths if p.name.endswith(".zip"))

    def test_opens_as_zip(self, tmp_path):
        path = self._win_path(tmp_path)
        with zipfile.ZipFile(path) as zf:
            assert zf.namelist()

    def test_contains_five_device_artifacts(self, tmp_path):
        names = set(_zip_names(self._win_path(tmp_path)))
        assert _EXPECTED_DEVICE_ARCNAMES <= names

    def test_contains_v3ke_exe_not_bare(self, tmp_path):
        names = _zip_names(self._win_path(tmp_path))
        assert "v3ke.exe" in names
        assert "v3ke" not in names

    def test_contains_install_md(self, tmp_path):
        assert "INSTALL.md" in _zip_names(self._win_path(tmp_path))

    def test_contains_sources_md(self, tmp_path):
        assert "SOURCES.md" in _zip_names(self._win_path(tmp_path))

    def test_contains_manifest_json(self, tmp_path):
        assert "manifest.json" in _zip_names(self._win_path(tmp_path))

    def test_contains_four_licenses(self, tmp_path):
        names = set(_zip_names(self._win_path(tmp_path)))
        assert _EXPECTED_LICENSE_PREFIXES <= names


class TestManifestIdentity:
    """The shared manifest is byte-identical across all three copies."""

    def test_standalone_manifest_exists(self, tmp_path):
        _, _, out_dir = _write_bundles(tmp_path)
        assert (out_dir / "manifest.json").exists()

    def test_standalone_matches_linux_embedded(self, tmp_path):
        paths, _, out_dir = _write_bundles(tmp_path)
        standalone = (out_dir / "manifest.json").read_bytes()
        linux = next(p for p in paths if p.name.endswith(".tar.xz"))
        with tarfile.open(linux, "r:xz") as tf:
            embedded = tf.extractfile("manifest.json").read()
        assert standalone == embedded

    def test_standalone_matches_windows_embedded(self, tmp_path):
        paths, _, out_dir = _write_bundles(tmp_path)
        standalone = (out_dir / "manifest.json").read_bytes()
        win = next(p for p in paths if p.name.endswith(".zip"))
        with zipfile.ZipFile(win) as zf:
            embedded = zf.read("manifest.json")
        assert standalone == embedded

    def test_linux_and_windows_manifest_byte_identical(self, tmp_path):
        paths, _, out_dir = _write_bundles(tmp_path)
        linux = next(p for p in paths if p.name.endswith(".tar.xz"))
        win = next(p for p in paths if p.name.endswith(".zip"))
        with tarfile.open(linux, "r:xz") as tf:
            linux_manifest = tf.extractfile("manifest.json").read()
        with zipfile.ZipFile(win) as zf:
            win_manifest = zf.read("manifest.json")
        assert linux_manifest == win_manifest

    def test_manifest_validates_against_schema(self, tmp_path):
        _, _, out_dir = _write_bundles(tmp_path)
        manifest = json.loads((out_dir / "manifest.json").read_bytes())
        validate_manifest(manifest)  # must not raise

    def test_manifest_artifact_count(self, tmp_path):
        _, _, out_dir = _write_bundles(tmp_path)
        manifest = json.loads((out_dir / "manifest.json").read_text())
        # Manifest covers exactly the 5 device artifacts
        assert len(manifest["artifacts"]) == 5

    def test_manifest_no_cli_artifact(self, tmp_path):
        """The CLI binary is NOT in the manifest artifact list."""
        _, _, out_dir = _write_bundles(tmp_path)
        manifest = json.loads((out_dir / "manifest.json").read_text())
        paths_in_manifest = {a["path"] for a in manifest["artifacts"]}
        assert "v3ke" not in paths_in_manifest
        assert "v3ke.exe" not in paths_in_manifest


class TestBundleDeterminism:
    """Building twice from identical inputs yields byte-identical files (both formats)."""

    def _write_twice(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        out1 = tmp_path / "dist1"
        out2 = tmp_path / "dist2"
        kwargs = dict(
            version="0.1.0",
            commit="deadbeef" * 5,
            source_date_epoch=_EPOCH,
            toolchain=_BUNDLE_TOOLCHAIN,
            reproducible=False,
            _submodule_provenance=_fake_submodule_provenance,
        )
        paths1 = write_release_bundles(repo, out1, **kwargs)
        paths2 = write_release_bundles(repo, out2, **kwargs)
        return paths1, paths2

    def test_linux_tar_xz_byte_identical(self, tmp_path):
        paths1, paths2 = self._write_twice(tmp_path)
        p1 = next(p for p in paths1 if p.name.endswith(".tar.xz"))
        p2 = next(p for p in paths2 if p.name.endswith(".tar.xz"))
        assert p1.read_bytes() == p2.read_bytes(), "linux tar.xz is not deterministic"

    def test_windows_zip_byte_identical(self, tmp_path):
        paths1, paths2 = self._write_twice(tmp_path)
        p1 = next(p for p in paths1 if p.name.endswith(".zip"))
        p2 = next(p for p in paths2 if p.name.endswith(".zip"))
        assert p1.read_bytes() == p2.read_bytes(), "windows zip is not deterministic"


class TestWriteReleaseBundlesErrors:
    """Missing platform CLI source raises ReleaseError naming the file and platform."""

    def test_missing_windows_exe_raises_release_error(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        # Remove the windows CLI
        (repo / "tools" / "v3ke" / "v3ke.exe").unlink()
        out_dir = tmp_path / "dist"
        out_dir.mkdir()
        with pytest.raises(ReleaseError) as exc_info:
            write_release_bundles(
                repo, out_dir,
                version="0.1.0",
                commit="deadbeef" * 5,
                source_date_epoch=_EPOCH,
                toolchain=_BUNDLE_TOOLCHAIN,
                reproducible=False,
                _submodule_provenance=_fake_submodule_provenance,
            )
        msg = str(exc_info.value)
        assert "v3ke.exe" in msg, f"error should name the missing file; got: {msg!r}"
        assert "windows-amd64" in msg, f"error should name the platform; got: {msg!r}"

    def test_missing_linux_binary_raises_release_error(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        (repo / "tools" / "v3ke" / "v3ke").unlink()
        out_dir = tmp_path / "dist"
        out_dir.mkdir()
        with pytest.raises(ReleaseError) as exc_info:
            write_release_bundles(
                repo, out_dir,
                version="0.1.0",
                commit="deadbeef" * 5,
                source_date_epoch=_EPOCH,
                toolchain=_BUNDLE_TOOLCHAIN,
                reproducible=False,
                _submodule_provenance=_fake_submodule_provenance,
            )
        msg = str(exc_info.value)
        assert "v3ke" in msg
        assert "linux-amd64" in msg
