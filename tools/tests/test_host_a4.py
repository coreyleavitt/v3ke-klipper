"""A4 unit tests — MIPS host build command builders.

Slice A4: pure Python, FakeRunner, no real toolchain.

Test structure
──────────────
A4-1: c_helper_steps — ABI flags (-mips32r2, -mabi=32, -mhard-float, -mfp64, -mnan=2008),
      -shared -fPIC, kind==SHARED_LIBRARY, output path ends klippy/chelper/c_helper.so.
A4-2: c_helper_steps — source arguments equal chelper_sources() resolved under chelper dir.
A4-3: c_helper_steps — determinism prefix-map flags present; gcc path derived from
      toolchain_root; sysroot derived correctly.
A4-4: klipper_mcu_steps — first step is clean; build step runs make -C external/klipper;
      KCONFIG_CONFIG points at real existing klipper-host-mcu.config; CROSS_PREFIX set;
      determinism vars present; output path external/klipper/out/klipper.elf; kind==EXECUTABLE.
A4-5: host_steps == c_helper_steps + klipper_mcu_steps (count + order).
A4-6: host_steps + FakeRunner through run_steps completes without error.
"""

from __future__ import annotations

import pathlib
from pathlib import Path

import pytest

from build.host import (
    c_helper_steps,
    klipper_mcu_steps,
    host_steps,
    chelper_sources,
)
from build.artifacts import BuildStep, FakeRunner, run_steps
from abi.abi_spec import ArtifactKind

# ──────────────────────────────────────────────────────────────────────────────
# Constants / fixtures
# ──────────────────────────────────────────────────────────────────────────────

# klipper-mainline/ is three levels above tools/tests/
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

# Fixed epoch — no git required for unit tests.
_EPOCH = 1_700_000_000

# Fake toolchain root; the functions must only derive paths from it, not stat it.
_FAKE_TC = Path("/fake/toolchain")

# Real chelper dir (under the pinned klipper submodule)
_CHELPER_DIR = _REPO_ROOT / "external" / "klipper" / "klippy" / "chelper"

# Real klipper host MCU kconfig
_HOST_MCU_KCONFIG = _REPO_ROOT / "klipper" / "klipper_host_mcu" / "klipper-host-mcu.config"


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _c_helper_step(tc: Path = _FAKE_TC) -> BuildStep:
    """Return the single BuildStep from c_helper_steps (it's a one-element list)."""
    steps = c_helper_steps(_REPO_ROOT, _EPOCH, toolchain_root=tc)
    assert len(steps) == 1, f"Expected 1 c_helper step, got {len(steps)}"
    return steps[0]


def _klipper_mcu_step_list(tc: Path = _FAKE_TC) -> list[BuildStep]:
    return klipper_mcu_steps(_REPO_ROOT, _EPOCH, toolchain_root=tc)


# ──────────────────────────────────────────────────────────────────────────────
# A4-1: c_helper_steps — required ABI flags, -shared -fPIC, kind, output path
# ──────────────────────────────────────────────────────────────────────────────

