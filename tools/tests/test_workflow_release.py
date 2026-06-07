"""
release.yml specifics — hermetic, offline.

Covers only release.yml structure.  Cross-workflow invariants (schema, SHA-pins,
top-perms, no-write-all) are in test_workflows_common.py.

Security-critical assertions here:
  - Release job permissions are EXACTLY contents:write + id-token:write + packages:read
    (nothing broader — enforces the principle of least privilege for release signing).
  - cosign sign step is NOT continue-on-error (signing failure must abort the release).
  - repro-gate step (repro-check.sh) precedes the packaging step (ordering matters).
  - --prerelease is conditionally set for v0.* tags.
  - gh release create uses --generate-notes + --fail-if-exists.
  - SHA256SUMS computation covers manifest.json (cosign signature transitively
    authenticates build provenance).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _workflow_helpers import (
    all_steps,
    assert_uses_sha_pinned,
    find_step_by_uses_prefix,
    get_triggers,
    load_workflow,
    repo_root,
    step_index,
    steps_contain,
)

REPO_ROOT = repo_root()
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release.yml"


@pytest.fixture(scope="module")
def wf() -> dict:
    assert WORKFLOW_PATH.exists(), f"Workflow not found: {WORKFLOW_PATH}"
    return load_workflow(WORKFLOW_PATH)


@pytest.fixture(scope="module")
def release_job(wf: dict) -> dict:
    jobs = wf.get("jobs", {})
    assert "release" in jobs, f"No 'release' job in release.yml; jobs: {list(jobs)}"
    return jobs["release"]


@pytest.fixture(scope="module")
def release_steps(release_job: dict) -> list[dict]:
    return release_job.get("steps", [])


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


def test_trigger_push_tags_v_star(wf: dict):
    """release.yml must fire on push to v* tags."""
    push = get_triggers(wf).get("push", {}) or {}
    tags = push.get("tags", [])
    assert any("v*" in str(t) for t in tags), (
        f"release.yml push trigger must include a 'v*' tag pattern; got: {tags}"
    )


def test_trigger_workflow_dispatch(wf: dict):
    """workflow_dispatch allows dry-run without pushing a real tag."""
    assert "workflow_dispatch" in get_triggers(wf), (
        "release.yml must have a workflow_dispatch trigger"
    )


# ---------------------------------------------------------------------------
# Release job permissions — EXACT least-privilege set (security-critical)
# ---------------------------------------------------------------------------


def test_release_job_permissions_exact(release_job: dict):
    """
    The release job must have EXACTLY:
      contents: write  — create the GitHub Release and upload assets
      id-token: write  — cosign keyless OIDC signing (ambient credentials)
      packages: read   — pull the toolchain image from ghcr
    Any broader write grant would violate least-privilege for a release job.
    """
    perms = release_job.get("permissions", {})
    assert perms.get("contents") == "write", (
        f"release job must have contents: write; got: {perms}"
    )
    assert perms.get("id-token") == "write", (
        f"release job must have id-token: write (for cosign OIDC); got: {perms}"
    )
    assert perms.get("packages") == "read", (
        f"release job must have packages: read; got: {perms}"
    )
    allowed_writes = {"contents", "id-token"}
    for scope, level in perms.items():
        if level == "write":
            assert scope in allowed_writes, (
                f"release job has unexpected write permission: {scope}: write\n"
                f"Allowed write scopes: {allowed_writes}\n"
                f"Full permissions: {perms}"
            )


# ---------------------------------------------------------------------------
# Repro gate precedes packaging (ordering assertion — security-critical)
# ---------------------------------------------------------------------------


def test_repro_gate_before_packaging(release_steps: list[dict]):
    """
    The repro-gate step (scripts/repro-check.sh) must appear BEFORE the packaging
    step (build.py … release).  A non-reproducible build must never be packaged or
    published; the gate must abort the job on sha256 divergence.
    """
    repro_idx = step_index(release_steps, "repro-check.sh")
    pkg_idx = step_index(release_steps, "build.py", "release")
    assert repro_idx != -1, (
        "No repro-gate step found (scripts/repro-check.sh) in release job"
    )
    assert pkg_idx != -1, (
        "No packaging step found (build.py ... release) in release job"
    )
    assert repro_idx < pkg_idx, (
        f"repro-gate step (index {repro_idx}) must precede the packaging step "
        f"(index {pkg_idx}); a non-reproducible build must never be packaged"
    )


# ---------------------------------------------------------------------------
# SHA256SUMS covers manifest.json
# ---------------------------------------------------------------------------


def test_sha256sums_covers_manifest_json(release_steps: list[dict]):
    """
    The SHA256SUMS computation must include manifest.json so the cosign signature
    transitively authenticates the build provenance record.
    """
    for step in release_steps:
        run = str(step.get("run", ""))
        if "SHA256SUMS" in run and "sha256sum" in run:
            assert "manifest.json" in run, (
                "SHA256SUMS computation step must include manifest.json.\n"
                "The cosign signature covers SHA256SUMS, which must cover manifest.json "
                "to authenticate the build provenance record.\n"
                f"Step run:\n{run}"
            )
            return
    pytest.fail(
        "No step found that computes sha256sum and writes SHA256SUMS in the release job"
    )


# ---------------------------------------------------------------------------
# cosign: installed via SHA-pinned action + sign step NOT continue-on-error
# ---------------------------------------------------------------------------


def test_cosign_installed_via_sha_pinned_action(release_steps: list[dict]):
    """
    cosign must be installed via sigstore/cosign-installer (SHA-pinned).
    The installer action puts the cosign binary on PATH without a separate download step.
    """
    found = steps_contain(release_steps, "sigstore/cosign-installer")
    assert found, (
        "release job must install cosign via sigstore/cosign-installer (SHA-pinned action)"
    )
    for step in release_steps:
        if "sigstore/cosign-installer" in step.get("uses", ""):
            assert_uses_sha_pinned(step)
            return


def test_cosign_sign_step_not_continue_on_error(release_steps: list[dict]):
    """
    The cosign sign-blob step must NOT be continue-on-error: true.
    Signing failure must abort the release — releasing unsigned assets is not permitted.
    This is a hard supply-chain invariant: if the OIDC token is unavailable or cosign
    fails for any reason, the release must stop rather than publish unsigned files.
    """
    for step in release_steps:
        if "cosign sign-blob" in str(step.get("run", "")):
            coe = step.get("continue-on-error")
            assert coe is not True, (
                "cosign sign-blob step must NOT have continue-on-error: true.\n"
                "Signing failure must abort the release entirely.\n"
                f"Step: {step}"
            )
            return
    pytest.fail("No 'cosign sign-blob' step found in the release job")


def test_cosign_sign_produces_sha256sums_sig(release_steps: list[dict]):
    """cosign sign-blob must produce SHA256SUMS.sig (the file uploaded in the release)."""
    found = steps_contain(release_steps, "cosign sign-blob", "SHA256SUMS.sig")
    assert found, (
        "cosign sign-blob step must produce SHA256SUMS.sig\n"
        "Expected: cosign sign-blob --output-signature dist/SHA256SUMS.sig dist/SHA256SUMS"
    )


# ---------------------------------------------------------------------------
# gh release create — flags and pre-release handling
# ---------------------------------------------------------------------------


def test_gh_release_create_generate_notes(release_steps: list[dict]):
    """--generate-notes auto-generates a changelog from merged PRs since the last tag."""
    found = steps_contain(release_steps, "gh release create", "--generate-notes")
    assert found, (
        "'gh release create' must include --generate-notes "
        "(auto-generates a changelog from merged PRs)"
    )


def test_gh_release_create_fail_if_exists(release_steps: list[dict]):
    """--fail-if-exists prevents silently overwriting an existing release."""
    found = steps_contain(release_steps, "gh release create", "--fail-if-exists")
    assert found, (
        "'gh release create' must include --fail-if-exists "
        "(idempotency guard against re-pushed tags)"
    )


def test_prerelease_flag_conditional_on_v0(release_steps: list[dict]):
    """
    The --prerelease flag must be conditionally set for v0.* tags.
    Tags matching v0.* are pre-1.0 and must be marked as pre-releases on GitHub.
    """
    found_v0_check = any(
        "v0." in str(s.get("run", "")) or "v0." in str(s.get("env", ""))
        for s in release_steps
    )
    assert found_v0_check, (
        "release job must check for v0.* tag pattern to set --prerelease conditionally"
    )
    found_prerelease = steps_contain(release_steps, "--prerelease")
    assert found_prerelease, (
        "release job must use --prerelease flag (conditionally for v0.* tags)"
    )
