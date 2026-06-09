"""
release.yaml specifics — hermetic, offline.

Covers only release.yaml structure.  Cross-workflow invariants (schema, SHA-pins,
top-perms, no-write-all) are in test_workflows_common.py.

New contract (nopal dispatch-button model):
  - workflow_dispatch ONLY (no tag-push trigger — the workflow CREATES the tag).
  - Two jobs: prepare-version (contents:write) + release (contents:write,
    id-token:write, packages:read).
  - prepare-version writes VERSION and creates a signed tag via the Git Data API.
  - release checks out the new tag, runs the submodule gate, pulls toolchain by
    digest (hard fail, no fallback, rejects all-zeros placeholder), runs repro-check,
    explicitly builds device artifacts, builds the v3ke CLI via the pinned Nim image,
    sets up uv, packages under `uv run` (provides jsonschema), SHA256SUMs (covering
    manifest.json), cosign-signs (not continue-on-error), and gh release create
    --generate-notes.
  - NO continue-on-error anywhere.
  - NO inline toolchain-build fallback step.
  - Prerelease flag driven by -alpha/-beta/-rc suffix, NOT v0.* pattern.

Security-critical assertions:
  - Release job permissions EXACTLY: contents:write + id-token:write + packages:read.
  - cosign sign step NOT continue-on-error.
  - repro-gate precedes packaging (ordering).
  - All-zeros digest placeholder explicitly rejected.
  - sha256sum covers manifest.json.
  - gh release create has --generate-notes (and must NOT pass the invalid --fail-if-exists).

Integration-gap fixes (validated locally):
  - tools/v3ke/v3ke is gitignored — must be built in the workflow via the pinned
    Nim image before packaging (Build v3ke CLI step).
  - build.py release requires jsonschema (pinned dev dep in tools/uv.lock) — the
    package step must run under `uv run --project tools`, not bare python3.
  - Device artifacts must be present at packaging time via an explicit build step,
    not as a side effect of the repro gate.
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
    step_run_text,
    steps_contain,
)

REPO_ROOT = repo_root()
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release.yaml"


@pytest.fixture(scope="module")
def wf() -> dict:
    assert WORKFLOW_PATH.exists(), f"Workflow not found: {WORKFLOW_PATH}"
    return load_workflow(WORKFLOW_PATH)


@pytest.fixture(scope="module")
def prepare_version_job(wf: dict) -> dict:
    jobs = wf.get("jobs", {})
    assert "prepare-version" in jobs, (
        f"No 'prepare-version' job in release.yaml; jobs: {list(jobs)}"
    )
    return jobs["prepare-version"]


@pytest.fixture(scope="module")
def release_job(wf: dict) -> dict:
    jobs = wf.get("jobs", {})
    assert "release" in jobs, f"No 'release' job in release.yaml; jobs: {list(jobs)}"
    return jobs["release"]


@pytest.fixture(scope="module")
def prepare_version_steps(prepare_version_job: dict) -> list[dict]:
    return prepare_version_job.get("steps", [])


@pytest.fixture(scope="module")
def release_steps(release_job: dict) -> list[dict]:
    return release_job.get("steps", [])


# ---------------------------------------------------------------------------
# Triggers: workflow_dispatch ONLY — no tag-push trigger
# ---------------------------------------------------------------------------


def test_trigger_workflow_dispatch_present(wf: dict):
    """release.yaml must have workflow_dispatch (the dispatch-button release model)."""
    assert "workflow_dispatch" in get_triggers(wf), (
        "release.yaml must have a workflow_dispatch trigger"
    )


def test_trigger_no_tag_push(wf: dict):
    """
    release.yaml must NOT have a push/tags trigger.
    The workflow itself creates the tag — a tag-push trigger would create an infinite loop
    and also violates the dispatch-button model where humans control when releases happen.
    """
    push = get_triggers(wf).get("push", {}) or {}
    tags = push.get("tags", [])
    assert len(tags) == 0, (
        f"release.yaml must NOT have a tag-push trigger (the workflow creates the tag); "
        f"got push.tags: {tags}"
    )


def test_trigger_bump_input_present(wf: dict):
    """workflow_dispatch must have a 'bump' choice input (none/patch/minor/major/stable)."""
    dispatch = get_triggers(wf).get("workflow_dispatch", {}) or {}
    inputs = dispatch.get("inputs", {}) or {}
    assert "bump" in inputs, (
        f"workflow_dispatch must have a 'bump' input; inputs: {list(inputs)}"
    )
    bump = inputs["bump"]
    assert bump.get("type") == "choice", (
        f"'bump' input must be type: choice; got: {bump.get('type')!r}"
    )
    expected_options = {"none", "patch", "minor", "major", "stable"}
    actual_options = set(bump.get("options", []))
    assert expected_options == actual_options, (
        f"'bump' input options must be {expected_options}; got: {actual_options}"
    )


def test_trigger_prerelease_input_present(wf: dict):
    """workflow_dispatch must have a 'prerelease' choice input (no/alpha/beta/rc)."""
    dispatch = get_triggers(wf).get("workflow_dispatch", {}) or {}
    inputs = dispatch.get("inputs", {}) or {}
    assert "prerelease" in inputs, (
        f"workflow_dispatch must have a 'prerelease' input; inputs: {list(inputs)}"
    )
    pr_input = inputs["prerelease"]
    assert pr_input.get("type") == "choice", (
        f"'prerelease' input must be type: choice; got: {pr_input.get('type')!r}"
    )
    expected_options = {"no", "alpha", "beta", "rc"}
    actual_options = set(pr_input.get("options", []))
    assert expected_options == actual_options, (
        f"'prerelease' input options must be {expected_options}; got: {actual_options}"
    )


# ---------------------------------------------------------------------------
# prepare-version job
# ---------------------------------------------------------------------------


def test_prepare_version_job_permissions(prepare_version_job: dict):
    """prepare-version job must have contents: write (to push the version commit + tag)."""
    perms = prepare_version_job.get("permissions", {})
    assert perms.get("contents") == "write", (
        f"prepare-version job must have contents: write; got: {perms}"
    )


def test_prepare_version_writes_version_file(prepare_version_steps: list[dict]):
    """
    prepare-version must write the VERSION file.
    The signed commit created via the Git Data API must include the updated VERSION file.
    """
    found = steps_contain(prepare_version_steps, "VERSION")
    assert found, (
        "prepare-version job must have a step that writes the VERSION file"
    )


def test_prepare_version_creates_tag_via_git_data_api(prepare_version_steps: list[dict]):
    """
    prepare-version must create the release tag via the GitHub Git Data API
    (gh api .../git/refs POST), yielding a 'Verified' commit using only GITHUB_TOKEN.
    This is the nopal pattern: signed commits without a bot GPG key.
    """
    # The tag creation call: POST to /git/refs with ref="refs/tags/..."
    found = steps_contain(prepare_version_steps, "git/refs")
    assert found, (
        "prepare-version must create the tag via the GitHub Git Data API "
        "(gh api .../git/refs) — the nopal signed-commit pattern"
    )


def test_prepare_version_creates_commit_via_git_data_api(prepare_version_steps: list[dict]):
    """
    prepare-version must create a commit via the Git Data API
    (gh api .../git/commits) so the release commit is 'Verified' on GitHub.
    """
    found = steps_contain(prepare_version_steps, "git/commits")
    assert found, (
        "prepare-version must create a signed commit via gh api .../git/commits"
    )


def test_prepare_version_outputs_version_tag_commit_sha(prepare_version_job: dict):
    """
    prepare-version job must declare outputs: version, tag, commit_sha
    so the release job can check out the exact new commit.
    """
    outputs = prepare_version_job.get("outputs", {}) or {}
    for key in ("version", "tag", "commit_sha"):
        assert key in outputs, (
            f"prepare-version job must declare output '{key}'; outputs: {list(outputs)}"
        )


# ---------------------------------------------------------------------------
# release job — structure
# ---------------------------------------------------------------------------


def test_release_job_needs_prepare_version(release_job: dict):
    """release job must declare needs: prepare-version."""
    needs = release_job.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert "prepare-version" in needs, (
        f"release job must need prepare-version; needs: {needs}"
    )


def test_release_job_checkouts_new_tag(release_steps: list[dict]):
    """
    The checkout in the release job must use the tag/commit produced by prepare-version,
    not the triggering ref.  This guarantees the release builds from the version-bumped commit.
    """
    checkout_step = find_step_by_uses_prefix(release_steps, "actions/checkout")
    assert checkout_step is not None, "release job must have a checkout step"
    with_block = checkout_step.get("with", {}) or {}
    ref = str(with_block.get("ref", ""))
    assert "prepare-version" in ref, (
        f"release job checkout must use needs.prepare-version.outputs.tag (or commit_sha); "
        f"got ref: {ref!r}"
    )


def test_release_job_permissions_exact(release_job: dict):
    """
    The release job must have EXACTLY:
      contents: write  — create the GitHub Release and upload assets
      id-token: write  — cosign keyless OIDC signing (ambient credentials)
      packages: read   — pull the toolchain image from ghcr
    Any broader write grant violates least-privilege.
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
# release job — submodule integrity gate
# ---------------------------------------------------------------------------