class TestCHelperStepsAbiFlags:
    """The c_helper BuildStep must carry every required MIPS ABI flag."""

    def test_mips32r2_flag_present(self):
        step = _c_helper_step()
        assert "-mips32r2" in step.cmd, f"Missing -mips32r2 in {step.cmd}"

    def test_mabi32_flag_present(self):
        step = _c_helper_step()
        assert "-mabi=32" in step.cmd, f"Missing -mabi=32 in {step.cmd}"

    def test_mhard_float_flag_present(self):
        step = _c_helper_step()
        assert "-mhard-float" in step.cmd, f"Missing -mhard-float in {step.cmd}"

    def test_mfp64_flag_present(self):
        step = _c_helper_step()
        assert "-mfp64" in step.cmd, f"Missing -mfp64 in {step.cmd}"

    def test_mnan2008_flag_present(self):
        step = _c_helper_step()
        assert "-mnan=2008" in step.cmd, f"Missing -mnan=2008 in {step.cmd}"

    def test_shared_flag_present(self):
        step = _c_helper_step()
        assert "-shared" in step.cmd, f"Missing -shared in {step.cmd}"

    def test_fpic_flag_present(self):
        step = _c_helper_step()
        assert "-fPIC" in step.cmd, f"Missing -fPIC in {step.cmd}"

    def test_kind_is_shared_library(self):
        step = _c_helper_step()
        assert step.kind is ArtifactKind.SHARED_LIBRARY, (
            f"Expected SHARED_LIBRARY, got {step.kind!r}"
        )

    def test_output_path_ends_with_c_helper_so(self):
        step = _c_helper_step()
        assert step.output_path.name == "c_helper.so", (
            f"Expected output to be c_helper.so, got {step.output_path.name}"
        )

    def test_output_path_under_klippy_chelper(self):
        """Output must be <repo_root>/external/klipper/klippy/chelper/c_helper.so."""
        step = _c_helper_step()
        expected = _REPO_ROOT / "external" / "klipper" / "klippy" / "chelper" / "c_helper.so"
        assert step.output_path == expected, (
            f"Output path mismatch: {step.output_path} != {expected}"
        )

    def test_c_helper_steps_returns_list_of_one(self):
        steps = c_helper_steps(_REPO_ROOT, _EPOCH, toolchain_root=_FAKE_TC)
        assert len(steps) == 1

    def test_sysroot_arg_derived_from_toolchain_root(self):
        """--sysroot=<tc>/mipsel-buildroot-linux-gnu/sysroot must appear in cmd."""
        step = _c_helper_step()
        expected_sysroot = str(_FAKE_TC / "mipsel-buildroot-linux-gnu" / "sysroot")
        sysroot_args = [a for a in step.cmd if a.startswith("--sysroot")]
        assert sysroot_args, f"No --sysroot argument in {step.cmd}"
        # accept both --sysroot=<path> and --sysroot <path>
        combined = " ".join(sysroot_args)
        assert expected_sysroot in combined, (
            f"Expected sysroot {expected_sysroot} in cmd, got {sysroot_args}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# A4-2: c_helper_steps — source arguments match chelper_sources()
# ──────────────────────────────────────────────────────────────────────────────

class TestCHelperSources:
    """Source arguments in the gcc cmd must match chelper_sources() resolved under chelper dir."""

    def test_source_files_match_chelper_sources(self):
        """The source paths in the gcc command must exactly match chelper_sources() resolved."""
        step = _c_helper_step()
        expected_sources = [
            str(_CHELPER_DIR / f) for f in chelper_sources(_CHELPER_DIR / "__init__.py")
        ]
        # Source files are positional arguments (no flag prefix) that end with .c
        actual_sources = [a for a in step.cmd if a.endswith(".c")]
        assert actual_sources == expected_sources, (
            f"Source files mismatch:\n  expected: {expected_sources}\n  actual:   {actual_sources}"
        )

    def test_all_sources_are_absolute_paths(self):
        """All source file arguments must be absolute paths."""
        step = _c_helper_step()
        c_sources = [a for a in step.cmd if a.endswith(".c")]
        assert c_sources, "No .c source files found in cmd"
        for src in c_sources:
            assert Path(src).is_absolute(), f"Source path is not absolute: {src}"

    def test_all_sources_are_under_chelper_dir(self):
        """All source paths must be under the chelper directory."""
        step = _c_helper_step()
        c_sources = [a for a in step.cmd if a.endswith(".c")]
        for src in c_sources:
            assert str(_CHELPER_DIR) in src, (
                f"Source path {src} not under chelper dir {_CHELPER_DIR}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# A4-3: c_helper_steps — determinism flags + gcc path + sysroot
# ──────────────────────────────────────────────────────────────────────────────

class TestCHelperDeterminism:
    """Determinism prefix-map flags and SOURCE_DATE_EPOCH must be present."""

    def test_source_date_epoch_in_cmd(self):
        """SOURCE_DATE_EPOCH must be injected (via env prefix or as an argument)."""
        step = _c_helper_step()
        # Injected via ["env", "SOURCE_DATE_EPOCH=<epoch>", gcc, ...] form
        combined = " ".join(step.cmd)
        assert f"SOURCE_DATE_EPOCH={_EPOCH}" in combined, (
            f"SOURCE_DATE_EPOCH={_EPOCH} not found in cmd: {step.cmd}"
        )

    def test_ffile_prefix_map_present(self):
        step = _c_helper_step()
        assert any("-ffile-prefix-map=" in a for a in step.cmd), (
            f"Missing -ffile-prefix-map in cmd: {step.cmd}"
        )

    def test_fdebug_prefix_map_present(self):
        step = _c_helper_step()
        assert any("-fdebug-prefix-map=" in a for a in step.cmd), (
            f"Missing -fdebug-prefix-map in cmd: {step.cmd}"
        )

    def test_prefix_map_contains_repo_root(self):
        """Prefix maps must reference the actual repo root so they're effective."""
        step = _c_helper_step()
        prefix_args = [a for a in step.cmd if "-ffile-prefix-map=" in a or "-fdebug-prefix-map=" in a]
        combined = " ".join(prefix_args)
        assert str(_REPO_ROOT) in combined, (
            f"Prefix map does not contain repo_root {_REPO_ROOT}: {prefix_args}"
        )

    def test_gcc_path_derived_from_toolchain_root(self):
        """The gcc executable must be <toolchain_root>/bin/mipsel-buildroot-linux-gnu-gcc."""
        step = _c_helper_step()
        expected_gcc = str(_FAKE_TC / "bin" / "mipsel-buildroot-linux-gnu-gcc")
        # gcc is in the cmd (possibly after 'env SOURCE_DATE_EPOCH=...')
        assert expected_gcc in step.cmd, (
            f"Expected gcc={expected_gcc} in cmd, got {step.cmd}"
        )

    def test_different_toolchain_root_propagates(self):
        """A different toolchain_root must produce a different gcc/sysroot path."""
        tc2 = Path("/other/toolchain")
        step = _c_helper_step(tc=tc2)
        expected_gcc = str(tc2 / "bin" / "mipsel-buildroot-linux-gnu-gcc")
        assert expected_gcc in step.cmd

    def test_epoch_value_propagates(self):
        """A different epoch must be reflected in the command."""
        other_epoch = 1_600_000_000
        steps = c_helper_steps(_REPO_ROOT, other_epoch, toolchain_root=_FAKE_TC)
        step = steps[0]
        combined = " ".join(step.cmd)
        assert f"SOURCE_DATE_EPOCH={other_epoch}" in combined


# ──────────────────────────────────────────────────────────────────────────────
# A4-4: klipper_mcu_steps — triplet shape and constraint checks
# ──────────────────────────────────────────────────────────────────────────────

class TestKlipperMcuSteps:
    """klipper_mcu_steps produces a 3-step clean/olddefconfig/build triplet."""

    def test_triplet_count(self):
        steps = _klipper_mcu_step_list()
        assert len(steps) == 3, f"Expected 3 steps, got {len(steps)}"

    def test_first_step_is_clean(self):
        """Clean must be first — ensures a fresh build (Klipper timestamps cleanbuild)."""
        steps = _klipper_mcu_step_list()
        assert steps[0].name.endswith("-clean"), (
            f"First step must be clean, got: {steps[0].name}"
        )

    def test_second_step_is_olddefconfig(self):
        steps = _klipper_mcu_step_list()
        assert "olddefconfig" in steps[1].name or "olddefconfig" in " ".join(steps[1].cmd), (
            f"Second step must be olddefconfig, got: {steps[1]}"
        )

    def test_build_step_is_last(self):
        steps = _klipper_mcu_step_list()
        build_step = steps[-1]
        assert "build" in build_step.name or (
            "make" in build_step.cmd and "clean" not in build_step.cmd and "olddefconfig" not in build_step.cmd
        ), f"Last step must be build, got: {build_step}"

    def test_make_runs_in_external_klipper(self):
        """The make -C argument must point to external/klipper."""
        steps = _klipper_mcu_step_list()
        for step in steps:
            for arg in step.cmd:
                if arg.startswith("-C"):
                    d = Path(arg[2:])
                    assert d == _REPO_ROOT / "external" / "klipper", (
                        f"Expected make -C to point to external/klipper, got {d}"
                    )

    def test_kconfig_points_to_real_existing_config(self):
        """KCONFIG_CONFIG must point to the real klipper-host-mcu.config which must exist."""
        steps = _klipper_mcu_step_list()
        kconfig_paths = set()
        for step in steps:
            for arg in step.cmd:
                if arg.startswith("KCONFIG_CONFIG="):
                    kconfig_paths.add(Path(arg[len("KCONFIG_CONFIG="):]))
        assert kconfig_paths, "No KCONFIG_CONFIG= found in klipper_mcu steps"
        for p in kconfig_paths:
            assert p.exists(), (
                f"KCONFIG_CONFIG path does not exist: {p}"
            )
            assert p.name == "klipper-host-mcu.config", (
                f"Expected klipper-host-mcu.config, got {p.name}"
            )

    def test_kconfig_is_under_klipper_host_mcu_dir(self):
        """KCONFIG_CONFIG must point into klipper/klipper_host_mcu/, not into external/."""
        steps = _klipper_mcu_step_list()
        for step in steps:
            for arg in step.cmd:
                if arg.startswith("KCONFIG_CONFIG="):
                    p = Path(arg[len("KCONFIG_CONFIG="):])
                    assert "klipper_host_mcu" in p.parts, (
                        f"KCONFIG_CONFIG must be under klipper_host_mcu/, got {p}"
                    )

    def test_cross_prefix_set_in_build_and_config_steps(self):
        """CROSS_PREFIX=<toolchain_root>/bin/mipsel-buildroot-linux-gnu- must be in all steps."""
        steps = _klipper_mcu_step_list()
        expected_prefix = str(_FAKE_TC / "bin" / "mipsel-buildroot-linux-gnu-")
        expected_arg = f"CROSS_PREFIX={expected_prefix}"
        # CROSS_PREFIX must appear in at least the olddefconfig + build steps
        non_clean = [s for s in steps if not s.name.endswith("-clean")]
        for step in non_clean:
            assert expected_arg in step.cmd, (
                f"Step '{step.name}' missing CROSS_PREFIX arg: {step.cmd}"
            )

    def test_determinism_vars_in_compilation_steps(self):
        """olddefconfig and build steps must carry SOURCE_DATE_EPOCH and prefix-map flags."""
        steps = _klipper_mcu_step_list()
        non_clean = [s for s in steps if not s.name.endswith("-clean")]
        for step in non_clean:
            cmd_str = " ".join(step.cmd)
            assert f"SOURCE_DATE_EPOCH={_EPOCH}" in step.cmd, (
                f"Step '{step.name}' missing SOURCE_DATE_EPOCH in {step.cmd}"
            )
            assert any("-ffile-prefix-map=" in a for a in step.cmd), (
                f"Step '{step.name}' missing -ffile-prefix-map"
            )
            assert any("-fdebug-prefix-map=" in a for a in step.cmd), (
                f"Step '{step.name}' missing -fdebug-prefix-map"
            )

    def test_output_path_is_klipper_elf(self):
        """Build step output_path must be external/klipper/out/klipper.elf."""
        steps = _klipper_mcu_step_list()
        build_step = steps[-1]
        expected = _REPO_ROOT / "external" / "klipper" / "out" / "klipper.elf"
        assert build_step.output_path == expected, (
            f"Output path mismatch: {build_step.output_path} != {expected}"
        )

    def test_output_kind_is_executable(self):
        """The build step must carry ArtifactKind.EXECUTABLE."""
        steps = _klipper_mcu_step_list()
        build_step = steps[-1]
        assert build_step.kind is ArtifactKind.EXECUTABLE, (
            f"Expected EXECUTABLE, got {build_step.kind!r}"
        )

    def test_clean_step_does_not_carry_cflags(self):
        """Clean step must not carry CFLAGS_EXTRA (no compilation happens)."""
        steps = _klipper_mcu_step_list()
        clean_step = steps[0]
        assert not any("CFLAGS_EXTRA" in a for a in clean_step.cmd), (
            f"Clean step must not have CFLAGS_EXTRA: {clean_step.cmd}"
        )

    def test_external_klipper_dir_exists(self):
        """The make -C target must be an existing directory."""
        steps = _klipper_mcu_step_list()
        for step in steps:
            for arg in step.cmd:
                if arg.startswith("-C"):
                    d = Path(arg[2:])
                    assert d.is_dir(), f"make -C dir not found: {d}"


# ──────────────────────────────────────────────────────────────────────────────
# A4-5: host_steps == c_helper_steps + klipper_mcu_steps
# ──────────────────────────────────────────────────────────────────────────────

class TestHostSteps:
    """host_steps must be the ordered concatenation of c_helper_steps + klipper_mcu_steps."""

    def test_host_steps_count(self):
        """1 c_helper + 3 klipper_mcu = 4 total steps."""
        steps = host_steps(_REPO_ROOT, _EPOCH, toolchain_root=_FAKE_TC)
        assert len(steps) == 4, f"Expected 4 host steps, got {len(steps)}"

    def test_host_steps_order_c_helper_first(self):
        """c_helper step must come before klipper_mcu steps."""
        steps = host_steps(_REPO_ROOT, _EPOCH, toolchain_root=_FAKE_TC)
        # c_helper step produces c_helper.so; klipper_mcu produces klipper.elf
        assert steps[0].output_path.name == "c_helper.so", (
            f"First step must be c_helper, got {steps[0].output_path.name}"
        )

    def test_host_steps_equals_concat(self):
        """host_steps output must equal c_helper_steps + klipper_mcu_steps by name."""
        c_steps = c_helper_steps(_REPO_ROOT, _EPOCH, toolchain_root=_FAKE_TC)
        k_steps = klipper_mcu_steps(_REPO_ROOT, _EPOCH, toolchain_root=_FAKE_TC)
        h_steps = host_steps(_REPO_ROOT, _EPOCH, toolchain_root=_FAKE_TC)

        expected_names = [s.name for s in c_steps + k_steps]
        actual_names = [s.name for s in h_steps]
        assert actual_names == expected_names, (
            f"host_steps names don't match concat:\n  expected: {expected_names}\n  actual:   {actual_names}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# A4-6: host_steps + FakeRunner → run_steps completes without error
# ──────────────────────────────────────────────────────────────────────────────

class TestHostStepsFakeRunner:
    """run_steps with FakeRunner must complete all 4 steps without raising.

    No real files exist so check_abi is not invoked on non-existent output.
    Confirms the wiring shape without any real build.
    """

    def test_fake_runner_completes_all_steps(self):
        steps = host_steps(_REPO_ROOT, _EPOCH, toolchain_root=_FAKE_TC)
        results = run_steps(steps, FakeRunner())
        assert len(results) == 4
        assert all(sr.ok for sr in results)

    def test_step_results_ok_for_all_steps(self):
        """All 4 steps must succeed with FakeRunner (wiring shape check).

        Note: run_steps will attempt check_abi for SHARED_LIBRARY/EXECUTABLE
        steps when output_path exists on disk.  The c_helper.so output file may
        exist from a prior build; if so, check_abi runs against it.  That is
        correct behavior — we only assert all steps succeed and return results.
        """
        steps = host_steps(_REPO_ROOT, _EPOCH, toolchain_root=_FAKE_TC)
        results = run_steps(steps, FakeRunner())
        for sr in results:
            assert sr.ok, f"Step '{sr.name}' unexpectedly failed"
