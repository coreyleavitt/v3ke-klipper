"""
ci.yml specifics — hermetic, offline.

Merges the behavioral assertions from test_workflows_b2.py (test job),
test_workflows_b3.py (build job), and test_workflows_b4.py (repro + repro-compare jobs).
Cross-workflow invariants (schema, SHA-pins, top-perms, no-write-all) are in
test_workflows_common.py and are NOT repeated here.

Coverage:
  test job:
    - Triggers: push→main, pull_request, workflow_dispatch.
    - Checkout: fetch-depth:0, submodules:recursive.
    - Submodule-integrity gate: greps git submodule status for +/-/U.
    - Python suite: pytest -m "not integration" inside v3ke-dev (NOT toolchain image).
    - Nim suite: nim c -r (NOT nimble test) tharness+tabi inside v3ke-dev.
    - test job: no if: gating (runs on every push/PR).
  build job:
    - Gated: if: workflow_dispatch || ref==main (NOT every PR — 61 GB image).
    - Permissions: packages:read + contents:read.
    - Digest-pinned pull from toolchain/IMAGE_DIGEST + inline fallback.
    - Orchestrator invoked: build.py artifacts with --runtime docker.
    - Artifact upload via SHA-pinned actions/upload-artifact.
  repro job:
    - strategy.matrix with ≥2 values (two independent runners).
    - Both instances pull same digest from toolchain/IMAGE_DIGEST.
    - Gated off PRs.
    - Permissions: packages:read + contents:read.
  repro-compare job:
    - needs: repro; sha256 comparison present; diffoscope referenced.
  toolchain/IMAGE_DIGEST:
    - File exists and contains a valid sha256:<64-hex> line.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest

from _workflow_helpers import (
    all_steps,
    assert_uses_sha_pinned,
    find_step_by_uses_prefix,
    get_triggers,
    job_perms,
    load_workflow,
    repo_root,
    step_index,
    steps_contain,
)

REPO_ROOT = repo_root()
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
IMAGE_DIGEST_PATH = REPO_ROOT / "toolchain" / "IMAGE_DIGEST"
GHCR_REPO = "ghcr.io/coreyleavitt/v3ke-toolchain"


@pytest.fixture(scope="module")
def wf() -> dict:
    assert WORKFLOW_PATH.exists(), f"Workflow not found: {WORKFLOW_PATH}"
    return load_workflow(WORKFLOW_PATH)


@pytest.fixture(scope="module")
def test_job(wf: dict) -> dict:
    jobs = wf.get("jobs", {})
    assert "test" in jobs, f"No 'test' job in ci.yml; jobs: {list(jobs)}"
    return jobs["test"]


@pytest.fixture(scope="module")
def test_steps(test_job: dict) -> list[dict]:
    return test_job.get("steps", [])


@pytest.fixture(scope="module")
def build_job(wf: dict) -> dict:
    jobs = wf.get("jobs", {})
    assert "build" in jobs, f"No 'build' job in ci.yml; jobs: {list(jobs)}"
    return jobs["build"]


@pytest.fixture(scope="module")
def build_steps(build_job: dict) -> list[dict]:
    return build_job.get("steps", [])


@pytest.fixture(scope="module")
def repro_job(wf: dict) -> dict:
    jobs = wf.get("jobs", {})
    assert "repro" in jobs, (
        f"No 'repro' job in ci.yml; jobs: {list(jobs)}\n"
        "B4 requires a 'repro' job for the reproducibility proof."
    )
    return jobs["repro"]


@pytest.fixture(scope="module")
def repro_steps(repro_job: dict) -> list[dict]:
    return repro_job.get("steps", [])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _all_repro_related_steps(wf: dict) -> list[dict]:
    """Steps from all jobs whose key/name contains 'repro', plus jobs that need repro."""
    out: list[dict] = []
    for key, job in wf.get("jobs", {}).items():
        if "repro" in key or "repro" in str(job.get("name", "")).lower():
            out.extend(job.get("steps", []))
        needs = job.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        if any("repro" in n for n in needs):
            out.extend(job.get("steps", []))
    return out


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


def test_trigger_push_main(wf: dict):
    """CI must run on push to main."""
    push = get_triggers(wf).get("push", {}) or {}
    assert "main" in push.get("branches", []), (
        "ci.yml push trigger must target 'main'"
    )


def test_trigger_pull_request(wf: dict):
    """CI must run on every pull_request (surface failures before merge)."""
    assert "pull_request" in get_triggers(wf), (
        "ci.yml must have a pull_request trigger"
    )


def test_trigger_workflow_dispatch(wf: dict):
    """workflow_dispatch is required for manual re-runs."""
    assert "workflow_dispatch" in get_triggers(wf), (
        "ci.yml must have a workflow_dispatch trigger"
    )


# ---------------------------------------------------------------------------
# All expected jobs present
# ---------------------------------------------------------------------------


def test_test_job_present(wf: dict):
    assert "test" in wf.get("jobs", {}), "ci.yml must have a 'test' job"


def test_build_job_present(wf: dict):
    assert "build" in wf.get("jobs", {}), "ci.yml must have a 'build' job"


def test_repro_job_present(wf: dict):
    assert "repro" in wf.get("jobs", {}), "ci.yml must have a 'repro' job"


# ---------------------------------------------------------------------------
# test job — checkout
# ---------------------------------------------------------------------------


def test_checkout_fetch_depth_zero(test_steps: list[dict]):
    """Full history (fetch-depth:0) required by C1 git-describe versioning."""
    step = find_step_by_uses_prefix(test_steps, "actions/checkout")
    assert step is not None, "test job must have an actions/checkout step"
    fd = (step.get("with") or {}).get("fetch-depth")
    assert fd == 0, f"test job checkout must set fetch-depth: 0; got: {fd!r}"


def test_checkout_submodules_recursive(test_steps: list[dict]):
    """external/ submodules (klipper, katapult) must be populated for fixture tests."""
    step = find_step_by_uses_prefix(test_steps, "actions/checkout")
    assert step is not None
    submodules = (step.get("with") or {}).get("submodules")
    assert submodules in ("recursive", True, "true"), (
        f"test job checkout must set submodules: recursive; got: {submodules!r}"
    )


# ---------------------------------------------------------------------------
# test job — submodule-integrity gate (security: detects +/-/U prefix drift)
# ---------------------------------------------------------------------------


def test_submodule_integrity_gate_present(test_steps: list[dict]):
    """
    A step must run 'git submodule status' and fail on +/-/U prefixes.
    This catches stale --remote bumps, uninitialised submodules, and conflicts.
    Without it, a force-pushed upstream tag would silently swap pinned source.
    """
    found = any(
        "git submodule status" in str(s.get("run", ""))
        and "+" in str(s.get("run", ""))
        for s in test_steps
    )
    assert found, (
        "test job must have a submodule-integrity gate step that runs "
        "'git submodule status --recursive' and checks for the '+' prefix "
        "(checked-out commit != pinned index commit)"
    )


# ---------------------------------------------------------------------------
# test job — Python suite: pytest -m "not integration" in v3ke-dev
# ---------------------------------------------------------------------------


def test_pytest_not_integration_in_v3ke_dev(test_steps: list[dict]):
    """
    pytest must run with -m 'not integration' inside the v3ke-dev image.
    'not integration' excludes toolchain-image-gated tests from the unit CI job.
    v3ke-dev (not v3ke-toolchain) is the correct image — the unit suite is lightweight.
    """
    found = any(
        "pytest" in str(s.get("run", ""))
        and "not integration" in str(s.get("run", ""))
        and "v3ke-dev" in str(s.get("run", ""))
        for s in test_steps
    )
    assert found, (
        "test job must run 'pytest -m \"not integration\"' inside the v3ke-dev image"
    )


def test_pytest_not_in_toolchain_image(test_steps: list[dict]):
    """pytest must NOT run in v3ke-toolchain (unit suite needs only v3ke-dev)."""
    for step in test_steps:
        run = str(step.get("run", ""))
        if "pytest" in run:
            assert "v3ke-toolchain" not in run, (
                "pytest step must not reference v3ke-toolchain; only v3ke-dev is needed"
            )


# ---------------------------------------------------------------------------
# test job — Nim suite: nim c -r (NOT nimble test) in v3ke-dev
# ---------------------------------------------------------------------------


def test_nim_uses_direct_nim_not_nimble(test_steps: list[dict]):
    """
    'nimble test' downloads stock nim from the network (~16 MB per run), breaking
    --network=none hermeticity.  The canonical command is 'nim c --hints:off -r'.
    """
    for step in test_steps:
        run = str(step.get("run", ""))
        if "tharness" in run or "tabi" in run:
            assert "nimble test" not in run, (
                "Nim test step must not use 'nimble test' — it downloads stock nim from "
                "the network on every run (breaks --network=none hermeticity).\n"
                "Use 'nim c --hints:off --path:. -r tests/t*.nim' instead."
            )


def test_nim_runs_tharness_and_tabi_in_v3ke_dev(test_steps: list[dict]):
    """tharness.nim + tabi.nim must both be driven inside v3ke-dev."""
    found_tharness = any(
        "tharness" in str(s.get("run", "")) and "v3ke-dev" in str(s.get("run", ""))
        for s in test_steps
    )
    found_tabi = any("tabi" in str(s.get("run", "")) for s in test_steps)
    assert found_tharness, "Nim test step must run tharness.nim inside v3ke-dev"
    assert found_tabi, "Nim test step must also run tabi.nim"


# ---------------------------------------------------------------------------
# test job — no if: gating (must run on every push/PR)
# ---------------------------------------------------------------------------


def test_test_job_not_gated(test_job: dict):
    """The test job must not have an if: condition — it runs unconditionally."""
    assert "if" not in test_job, (
        "test job must not have an if: condition; it must run on every push/PR.\n"
        "Only the expensive build/repro jobs are gated."
    )


# ---------------------------------------------------------------------------
# build job — gating (NOT on every PR)
# ---------------------------------------------------------------------------


def test_build_job_gated_off_prs(build_job: dict):
    """
    The build job must have an if: condition that excludes pull_request events.
    The v3ke-toolchain image is ~61 GB; per-PR builds are prohibitively expensive.
    """
    condition = str(build_job.get("if", ""))
    assert condition, (
        "build job must have an if: condition to gate off PRs (61 GB image cost)"
    )
    allows_dispatch_or_main = "workflow_dispatch" in condition or "main" in condition
    assert allows_dispatch_or_main, (
        f"build job if: must gate on workflow_dispatch or main; got: {condition!r}"
    )


# ---------------------------------------------------------------------------
# build job — permissions
# ---------------------------------------------------------------------------


def test_build_job_permissions(build_job: dict):
    """build job must have packages:read + contents:read, no write scopes."""
    perms = build_job.get("permissions", {})
    assert perms.get("packages") == "read", (
        f"build job must have packages: read (to pull from ghcr); got: {perms}"
    )
    assert perms.get("contents") == "read", (
        f"build job must have contents: read (for checkout); got: {perms}"
    )
    for scope, level in perms.items():
        assert level != "write", (
            f"build job must not have any write permissions; got {scope}: write\n"
            f"Full perms: {perms}"
        )


# ---------------------------------------------------------------------------
# build job — digest-pinned pull + fallback + IMAGE_DIGEST file
# ---------------------------------------------------------------------------


def test_image_digest_file_exists():
    """toolchain/IMAGE_DIGEST must exist as a repo-tracked file."""
    assert IMAGE_DIGEST_PATH.exists(), (
        f"toolchain/IMAGE_DIGEST not found at {IMAGE_DIGEST_PATH}.\n"
        "This file parameterises the digest-pinned image pull in the build and repro jobs."
    )


def test_image_digest_file_format():
    """IMAGE_DIGEST must contain a valid sha256:<64-hex> line."""
    content = IMAGE_DIGEST_PATH.read_text()
    sha_lines = [
        l.strip() for l in content.splitlines() if l.strip().startswith("sha256:")
    ]
    assert sha_lines, (
        f"toolchain/IMAGE_DIGEST must contain a 'sha256:...' line; contents:\n{content}"
    )
    sha_re = re.compile(r"^sha256:[0-9a-f]{64}$")
    for line in sha_lines:
        assert sha_re.match(line), (
            f"sha256: line has wrong format: {line!r}\n"
            "Expected: sha256:<64 lowercase hex chars>"
        )


def test_build_job_references_image_digest(build_steps: list[dict]):
    """A build job step must read toolchain/IMAGE_DIGEST to form the digest pull ref."""
    assert steps_contain(build_steps, "IMAGE_DIGEST"), (
        "build job must reference IMAGE_DIGEST to source the digest-pinned pull reference"
    )


def test_build_job_inline_fallback(build_steps: list[dict]):
    """
    An inline fallback step must build the toolchain image from toolchain/Containerfile
    when the digest pull fails (ghcr unavailable, placeholder digest).
    This makes the build job self-healing.
    """
    found = any(
        "docker build" in str(s.get("run", ""))
        and "Containerfile" in str(s.get("run", ""))
        and "toolchain" in str(s.get("run", ""))
        for s in build_steps
    )
    assert found, (
        "build job must have an inline fallback 'docker build ... toolchain/Containerfile' step"
    )


# ---------------------------------------------------------------------------
# build job — orchestrator invocation + artifact upload
# ---------------------------------------------------------------------------


def test_build_job_invokes_orchestrator_with_docker(build_steps: list[dict]):
    """
    The build job must invoke 'build.py artifacts' with --runtime docker.
    GitHub-hosted runners use Docker (not podman); the flag must be explicit.
    """
    found = any(
        "build.py" in str(s.get("run", ""))
        and "artifacts" in str(s.get("run", ""))
        and "docker" in str(s.get("run", ""))
        for s in build_steps
    )
    assert found, (
        "build job must invoke 'build.py ... artifacts' with --runtime docker"
    )


def test_build_job_uploads_artifacts(build_steps: list[dict]):
    """build job must upload artifacts via SHA-pinned actions/upload-artifact."""
    step = find_step_by_uses_prefix(build_steps, "actions/upload-artifact")
    assert step is not None, (
        "build job must have an actions/upload-artifact step "
        "(so repro/release jobs can consume artifacts without rebuilding)"
    )
    assert_uses_sha_pinned(step)


# ---------------------------------------------------------------------------
# repro job — matrix strategy (two independent runners)
# ---------------------------------------------------------------------------


def test_repro_job_matrix_strategy(repro_job: dict):
    """
    The repro job must use strategy.matrix so GitHub schedules two independent runners.
    A single runner that builds twice would share temp files, caches, and process state,
    invalidating the cross-environment reproducibility proof.
    """
    matrix = repro_job.get("strategy", {}).get("matrix", {})
    assert matrix, (
        "repro job must use strategy.matrix (two independent runners for the repro proof)"
    )
    list_vals = [v for v in matrix.values() if isinstance(v, list)]
    assert any(len(v) >= 2 for v in list_vals), (
        f"repro job matrix must have at least 2 values (two builds); got: {matrix}"
    )


# ---------------------------------------------------------------------------
# repro job — permissions + gating
# ---------------------------------------------------------------------------


def test_repro_job_permissions(repro_job: dict):
    """repro job must have packages:read + contents:read, no write scopes."""
    perms = repro_job.get("permissions", {})
    assert perms.get("packages") == "read", (
        f"repro job must have packages: read; got: {perms}"
    )
    assert perms.get("contents") == "read", (
        f"repro job must have contents: read; got: {perms}"
    )
    for scope, level in perms.items():
        assert level != "write", (
            f"repro job must not have write permissions; got {scope}: write"
        )


def test_repro_job_gated_off_prs(repro_job: dict):
    """repro job must not run on every PR (2× toolchain build is expensive)."""
    condition = str(repro_job.get("if", ""))
    assert condition, "repro job must have an if: condition to gate off PRs"
    allows_dispatch_main_or_tag = (
        "workflow_dispatch" in condition or "main" in condition or "tag" in condition
    )
    assert allows_dispatch_main_or_tag, (
        f"repro job if: must gate on dispatch/main/tags; got: {condition!r}"
    )


def test_repro_job_references_image_digest(repro_steps: list[dict]):
    """Both repro instances must pull by the same digest from toolchain/IMAGE_DIGEST."""
    assert steps_contain(repro_steps, "IMAGE_DIGEST"), (
        "repro job must reference IMAGE_DIGEST so both matrix instances pull the same digest"
    )


# ---------------------------------------------------------------------------
# repro-compare job — sha256 comparison + diffoscope (security: proves reproducibility)
# ---------------------------------------------------------------------------


def test_repro_compare_sha256_and_diffoscope(wf: dict):
    """
    The repro-compare job (or any repro-related job) must compare sha256 manifests
    and reference diffoscope for mismatch diagnosis.
    diffoscope provides byte-level diff on mismatch — without it non-determinism
    is reported but not diagnosable.
    """
    repro_steps = _all_repro_related_steps(wf)
    has_sha256 = any(
        "sha256" in str(s.get("run", "")).lower() for s in repro_steps
    )
    has_diffoscope = any(
        "diffoscope" in str(s.get("run", "")).lower() for s in repro_steps
    )
    assert has_sha256, (
        "repro/repro-compare job must compare sha256 checksums across both builds"
    )
    assert has_diffoscope, (
        "repro/repro-compare job must reference diffoscope for mismatch diagnosis"
    )
