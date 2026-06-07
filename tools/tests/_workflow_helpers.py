"""
Shared helpers for workflow YAML validation tests (B0, B2, and future workflow tests).

Factored out to avoid copy-paste between test_workflows_b0.py and test_workflows_b2.py;
both modules import from here.  This is a private module (leading underscore) — it is not
a test file and pytest will not collect it directly.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Repository root resolution
# ---------------------------------------------------------------------------

def repo_root() -> Path:
    """
    Resolve the git repository root in a way that works both on the host and
    inside the bind-mounted container (where __file__ may resolve to /w/tools/…).
    Falls back to two parents above tools/tests/ if git is unavailable.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent,
    )
    if result.returncode == 0:
        return Path(result.stdout.strip())
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------

def load_workflow(path: Path) -> dict:
    """Load a GitHub Actions workflow YAML file, returning the parsed dict."""
    with path.open() as fh:
        return yaml.safe_load(fh)


def get_triggers(workflow_yaml: dict) -> dict:
    """
    Extract the 'on:' triggers block from a parsed workflow dict.

    PyYAML (YAML 1.1) parses the bare word 'on' as Python True.  GitHub Actions
    workflows use 'on:' as the trigger key; the actual YAML is valid, but the
    Python key is True (bool), not 'on' (str).  Check both so the helper is
    future-safe if a caller uses YAML 1.2-compliant loading.
    """
    return workflow_yaml.get(True, workflow_yaml.get("on", {})) or {}


def all_steps(workflow_yaml: dict) -> list[dict]:
    """Return the flat list of all steps across all jobs in a workflow."""
    steps: list[dict] = []
    for job in workflow_yaml.get("jobs", {}).values():
        steps.extend(job.get("steps", []))
    return steps


# ---------------------------------------------------------------------------
# SHA-pin assertion
# ---------------------------------------------------------------------------

SHA40_RE = re.compile(r"^[a-f0-9]{40}$")


def assert_uses_sha_pinned(step: dict) -> None:
    """
    Assert that a step's `uses:` reference is pinned to a 40-hex-char commit SHA.
    SHA pins prevent tag-mutation supply-chain attacks; tag pins are mutable.
    """
    uses = step.get("uses", "")
    if not uses:
        return
    assert "@" in uses, f"uses: {uses!r} has no @ref"
    ref = uses.split("@", 1)[1]
    assert SHA40_RE.match(ref), (
        f"uses: {uses!r} is not SHA-pinned "
        f"(expected 40-char hex commit hash, got {ref!r}).\n"
        "SHA pins prevent tag-mutation supply-chain attacks."
    )


# ---------------------------------------------------------------------------
# Additional helpers for consolidated workflow tests
# ---------------------------------------------------------------------------


def all_workflows() -> list[Path]:
    """Return all .github/workflows/*.yml files in the repository."""
    root = repo_root()
    return sorted((root / ".github" / "workflows").glob("*.yml"))


def job_perms(workflow_yaml: dict, job_name: str) -> dict:
    """Return the permissions dict for a named job (empty dict if absent)."""
    jobs = workflow_yaml.get("jobs", {})
    job = jobs.get(job_name, {})
    return job.get("permissions", {})


def step_run_text(step: dict) -> str:
    """Return the concatenated run+with+env+uses text for a step (for needle searches)."""
    return (
        str(step.get("run", ""))
        + str(step.get("with", ""))
        + str(step.get("env", ""))
        + str(step.get("uses", ""))
    )


def steps_contain(steps: list[dict], *needles: str) -> bool:
    """Return True if any step's combined text contains ALL of the given needles."""
    for step in steps:
        combined = step_run_text(step)
        if all(n in combined for n in needles):
            return True
    return False


def find_step_by_uses_prefix(steps: list[dict], prefix: str) -> dict | None:
    """Return the first step whose uses: starts with the given prefix, or None."""
    for step in steps:
        if step.get("uses", "").startswith(prefix):
            return step
    return None


def step_index(steps: list[dict], *needles: str) -> int:
    """Return the index of the first step whose combined text contains ALL needles, or -1."""
    for i, step in enumerate(steps):
        if all(n in step_run_text(step) for n in needles):
            return i
    return -1


# ---------------------------------------------------------------------------
# Schema validation (offline, vendored)
# ---------------------------------------------------------------------------

def validate_workflow_schema(path: Path) -> None:
    """
    Run check-jsonschema against the vendored GitHub Actions JSON schema (offline).

    The GitHub Actions schema is bundled inside the check-jsonschema package —
    no network call is made at validation time.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "check_jsonschema",
            "--builtin-schema",
            "vendor.github-workflows",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"check-jsonschema schema validation failed for {path}:\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
