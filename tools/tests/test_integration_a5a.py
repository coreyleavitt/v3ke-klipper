"""A5a integration acceptance test — real in-container build via build_all_artifacts.

Marked @pytest.mark.integration — requires the v3ke-toolchain image with CROSS_TOOLCHAIN
set.  Run explicitly in the toolchain image:

  podman run --rm -v "$PWD":/work -w /work/tools v3ke-toolchain \\
      uv run pytest tests/test_integration_a5a.py -m integration -v

Test structure
──────────────
I-A5a-1: build_all_artifacts completes (11 StepResults, all ok=True).
I-A5a-2: The 4 final artifacts exist at their expected output paths.
I-A5a-3: The 2 ELF artifacts (c_helper.so, klipper_mcu.elf) pass check_abi
          with fp_abi=6 (FP64, as required by §5 O6 resolution).
I-A5a-4: klipper_mcu.elf contains no embedded YYYYMMDD_HHMMSS timestamp
          (asserts cleanbuild=True held — determinism precondition).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from build.orchestrate import build_all_artifacts
from build.elf import inspect_elf, check_abi
from abi.abi_spec import ArtifactKind

# ──────────────────────────────────────────────────────────────────────────────
# Skip if not inside the toolchain container
# ──────────────────────────────────────────────────────────────────────────────

_CROSS_TOOLCHAIN = os.environ.get("CROSS_TOOLCHAIN", "")
_REPO_ROOT = Path("/work")   # canonical mount point inside the container

pytestmark = pytest.mark.integration

if not _CROSS_TOOLCHAIN:
    pytest.skip(
        "CROSS_TOOLCHAIN not set — integration tests require the v3ke-toolchain image",
        allow_module_level=True,
    )

# ──────────────────────────────────────────────────────────────────────────────
# Shared build fixture (build once, verify many assertions)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def build_results():
    """Run build_all_artifacts once and return the list of StepResults."""
    toolchain_root = Path(_CROSS_TOOLCHAIN)
    results = build_all_artifacts(
        repo_root=_REPO_ROOT,
        toolchain_root=toolchain_root,
    )
    return results


# Expected artifact paths relative to /work
_EXPECTED_ARTIFACTS = {
    "katapult.bin": _REPO_ROOT / "external" / "katapult" / "out" / "katapult.bin",
    "klipper.bin":  _REPO_ROOT / "mcu-firmware" / "klipper.bin",   # captured copy
    "c_helper.so":  _REPO_ROOT / "external" / "klipper" / "klippy" / "chelper" / "c_helper.so",
    "klipper_mcu.elf": _REPO_ROOT / "external" / "klipper" / "out" / "klipper.elf",
}

_ELF_ARTIFACTS = {
    "c_helper.so":     (ArtifactKind.SHARED_LIBRARY, _EXPECTED_ARTIFACTS["c_helper.so"]),
    "klipper_mcu.elf": (ArtifactKind.EXECUTABLE,     _EXPECTED_ARTIFACTS["klipper_mcu.elf"]),
}


# ──────────────────────────────────────────────────────────────────────────────
# I-A5a-1: build completes, 11 steps, all ok
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildCompletes:
    def test_11_step_results(self, build_results):
        assert len(build_results) == 11, (
            f"Expected 11 StepResults, got {len(build_results)}: "
            f"{[r.name for r in build_results]}"
        )

    def test_all_steps_ok(self, build_results):
        failures = [r for r in build_results if not r.ok]
        assert not failures, (
            f"Build steps failed: {[(r.name, r.detail) for r in failures]}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# I-A5a-2: the 4 final artifacts exist
# ──────────────────────────────────────────────────────────────────────────────

class TestArtifactsExist:
    @pytest.mark.parametrize("name,path", list(_EXPECTED_ARTIFACTS.items()))
    def test_artifact_exists(self, build_results, name, path):
        assert path.exists(), (
            f"Expected artifact '{name}' not found at {path}"
        )

    @pytest.mark.parametrize("name,path", list(_EXPECTED_ARTIFACTS.items()))
    def test_artifact_non_empty(self, build_results, name, path):
        assert path.stat().st_size > 0, (
            f"Artifact '{name}' is empty: {path}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# I-A5a-3: ELF artifacts pass ABI check with fp_abi=6 (FP64)
# ──────────────────────────────────────────────────────────────────────────────

class TestElfAbi:
    @pytest.mark.parametrize("name,kind_and_path", list(_ELF_ARTIFACTS.items()))
    def test_elf_abi_clean(self, build_results, name, kind_and_path):
        kind, path = kind_and_path
        data = path.read_bytes()
        info = inspect_elf(data)
        result = check_abi(info, kind)
        assert result.applicable, f"{name}: check_abi returned applicable=False (not an ELF?)"
        assert result.ok, (
            f"{name}: ABI check failed:\n"
            + "\n".join(
                f"  {v}" for v in result.violations
            )
        )

    @pytest.mark.parametrize("name,kind_and_path", list(_ELF_ARTIFACTS.items()))
    def test_fp_abi_is_fp64(self, build_results, name, kind_and_path):
        """fp_abi must be 6 (FP64) — confirmed by A-spike (§5 O6); any other value is a regression."""
        kind, path = kind_and_path
        data = path.read_bytes()
        info = inspect_elf(data)
        assert info.fp_abi == 6, (
            f"{name}: expected fp_abi=6 (FP64), got fp_abi={info.fp_abi} — "
            "stale artifact or wrong toolchain?"
        )


# ──────────────────────────────────────────────────────────────────────────────
# I-A5a-4: klipper_mcu.elf has no embedded timestamp (cleanbuild=True asserted)
# ──────────────────────────────────────────────────────────────────────────────

class TestCleanBuild:
    def test_klipper_mcu_elf_no_timestamp(self, build_results):
        """Klipper embeds a YYYYMMDD_HHMMSS timestamp when cleanbuild is False.

        A match here means the build was dirty (stale object files, unreadable tool
        versions) and reproduciblity is broken.  The clean step in klipper_mcu_steps
        is the precondition that prevents this.
        """
        elf_path = _EXPECTED_ARTIFACTS["klipper_mcu.elf"]
        raw = elf_path.read_bytes()
        # Klipper's buildcommands.py embeds e.g. "20240115_123456"
        timestamp_pattern = rb"\d{8}_\d{6}"
        match = re.search(timestamp_pattern, raw)
        assert match is None, (
            f"klipper_mcu.elf contains an embedded timestamp '{match.group().decode()}' "
            "— cleanbuild=False; non-deterministic build detected"
        )
