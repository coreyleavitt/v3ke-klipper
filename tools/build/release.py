"""Release packaging — pure core + thin I/O.

Public interface (pure — heavily unit-tested):
  ReleaseError
      Exception raised by resolve_version when no valid tag is available.

  resolve_version(repo_root, *, runner=subprocess_runner_text) -> str
      Runs `git -C <repo> describe --match 'v*' --abbrev=12`.
      Returns stripped output.  Raises ReleaseError on empty/non-v*/error —
      never returns "unknown".  The error message names the bootstrap step.

  submodule_provenance(repo_root) -> dict[str, dict]
      Returns {name: {"url": ..., "commit": ...}} for the three pinned submodules
      (klipper, katapult, mainsail-config).

  build_manifest(*, version, commit, source_date_epoch, toolchain, submodules,
                 artifacts, reproducible) -> dict
      Pure assembly of the manifest dict.  Timestamp is derived from
      source_date_epoch (not wall clock) for reproducibility.

  hash_artifact(path, arcname) -> dict
      {"name": basename, "path": arcname, "sha256": hexdigest, "size": bytes}

  release_members(repo_root, *, version) -> list[tuple[Path, str]]
      Pure plan: (source_path, archive_name) for every file in the release zip.

  release_zip_name(version) -> str
      "v3ke-<version>-linux-amd64.zip"

  validate_manifest(manifest, *, schema_path=<...>) -> None
      jsonschema.validate; let ValidationError propagate.

Thin I/O:
  write_release_zip(repo_root, out_dir, *, version, commit, source_date_epoch,
                    toolchain, reproducible) -> Path
      Hash real files, build+validate manifest, write deterministic zip.
      Returns the zip path.

Archive layout
──────────────
  firmware/katapult.bin
  firmware/klipper.bin
  host/c_helper.so
  host/klipper.elf
  host/klipper.dict
  v3ke
  INSTALL.md
  SOURCES.md
  manifest.json
  LICENSES/v3ke.LICENSE            (repo root LICENSE)
  LICENSES/klipper.LICENSE         (external/klipper/COPYING — GPL)
  LICENSES/katapult.LICENSE        (external/katapult/LICENSE)
  LICENSES/mainsail-config.LICENSE (external/mainsail-config/LICENSE)
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

__all__ = [
    "ReleaseError",
    "resolve_version",
    "submodule_provenance",
    "build_manifest",
    "hash_artifact",
    "release_members",
    "release_zip_name",
    "validate_manifest",
    "write_release_zip",
]

# ──────────────────────────────────────────────────────────────────────────────
# Schema path
# ──────────────────────────────────────────────────────────────────────────────

_SCHEMA_PATH = Path(__file__).resolve().parent / "manifest.schema.json"

# ──────────────────────────────────────────────────────────────────────────────
# Static release asset directory
# ──────────────────────────────────────────────────────────────────────────────

_RELEASE_ASSETS = Path(__file__).resolve().parent / "release_assets"


# ──────────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────────

class ReleaseError(RuntimeError):
    """Raised when the release pipeline cannot proceed."""


# ──────────────────────────────────────────────────────────────────────────────
# Default runner (text mode, for git describe)
# ──────────────────────────────────────────────────────────────────────────────

def _subprocess_runner_text(cmd: list[str]) -> str:
    """Default text-mode runner: run cmd, return stdout as str."""
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout


# ──────────────────────────────────────────────────────────────────────────────
# 1. resolve_version
# ──────────────────────────────────────────────────────────────────────────────

_BOOTSTRAP_HINT = (
    "No valid version tag found. "
    "Bootstrap with: git tag -a v0.1.0 -m 'v0.1.0' && git push origin v0.1.0"
)


def resolve_version(
    repo_root: Path,
    *,
    runner: Callable[[list[str]], str] = _subprocess_runner_text,
) -> str:
    """Return the version string from `git describe --match 'v*'`.

    Parameters
    ----------
    repo_root:
        Absolute path to the git repository root.
    runner:
        Callable that accepts a command list and returns stdout as str.
        Defaults to a real subprocess call.  Injectable for unit tests.

    Returns
    -------
    str
        The stripped `git describe` output, e.g. ``"v0.1.0"`` or
        ``"v0.1.0-3-gabcdef012345"``.

    Raises
    ------
    ReleaseError
        If git describe returns empty output, output that does not start with
        ``v``, or if the runner raises any exception.  The error message
        always names the one-time bootstrap step (``git tag -a v0.1.0 ...``).
    """
    cmd = ["git", "-C", str(repo_root), "describe", "--match", "v*", "--abbrev=12"]
    try:
        raw = runner(cmd)
    except Exception as exc:
        raise ReleaseError(
            f"{_BOOTSTRAP_HINT}\n(underlying error: {exc})"
        ) from exc

    stripped = raw.strip()
    if not stripped:
        raise ReleaseError(
            f"{_BOOTSTRAP_HINT}\n(git describe returned empty output)"
        )
    if not stripped.startswith("v"):
        raise ReleaseError(
            f"{_BOOTSTRAP_HINT}\n"
            f"(git describe output '{stripped}' does not match v* — "
            "no annotated tag with 'v' prefix found)"
        )
    return stripped


# ──────────────────────────────────────────────────────────────────────────────
# 2. submodule_provenance
# ──────────────────────────────────────────────────────────────────────────────

_SUBMODULE_NAMES = ["klipper", "katapult", "mainsail-config"]
_SUBMODULE_PATHS = {
    "klipper":        "external/klipper",
    "katapult":       "external/katapult",
    "mainsail-config": "external/mainsail-config",
}


def submodule_provenance(
    repo_root: Path,
    *,
    runner: Callable[[list[str]], str] = _subprocess_runner_text,
) -> dict[str, dict]:
    """Return {name: {"url": ..., "commit": ...}} for the three pinned submodules.

    Reads the real repo via git commands.  Deterministic from the pinned submodule
    state — does not require network access.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.
    runner:
        Callable that accepts a command list and returns stdout as a string.
        Defaults to ``_subprocess_runner_text`` (a real subprocess call).
        Injectable for unit tests — pass a fake runner to avoid real git calls.
        Matches the established concrete-default pattern used by ``resolve_version``.

    Returns
    -------
    dict
        Keys: ``"klipper"``, ``"katapult"``, ``"mainsail-config"``.
        Each value has ``"url"`` (from .gitmodules) and ``"commit"``
        (the pinned HEAD commit SHA for that submodule path).
    """
    repo_root = Path(repo_root)
    result: dict[str, dict] = {}

    for name in _SUBMODULE_NAMES:
        path = _SUBMODULE_PATHS[name]

        # URL from .gitmodules
        url = runner(
            ["git", "config", "--file", str(repo_root / ".gitmodules"),
             f"submodule.{path}.url"]
        ).strip()

        # Pinned commit: the SHA that HEAD records for this submodule path
        commit = runner(
            ["git", "-C", str(repo_root), "rev-parse", f"HEAD:{path}"]
        ).strip()

        result[name] = {"url": url, "commit": commit}

    return result


# ──────────────────────────────────────────────────────────────────────────────
# 3. build_manifest
# ──────────────────────────────────────────────────────────────────────────────

def build_manifest(
    *,
    version: str,
    commit: str,
    source_date_epoch: int,
    toolchain: dict,
    submodules: dict[str, dict],
    artifacts: list[dict],
    reproducible: bool,
) -> dict:
    """Pure assembly of the manifest dict.

    Parameters
    ----------
    version:
        Release version string (e.g. ``"v0.1.0"``).
    commit:
        Repository HEAD SHA.
    source_date_epoch:
        Unix timestamp for the build.  Used to derive the ISO-8601 timestamp;
        must NOT be replaced with wall-clock time so the manifest stays
        reproducible.
    toolchain:
        Dict with ``"mips"`` and ``"arm"`` keys, each mapping component names
        to version strings (from ``ct-build emit-versions``).
    submodules:
        ``{name: {"url": ..., "commit": ...}}`` from ``submodule_provenance``.
    artifacts:
        List of artifact dicts from ``hash_artifact``.
    reproducible:
        Whether this build has been verified reproducible.

    Returns
    -------
    dict
        The complete manifest ready for JSON serialisation.
    """
    timestamp = datetime.fromtimestamp(source_date_epoch, tz=timezone.utc).isoformat()

    return {
        "_type": "v3ke-build",
        "schema_version": "1",
        "build": {
            "id": version,
            "commit": commit,
            "timestamp": timestamp,
            "reproducible": reproducible,
            "toolchain": toolchain,
        },
        "sources": submodules,
        "artifacts": artifacts,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 4. hash_artifact
# ──────────────────────────────────────────────────────────────────────────────

def hash_artifact(path: Path, arcname: str) -> dict:
    """Compute SHA-256 and size for an artifact file.

    Parameters
    ----------
    path:
        Absolute path to the artifact on disk.
    arcname:
        The archive path (e.g. ``"firmware/katapult.bin"``).

    Returns
    -------
    dict
        ``{"name": basename, "path": arcname, "sha256": hexdigest, "size": int}``
    """
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    return {
        "name": path.name,
        "path": arcname,
        "sha256": sha,
        "size": len(data),
    }


# ──────────────────────────────────────────────────────────────────────────────
# 5. release_members
# ──────────────────────────────────────────────────────────────────────────────

def release_members(
    repo_root: Path,
    *,
    version: str,
) -> list[tuple[Path, str]]:
    """Return the ordered plan of (source_path, archive_name) for the release zip.

    All artifact source paths point to ``mcu-firmware/`` (the canonical captured
    locations) rather than ``external/klipper/out/``, which is wiped by the host
    ``make clean`` and must never be used at packaging time.

    manifest.json is NOT included here — it is appended directly by
    write_release_zip after it is computed, so it never needs a sentinel path.

    Archive layout:
      firmware/katapult.bin
      firmware/klipper.bin           ← mcu-firmware/klipper.bin
      host/c_helper.so
      host/klipper.elf               ← mcu-firmware/klipper_mcu.elf
      host/klipper.dict              ← mcu-firmware/klipper.dict
      v3ke
      INSTALL.md
      SOURCES.md
      LICENSES/v3ke.LICENSE
      LICENSES/klipper.LICENSE       (from external/klipper/COPYING)
      LICENSES/katapult.LICENSE
      LICENSES/mainsail-config.LICENSE
      manifest.json                  (appended by write_release_zip)

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.
    version:
        Release version (used only for future extension; currently unused).

    Returns
    -------
    list of (Path, str)
        Ordered (source_path, archive_name) pairs.  Does NOT include
        manifest.json — that is appended by write_release_zip.
    """
    r = Path(repo_root)

    members: list[tuple[Path, str]] = [
        # Firmware blobs
        # katapult.bin: lives in external/katapult/out/ (not wiped by host build)
        (r / "external" / "katapult" / "out" / "katapult.bin",        "firmware/katapult.bin"),
        # klipper.bin: canonical captured copy in mcu-firmware/ (out/ is wiped by host clean)
        (r / "mcu-firmware" / "klipper.bin",                          "firmware/klipper.bin"),
        # Host artifacts
        (r / "external" / "klipper" / "klippy" / "chelper" / "c_helper.so", "host/c_helper.so"),
        # klipper_mcu.elf: MIPS host-MCU ELF captured to mcu-firmware/ after host build
        (r / "mcu-firmware" / "klipper_mcu.elf",                      "host/klipper.elf"),
        # klipper.dict: captured to mcu-firmware/ before host clean wipes out/
        (r / "mcu-firmware" / "klipper.dict",                         "host/klipper.dict"),
        # v3ke CLI binary
        (r / "tools" / "v3ke" / "v3ke",                                "v3ke"),
        # Human-readable files (from bundled release_assets)
        (_RELEASE_ASSETS / "INSTALL.md",                               "INSTALL.md"),
        (_RELEASE_ASSETS / "SOURCES.md",                               "SOURCES.md"),
        # License files
        (r / "LICENSE",                                                "LICENSES/v3ke.LICENSE"),
        (r / "external" / "klipper" / "COPYING",                      "LICENSES/klipper.LICENSE"),
        (r / "external" / "katapult" / "LICENSE",                      "LICENSES/katapult.LICENSE"),
        (r / "external" / "mainsail-config" / "LICENSE",               "LICENSES/mainsail-config.LICENSE"),
    ]
    return members


# ──────────────────────────────────────────────────────────────────────────────
# 6. release_zip_name
# ──────────────────────────────────────────────────────────────────────────────

def release_zip_name(version: str) -> str:
    """Return the canonical zip filename for a release.

    Pattern: ``v3ke-<version>-linux-amd64.zip``
    """
    return f"v3ke-{version}-linux-amd64.zip"


# ──────────────────────────────────────────────────────────────────────────────
# 7. validate_manifest
# ──────────────────────────────────────────────────────────────────────────────

def validate_manifest(
    manifest: dict,
    *,
    schema_path: Path = _SCHEMA_PATH,
) -> None:
    """Validate *manifest* against the checked-in JSON Schema.

    Parameters
    ----------
    manifest:
        The manifest dict to validate.
    schema_path:
        Path to the JSON Schema file.  Defaults to
        ``tools/build/manifest.schema.json``.

    Raises
    ------
    jsonschema.ValidationError
        If the manifest does not conform to the schema.
    """
    import jsonschema  # available in the dev venv (jsonschema 4.26.0)

    schema = json.loads(schema_path.read_text())
    jsonschema.validate(manifest, schema)


# ──────────────────────────────────────────────────────────────────────────────
# Thin I/O: write_release_zip
# ──────────────────────────────────────────────────────────────────────────────

def write_release_zip(
    repo_root: Path,
    out_dir: Path,
    *,
    version: str,
    commit: str,
    source_date_epoch: int,
    toolchain: dict,
    reproducible: bool,
    _submodule_provenance: Optional[Callable] = None,
) -> Path:
    """Hash real artifacts, build+validate the manifest, write the release zip.

    This is the only I/O-performing function in the module.  All decisions are
    delegated to the pure helpers above.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.
    out_dir:
        Directory in which to write the zip.
    version:
        Release version string.
    commit:
        Repository HEAD SHA.
    source_date_epoch:
        Unix timestamp (int) used for deterministic timestamps inside the zip.
    toolchain:
        Toolchain version dict (``{"mips": {...}, "arm": {...}}``).
    reproducible:
        Whether this build has been verified reproducible.
    _submodule_provenance:
        Injectable override for ``submodule_provenance`` (unit tests pass a
        stub; omit in production).

    Returns
    -------
    Path
        Absolute path to the written zip file.
    """
    repo_root = Path(repo_root)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve submodule provenance (real git calls, or injected stub for tests)
    _prov_fn = _submodule_provenance or submodule_provenance
    submodules = _prov_fn(repo_root)

    # Get the member plan (manifest.json is NOT in the list — appended below)
    members = release_members(repo_root, version=version)

    # Read each artifact's bytes ONCE — the same bytes are used for both the
    # manifest sha256 and the zip payload.  Two reads would risk the manifest
    # attesting different bytes than what was packed (R2-L1).
    artifact_arcnames = {
        "firmware/katapult.bin", "firmware/klipper.bin",
        "host/c_helper.so", "host/klipper.elf", "host/klipper.dict",
    }
    member_bytes: list[tuple[str, bytes]] = []  # (arcname, raw_bytes) in members order
    artifact_dicts: list[dict] = []
    for src, arcname in members:
        data = src.read_bytes()
        member_bytes.append((arcname, data))
        if arcname in artifact_arcnames:
            sha = hashlib.sha256(data).hexdigest()
            artifact_dicts.append({
                "name": src.name,
                "path": arcname,
                "sha256": sha,
                "size": len(data),
            })

    # Build and validate the manifest
    manifest = build_manifest(
        version=version,
        commit=commit,
        source_date_epoch=source_date_epoch,
        toolchain=toolchain,
        submodules=submodules,
        artifacts=artifact_dicts,
        reproducible=reproducible,
    )
    validate_manifest(manifest)
    manifest_json = json.dumps(manifest, indent=2, sort_keys=True)

    # Build the final zip with deterministic member order + fixed mtime.
    # Write from the already-read bytes (no second read).
    zip_name = release_zip_name(version)
    zip_path = out_dir / zip_name

    # Fixed mtime tuple for reproducibility: (year, month, day, hour, min, sec)
    epoch_dt = datetime.fromtimestamp(source_date_epoch, tz=timezone.utc)
    fixed_mtime = (
        epoch_dt.year, epoch_dt.month, epoch_dt.day,
        epoch_dt.hour, epoch_dt.minute, epoch_dt.second,
    )

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, data in member_bytes:
            info = zipfile.ZipInfo(arcname, date_time=fixed_mtime)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
        # Append manifest.json directly — not via release_members() sentinel
        manifest_info = zipfile.ZipInfo("manifest.json", date_time=fixed_mtime)
        zf.writestr(manifest_info, manifest_json)

    return zip_path