def test_submodule_integrity_gate_present(release_steps: list[dict]):
    """The release job must include a submodule integrity gate (git submodule status)."""
    found = steps_contain(release_steps, "git submodule status --recursive")
    assert found, (
        "release job must have a submodule integrity gate "
        "('git submodule status --recursive' + check for +/-/U prefixes)"
    )


# ---------------------------------------------------------------------------
# release job — toolchain image pull (hard fail, no fallback, rejects zeros)
# ---------------------------------------------------------------------------


def test_no_continue_on_error_anywhere(wf: dict):
    """
    NO step in any job may have continue-on-error: true.
    The old release.yml had continue-on-error on the digest-pull step to support
    an inline-build fallback — that pattern is explicitly banned in release.yaml.
    Every failure must abort the release.
    """
    for job_name, job in wf.get("jobs", {}).items():
        for step in job.get("steps", []):
            coe = step.get("continue-on-error")
            assert coe is not True, (
                f"Job '{job_name}', step '{step.get('name', step.get('id', '<unnamed>'))}' "
                f"has continue-on-error: true — forbidden in release.yaml.\n"
                f"Every failure must abort the release; no silent skips."
            )


def test_no_inline_toolchain_build_fallback(wf: dict):
    """
    release.yaml must NOT contain an inline toolchain-build fallback step
    (no step that builds from toolchain/Containerfile as a fallback).
    If the digest pull fails, the job must fail — full stop.
    Operators must run 'build.py image --push' before triggering a release.
    """
    for job_name, job in wf.get("jobs", {}).items():
        for step in job.get("steps", []):
            run_text = str(step.get("run", ""))
            # Detect a conditional inline build: if-gated on pull outcome + Containerfile build
            if_cond = str(step.get("if", ""))
            if "Containerfile" in run_text and "toolchain" in run_text.lower():
                pytest.fail(
                    f"Job '{job_name}', step '{step.get('name', '<unnamed>')}' "
                    f"appears to be an inline toolchain-build fallback.\n"
                    f"release.yaml must hard-fail when the digest pull fails — no fallback builds.\n"
                    f"run snippet: {run_text[:200]!r}"
                )


