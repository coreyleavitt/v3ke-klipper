"""
build-toolchain-image.yml specifics — hermetic, offline.

Tests here cover only build-toolchain-image.yml structure.
Cross-workflow invariants (schema, SHA-pins, top-perms, no-write-all)
are in test_workflows_common.py.

Coverage:
  - Triggers: workflow_dispatch + path-scoped push (toolchain/** + workflow file itself).
  - Job permissions: packages:write + contents:read, no extra writes.
  - Build step: references toolchain/Containerfile + ghcr.io/coreyleavitt/v3ke-toolchain.
  - Digest surfaced: $GITHUB_OUTPUT or $GITHUB_STEP_SUMMARY (downstream pull-by-digest).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _workflow_helpers import (
    all_steps,
    get_triggers,
    load_workflow,
    repo_root,
    steps_contain,
)

REPO_ROOT = repo_root()
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "build-toolchain-image.yml"
OWNER = "coreyleavitt"


@pytest.fixture(scope="module")
def wf() -> dict:
    assert WORKFLOW_PATH.exists(), f"Workflow not found: {WORKFLOW_PATH}"
    return load_workflow(WORKFLOW_PATH)


@pytest.fixture(scope="module")
def build_job(wf: dict) -> dict:
    jobs = wf.get("jobs", {})
    assert jobs, "build-toolchain-image.yml must define at least one job"
    return next(iter(jobs.values()))


@pytest.fixture(scope="module")
def job_steps(build_job: dict) -> list[dict]:
    return build_job.get("steps", [])


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


def test_trigger_workflow_dispatch(wf: dict):
    """workflow_dispatch is required for manual out-of-band rebuilds."""
    on = get_triggers(wf)
    assert "workflow_dispatch" in on, (
        "build-toolchain-image.yml: workflow_dispatch trigger is required"
    )


def test_trigger_push_path_scoped_to_toolchain(wf: dict):
    """The push trigger must be path-scoped to toolchain/** (builds are expensive)."""
    push = get_triggers(wf).get("push", {}) or {}
    paths = push.get("paths", [])
    assert any("toolchain" in p for p in paths), (
        f"push trigger must include toolchain/** in paths; got: {paths}"
    )


def test_trigger_push_path_includes_workflow_self(wf: dict):
    """The push trigger must include this workflow file itself (catch YAML misconfigs early)."""
    push = get_triggers(wf).get("push", {}) or {}
    paths = push.get("paths", [])
    assert any("build-toolchain-image" in p for p in paths), (
        f"push.paths must include the workflow file itself; got: {paths}"
    )


# ---------------------------------------------------------------------------
# Job permissions (least-privilege)
# ---------------------------------------------------------------------------


def test_job_permissions_packages_write(build_job: dict):
    """Job must have packages:write to push to ghcr."""
    perms = build_job.get("permissions", {})
    assert perms.get("packages") == "write", (
        f"build-push job must have packages: write; got: {perms}"
    )


def test_job_permissions_contents_read(build_job: dict):
    """Job must have contents:read for checkout."""
    perms = build_job.get("permissions", {})
    assert perms.get("contents") == "read", (
        f"build-push job must have contents: read; got: {perms}"
    )


def test_job_no_extra_write_permissions(build_job: dict):
    """Only packages:write is allowed; no other scope may have write."""
    perms = build_job.get("permissions", {})
    for scope, level in perms.items():
        if level == "write":
            assert scope == "packages", (
                f"build-push job has unexpected write permission: {scope}: write\n"
                f"Only packages: write is permitted; full perms: {perms}"
            )


# ---------------------------------------------------------------------------
# Build step: Containerfile + correct ghcr namespace
# ---------------------------------------------------------------------------


def test_containerfile_referenced(job_steps: list[dict]):
    """A step must reference toolchain/Containerfile (the cross-toolchain definition)."""
    found = steps_contain(job_steps, "Containerfile")
    assert found, (
        "No step references toolchain/Containerfile; "
        "the build step must specify file: toolchain/Containerfile"
    )


def test_ghcr_image_ref_matches_owner(job_steps: list[dict]):
    """Image tag must be under ghcr.io/coreyleavitt/v3ke-toolchain."""
    expected = f"ghcr.io/{OWNER}/v3ke-toolchain"
    found = steps_contain(job_steps, expected)
    assert found, (
        f"No step references {expected!r}; "
        f"image tags must be under ghcr.io/{OWNER}/v3ke-toolchain"
    )


# ---------------------------------------------------------------------------
# Digest surfaced for downstream consumption
# ---------------------------------------------------------------------------


def test_digest_surfaced(job_steps: list[dict]):
    """
    A step must emit the pushed image digest via $GITHUB_OUTPUT or $GITHUB_STEP_SUMMARY
    so downstream jobs (build, repro) can pull by immutable digest rather than :latest.
    """
    digest_signals = ("digest", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY")
    found = any(
        any(sig in str(step.get("run", "")) + str(step.get("with", "")) for sig in digest_signals)
        for step in job_steps
    )
    assert found, (
        "No step surfaces the image digest (via $GITHUB_OUTPUT or $GITHUB_STEP_SUMMARY); "
        "the digest must be emitted for downstream digest-pinned pull references"
    )
