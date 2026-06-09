"""
Shared workflow invariants — parametrized over every .github/workflows/*.yaml file.

Each test function is written once and exercised against all workflow files
(ci.yaml, release.yaml).  Invariants tested here:
  1. Schema-valid (offline vendored check-jsonschema).
  2. Every uses: is SHA-pinned (supply-chain / tag-mutation defence).
  3. Top-level permissions is {} (deny-by-default).
  4. No job (and no top-level) grants write-all.

Per-workflow specifics live in test_workflow_ci.py and test_workflow_release.py.
DO NOT add per-workflow assertions here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _workflow_helpers import (
    all_steps,
    all_workflows,
    assert_uses_sha_pinned,
    get_triggers,
    load_workflow,
    validate_workflow_schema,
)

# ---------------------------------------------------------------------------
# Parametrize over every workflow file — one test ID per file per invariant.
# ---------------------------------------------------------------------------

_WORKFLOW_PATHS = all_workflows()
_WORKFLOW_IDS = [p.name for p in _WORKFLOW_PATHS]


@pytest.fixture(scope="module", params=_WORKFLOW_PATHS, ids=_WORKFLOW_IDS)
def wf_path(request) -> Path:
    return request.param


@pytest.fixture(scope="module")
def wf_yaml(wf_path: Path) -> dict:
    assert wf_path.exists(), f"Workflow file not found: {wf_path}"
    return load_workflow(wf_path)


# ---------------------------------------------------------------------------
# Invariant 1: schema validation (offline, vendored)
# ---------------------------------------------------------------------------


def test_schema_valid(wf_path: Path, wf_yaml: dict):
    """Every workflow must pass the vendored GitHub Actions JSON schema (offline)."""
    validate_workflow_schema(wf_path)


# ---------------------------------------------------------------------------
# Invariant 2: all uses: are SHA-pinned (40-hex commit hash)
# ---------------------------------------------------------------------------


def test_all_uses_sha_pinned(wf_path: Path, wf_yaml: dict):
    """
    Every uses: reference in every workflow must be pinned to a 40-char hex commit SHA.
    Tag pins are mutable — a supply-chain attacker can push a new commit to an existing
    tag and have it execute in CI.  SHA pins are immutable.
    """
    for step in all_steps(wf_yaml):
        uses = step.get("uses", "")
        if uses:
            assert_uses_sha_pinned(step)


# ---------------------------------------------------------------------------
# Invariant 3: top-level permissions is {} (deny-by-default)
# ---------------------------------------------------------------------------


def test_top_level_permissions_deny_by_default(wf_path: Path, wf_yaml: dict):
    """
    Top-level permissions must be {} so no implicit write grants bleed into jobs.
    Each job must elevate only the specific permissions it needs.
    """
    top_perms = wf_yaml.get("permissions")
    assert top_perms == {}, (
        f"{wf_path.name}: top-level permissions must be {{}} (deny-by-default); "
        f"got: {top_perms!r}"
    )


# ---------------------------------------------------------------------------
# Invariant 4: no write-all at top-level or any job
# ---------------------------------------------------------------------------


def test_no_write_all(wf_path: Path, wf_yaml: dict):
    """
    write-all grants every permission to a workflow/job — forbidden by least-privilege.
    This check covers both the top-level permissions key and every job's permissions key.
    """
    top_perms = wf_yaml.get("permissions")
    assert top_perms != "write-all", (
        f"{wf_path.name}: top-level permissions must not be write-all"
    )
    for job_name, job in wf_yaml.get("jobs", {}).items():
        job_perms = job.get("permissions")
        assert job_perms != "write-all", (
            f"{wf_path.name}: job '{job_name}' permissions must not be write-all"
        )