def test_digest_pull_rejects_all_zeros_placeholder(release_steps: list[dict]):
    """
    The digest-pull step must explicitly reject the all-zeros placeholder
    (sha256:0000…0) and emit a clear error directing the operator to run
    'build.py image --push' first.
    This prevents accidentally releasing with an unbuilt or unverified image.
    """
    for step in release_steps:
        run = str(step.get("run", ""))
        if "IMAGE_DIGEST" in run and "docker pull" in run:
            assert "0000" in run or "00000" in run, (
                f"The digest-pull step must explicitly reject the all-zeros placeholder "
                f"(sha256:000...0).\n"
                f"Add a check like: if echo \"$DIGEST\" | grep -q '^sha256:0{{64}}$'; then exit 1.\n"
                f"Step run:\n{run}"
            )
            return
    pytest.fail(
        "No step found that reads IMAGE_DIGEST and does 'docker pull' in the release job"
    )


# ---------------------------------------------------------------------------
# release job — repro gate precedes packaging (ordering — security-critical)
# ---------------------------------------------------------------------------


def test_repro_gate_before_packaging(release_steps: list[dict]):
    """
    The repro-gate step (scripts/repro-check.sh) must appear BEFORE the packaging
    step (build.py … release).  A non-reproducible build must never be packaged or
    published.
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
# release job — explicit device artifact build (ordering)
# ---------------------------------------------------------------------------


def test_artifacts_build_step_present(release_steps: list[dict]):
    """
    The release job must have an explicit step that runs `build.py … artifacts`
    to build device artifacts.  Packaging must not rely on the repro gate's side
    effects — those builds target a temp directory; an explicit artifacts step
    guarantees the expected files are present at their standard paths when the
    package step runs.
    """
    found = steps_contain(release_steps, "build.py", "artifacts")
    assert found, (
        "release job must have a step that runs build.py … artifacts "
        "(explicit device artifact build, not a side effect of repro-check)"
    )


def test_artifacts_build_after_repro_gate(release_steps: list[dict]):
    """
    The explicit artifacts build step must appear AFTER the repro gate and
    BEFORE the packaging step.

    After: the repro gate has already pulled the image and validated
    reproducibility; the artifacts build reuses the same pulled image.
    Before: packaging requires the built artifacts to be present.
    """
    repro_idx = step_index(release_steps, "repro-check.sh")
    artifacts_idx = step_index(release_steps, "build.py", "artifacts")
    pkg_idx = step_index(release_steps, "build.py", "release")

    assert repro_idx != -1, "No repro-gate step found (scripts/repro-check.sh)"
    assert artifacts_idx != -1, "No artifacts build step found (build.py … artifacts)"
    assert pkg_idx != -1, "No packaging step found (build.py … release)"

    assert repro_idx < artifacts_idx, (
        f"artifacts build step (index {artifacts_idx}) must come AFTER the repro gate "
        f"(index {repro_idx})"
    )
    assert artifacts_idx < pkg_idx, (
        f"artifacts build step (index {artifacts_idx}) must come BEFORE the packaging step "
        f"(index {pkg_idx})"
    )


# ---------------------------------------------------------------------------
# release job — v3ke CLI build (integration gap fix)
# ---------------------------------------------------------------------------


def test_v3ke_cli_build_step_present(release_steps: list[dict]):
    """
    The release job must build the v3ke CLI binary before packaging.

    tools/v3ke/v3ke is gitignored (compiled output) and is never present on a
    fresh checkout.  It must be produced here by running `nimble build` inside
    the pinned Nim image; otherwise the package step will fail when it tries to
    include the binary in the release zip.
    """
    found = steps_contain(release_steps, "ghcr.io/coreyleavitt/nim", "nimble build")
    assert found, (
        "release job must build the v3ke CLI (nimble build inside "
        "ghcr.io/coreyleavitt/nim image) before packaging.\n"
        "tools/v3ke/v3ke is gitignored and absent on a fresh checkout."
    )


def test_v3ke_cli_build_before_packaging(release_steps: list[dict]):
    """
    The v3ke CLI build step must appear BEFORE the package step.
    The binary must exist at tools/v3ke/v3ke when build.py release runs.
    """
    nim_idx = step_index(release_steps, "ghcr.io/coreyleavitt/nim", "nimble build")
    pkg_idx = step_index(release_steps, "build.py", "release")

    assert nim_idx != -1, (
        "No v3ke CLI build step found "
        "(ghcr.io/coreyleavitt/nim + nimble build) in release job"
    )
    assert pkg_idx != -1, "No packaging step found (build.py … release) in release job"
    assert nim_idx < pkg_idx, (
        f"v3ke CLI build step (index {nim_idx}) must come BEFORE the packaging step "
        f"(index {pkg_idx}); the binary must exist when build.py release runs"
    )


# ---------------------------------------------------------------------------
# release job — uv setup + package step runs under uv run (integration gap fix)
# ---------------------------------------------------------------------------


def test_setup_uv_step_present(release_steps: list[dict]):
    """
    The release job must install uv via astral-sh/setup-uv (SHA-pinned) before the
    package step.  uv provisions the locked dev deps from tools/uv.lock, including
    jsonschema, which build.py release requires for manifest validation.
    Running the package step under bare python3 fails because jsonschema is not
    installed in the system Python on ubuntu-latest.
    """
    found = steps_contain(release_steps, "astral-sh/setup-uv")
    assert found, (
        "release job must set up uv via astral-sh/setup-uv before the package step "
        "so jsonschema (tools/uv.lock) is available for manifest validation"
    )


def test_setup_uv_sha_pinned(release_steps: list[dict]):
    """astral-sh/setup-uv in the release job must be SHA-pinned (supply-chain safety)."""
    for step in release_steps:
        if "astral-sh/setup-uv" in step.get("uses", ""):
            assert_uses_sha_pinned(step)
            return
    pytest.fail("No astral-sh/setup-uv step found in release job")


def test_setup_uv_before_packaging(release_steps: list[dict]):
    """
    The uv setup step must appear BEFORE the package step.
    uv must be on PATH when the `uv run` command executes.
    """
    uv_idx = step_index(release_steps, "astral-sh/setup-uv")
    pkg_idx = step_index(release_steps, "build.py", "release")

    assert uv_idx != -1, "No astral-sh/setup-uv step found in release job"
    assert pkg_idx != -1, "No packaging step found (build.py … release) in release job"
    assert uv_idx < pkg_idx, (
        f"setup-uv step (index {uv_idx}) must come BEFORE the packaging step "
        f"(index {pkg_idx})"
    )


def test_package_step_uses_uv_run(release_steps: list[dict]):
    """
    The package step must run under `uv run --project tools` rather than bare python3.
    This provisions jsonschema from tools/uv.lock; bare python3 on ubuntu-latest
    does not have jsonschema, causing build.py release to fail at manifest validation.
    """
    found = steps_contain(release_steps, "uv run", "build.py", "release")
    assert found, (
        "The package step must use 'uv run' to invoke build.py release.\n"
        "Bare python3 lacks jsonschema (a pinned dep in tools/uv.lock).\n"
        "Expected: uv run --project tools python tools/build.py … release …"
    )


def test_package_step_not_bare_python3(release_steps: list[dict]):
    """
    The package step must NOT invoke build.py release via bare python3.
    Bare python3 is missing jsonschema; use `uv run --project tools` instead.
    """
    for step in release_steps:
        run = str(step.get("run", ""))
        if "build.py" in run and "release" in run and "--reproducible" in run:
            assert "uv run" in run, (
                "Package step invokes build.py release but does not use `uv run`.\n"
                "Change: python3 tools/build.py → uv run --project tools python tools/build.py\n"
                f"Step run:\n{run}"
            )
            return
    pytest.fail("No packaging step found (build.py … release … --reproducible) in release job")


# ---------------------------------------------------------------------------
# release job — SHA256SUMS covers manifest.json
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
# release job — cosign: SHA-pinned installer + sign NOT continue-on-error
# ---------------------------------------------------------------------------


def test_cosign_installed_via_sha_pinned_action(release_steps: list[dict]):
    """cosign must be installed via sigstore/cosign-installer (SHA-pinned)."""
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
# release job — gh release create flags + prerelease logic
# ---------------------------------------------------------------------------


def test_gh_release_create_generate_notes(release_steps: list[dict]):
    """--generate-notes auto-generates a changelog from merged PRs since the last tag."""
    found = steps_contain(release_steps, "gh release create", "--generate-notes")
    assert found, (
        "'gh release create' must include --generate-notes "
        "(auto-generates a changelog from merged PRs)"
    )


def test_gh_release_create_no_invalid_fail_if_exists(release_steps: list[dict]):
    """Must NOT pass --fail-if-exists: it is not a real `gh release create` flag
    (gh errors on an existing release by default). Passing it makes gh print
    usage and exit 1, failing the publish step."""
    found = steps_contain(release_steps, "gh release create", "--fail-if-exists")
    assert not found, (
        "'gh release create' must not pass --fail-if-exists — it is not a valid "
        "flag and aborts the release; gh already fails if the release exists"
    )


def test_prerelease_flag_driven_by_suffix_not_v0(release_steps: list[dict]):
    """
    The --prerelease flag must be driven by the presence of an -alpha/-beta/-rc suffix
    in the version, NOT by the old 'v0.*' pattern.
    The dispatch-button model uses explicit prerelease inputs; v0.* detection is obsolete.
    """
    # Assert the new suffix-based check is present
    suffix_check = any(
        any(label in str(s.get("run", "")) for label in ("alpha", "beta", "rc"))
        for s in release_steps
    )
    assert suffix_check, (
        "release job must check for -alpha/-beta/-rc suffix to set --prerelease "
        "(not the old v0.* pattern)"
    )
    # Assert the old v0.* pattern is NOT used
    v0_check = any(
        "v0." in str(s.get("run", "")) or "'^v0\\." in str(s.get("run", ""))
        for s in release_steps
    )
    assert not v0_check, (
        "release job must NOT use the v0.* pattern to detect pre-releases.\n"
        "Use the -alpha/-beta/-rc suffix from the version string instead."
    )


def test_gh_release_create_uses_tag_from_prepare_version(release_steps: list[dict]):
    """
    gh release create must use the tag produced by prepare-version
    (needs.prepare-version.outputs.tag), not GITHUB_REF_NAME.
    The release job is triggered by dispatch, not a tag push — GITHUB_REF_NAME
    would be 'main', not the release tag.
    """
    for step in release_steps:
        run = str(step.get("run", ""))
        if "gh release create" in run:
            assert "GITHUB_REF_NAME" not in run, (
                "gh release create must NOT use GITHUB_REF_NAME in release.yaml.\n"
                "The workflow is dispatch-triggered; use needs.prepare-version.outputs.tag instead.\n"
                f"Step run:\n{run}"
            )
            return
    pytest.fail("No 'gh release create' step found in the release job")
