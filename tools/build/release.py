"""Release packaging — pure core + thin I/O.

Public interface (pure — heavily unit-tested):
  ReleaseError
      Exception raised by release helpers when the pipeline cannot proceed.

  ReleasePlatform
      Frozen dataclass describing one release target OS.

  RELEASE_PLATFORMS
      Tuple of the two supported ReleasePlatform instances:
      ``linux-amd64`` (tar.xz) and ``windows-amd64`` (zip).

  resolve_version(repo_root) -> str
      Reads ``<repo_root>/VERSION``, strips whitespace, validates bare semver.
      Raises ReleaseError on missing file, empty content, or malformed content.
      The error message names the CI prepare-version job and the local manual
      workaround.

  submodule_provenance(repo_root) -> dict[str, dict]
      Returns {name: {"url": ..., "commit": ...}} for the three pinned submodules
      (klipper, katapult, mainsail-config).

  build_manifest(*, version, commit, source_date_epoch, toolchain, submodules,
                 artifacts, reproducible) -> dict
      Pure assembly of the manifest dict.  Timestamp is derived from
      source_date_epoch (not wall clock) for reproducibility.

  hash_artifact(path, arcname) -> dict
      {"name": basename, "path": arcname, "sha256": hexdigest, "size": bytes}

  release_members(repo_root, *, version, platform=<linux-amd64>) -> list[tuple[Path, str]]
      Pure plan: (source_path, archive_name) for every file in the bundle.
      The CLI member is chosen from platform.cli_source / cli_arcname.
      manifest.json is NOT included — appended by write_release_bundles.

  bundle_name(version, platform) -> str
      ``f"v3ke-{version}-{platform.name}.{platform.fmt}"``
      e.g. ``"v3ke-0.1.0-linux-amd64.tar.xz"`` / ``"v3ke-0.1.0-windows-amd64.zip"``

  release_zip_name(version) -> str
      Compatibility alias: ``bundle_name(version, linux_platform)``.
      Preserved so callers that used the old single-zip API continue to compile.

  validate_manifest(manifest, *, schema_path=<...>) -> None
      jsonschema.validate; let ValidationError propagate.

Thin I/O:
  write_release_bundles(repo_root, out_dir, *, version, commit, source_date_epoch,
                        toolchain, reproducible, platforms=RELEASE_PLATFORMS,
                        _submodule_provenance=None) -> list[Path]
      Build manifest once from the 5 device artifacts, then write one bundle per
      platform in its native format (tar.xz or zip).  Returns the list of written
      paths.  Raises ReleaseError if any platform's CLI source is absent.

  write_release_zip(repo_root, out_dir, *, version, commit, source_date_epoch,
                    toolchain, reproducible, _submodule_provenance=None) -> Path
      Compatibility alias: calls write_release_bundles for all platforms and
      returns the linux bundle path.  Preserved so existing tests continue to work.

Bundle layout (per platform — identical except for CLI name)
────────────────────────────────────────────────────────────
  firmware/katapult.bin
  firmware/klipper.bin
  host/c_helper.so
  host/klipper.elf
  host/klipper.dict
  v3ke              ← linux-amd64
  v3ke.exe          ← windows-amd64
  INSTALL.md
  SOURCES.md
  manifest.json
  LICENSES/v3ke.LICENSE            (repo root LICENSE)
  LICENSES/klipper.LICENSE         (external/klipper/COPYING — GPL)
  LICENSES/katapult.LICENSE        (external/katapult/LICENSE)
  LICENSES/mainsail-config.LICENSE (external/mainsail-config/LICENSE)
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import re
import subprocess
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

__all__ = [
    "ReleasePlatform",
    "RELEASE_PLATFORMS",
    "ReleaseError",
    "resolve_version",
    "submodule_provenance",
    "build_manifest",
    "hash_artifact",
    "release_members",
    "bundle_name",
    "release_zip_name",
    "validate_manifest",
    "write_release_bundles",
    "write_release_zip",
]

# ──────────────────────────────────────────────────────────────────────────────
# Platform descriptors
# ──────────────────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class ReleasePlatform:
    """Immutable descriptor for one release target OS.

    Attributes
    ----------
    name:
        Canonical platform slug used in bundle filenames, e.g. ``"linux-amd64"``.
    cli_source:
        Repo-root-relative path to the CLI binary for this platform,
        e.g. ``"tools/v3ke/v3ke"`` or ``"tools/v3ke/v3ke.exe"``.
    cli_arcname:
        Archive member name for the CLI binary, e.g. ``"v3ke"`` or ``"v3ke.exe"``.
    fmt:
        Archive format: ``"tar.xz"`` (Linux) or ``"zip"`` (Windows).
    """

    name: str
    cli_source: str
    cli_arcname: str
    fmt: str


RELEASE_PLATFORMS: tuple[ReleasePlatform, ...] = (
    ReleasePlatform(
        name="linux-amd64",
        cli_source="tools/v3ke/v3ke",
        cli_arcname="v3ke",
        fmt="tar.xz",
    ),
    ReleasePlatform(
        name="windows-amd64",
        cli_source="tools/v3ke/v3ke.exe",
        cli_arcname="v3ke.exe",
        fmt="zip",
    ),
)

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
# Subprocess runner (used by submodule_provenance and other git-calling helpers)
# ──────────────────────────────────────────────────────────────────────────────

def _subprocess_runner_text(cmd: list[str]) -> str:
    """Default text-mode runner: run cmd, return stdout as str."""
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout


# ──────────────────────────────────────────────────────────────────────────────
# 1. resolve_version
# ──────────────────────────────────────────────────────────────────────────────

# Bare semver: X.Y.Z or X.Y.Z-<prerelease> (no leading 'v').
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$")

_VERSION_FILE_HINT = (
    "The VERSION file is managed by the release workflow's prepare-version CI job. "
    "For a local build, set it manually: echo '0.1.0' > VERSION"
)


def resolve_version(repo_root: Path) -> str:
    """Return the bare semver string from ``<repo_root>/VERSION``.

    Parameters
    ----------
    repo_root:
        Absolute path to the git repository root.

    Returns
    -------
    str
        Stripped content of the VERSION file, e.g. ``"0.1.0"`` or
        ``"0.1.0-rc.1"``.  No ``v`` prefix — the file is bare semver.

    Raises
    ------
    ReleaseError
        If the VERSION file is missing, empty/whitespace-only, or does not
        match ``^\\d+\\.\\d+\\.\\d+(-[0-9A-Za-z.]+)?$``.  The error message
        explains the prepare-version CI job and the local workaround.
    """
    version_path = Path(repo_root) / "VERSION"

    if not version_path.exists():
        raise ReleaseError(
            f"VERSION file not found at {version_path}. "
            f"{_VERSION_FILE_HINT}"
        )

    raw = version_path.read_text(encoding="utf-8")
    stripped = raw.strip()

    if not stripped:
        raise ReleaseError(
            f"VERSION file at {version_path} is empty or whitespace-only. "
            f"{_VERSION_FILE_HINT}"
        )

    if not _VERSION_RE.match(stripped):
        raise ReleaseError(
            f"VERSION file at {version_path} contains malformed version {stripped!r}. "
            f"Expected bare semver matching '^\\d+\\.\\d+\\.\\d+(-[0-9A-Za-z.]+)?$' "
            f"(no 'v' prefix). "
            f"{_VERSION_FILE_HINT}"
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
    platform: Optional[ReleasePlatform] = None,
) -> list[tuple[Path, str]]:
    """Return the ordered plan of (source_path, archive_name) for a release bundle.

    All artifact source paths point to ``mcu-firmware/`` (the canonical captured
    locations) rather than ``external/klipper/out/``, which is wiped by the host
    ``make clean`` and must never be used at packaging time.

    manifest.json is NOT included here — it is appended directly by
    write_release_bundles after it is computed, so it never needs a sentinel path.

    Bundle layout (common across platforms):
      firmware/katapult.bin
      firmware/klipper.bin           ← mcu-firmware/klipper.bin
      host/c_helper.so
      host/klipper.elf               ← mcu-firmware/klipper_mcu.elf
      host/klipper.dict              ← mcu-firmware/klipper.dict
      <cli_arcname>                  ← determined by platform (v3ke or v3ke.exe)
      INSTALL.md
      SOURCES.md
      LICENSES/v3ke.LICENSE
      LICENSES/klipper.LICENSE       (from external/klipper/COPYING)
      LICENSES/katapult.LICENSE
      LICENSES/mainsail-config.LICENSE
      manifest.json                  (appended by write_release_bundles)

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.
    version:
        Release version (reserved for future extension; currently unused in body).
    platform:
        The :class:`ReleasePlatform` that determines the CLI source path and its
        archive name.  Defaults to the ``linux-amd64`` platform for backward
        compatibility with callers that pre-date the multi-platform API.

    Returns
    -------
    list of (Path, str)
        Ordered (source_path, archive_name) pairs.  Does NOT include
        manifest.json — that is appended by write_release_bundles.
    """
    if platform is None:
        platform = RELEASE_PLATFORMS[0]  # linux-amd64 default

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
        # Platform CLI binary — arcname differs between linux (v3ke) and windows (v3ke.exe)
        (r / platform.cli_source,                                      platform.cli_arcname),
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
# 6. bundle_name  (replaces the old release_zip_name)
# ──────────────────────────────────────────────────────────────────────────────

def bundle_name(version: str, platform: ReleasePlatform) -> str:
    """Return the canonical bundle filename for *version* on *platform*.

    Pattern: ``v3ke-<version>-<platform.name>.<platform.fmt>``

    Examples
    --------
    >>> bundle_name("0.1.0", linux_platform)
    "v3ke-0.1.0-linux-amd64.tar.xz"
    >>> bundle_name("0.1.0", windows_platform)
    "v3ke-0.1.0-windows-amd64.zip"
    """
    return f"v3ke-{version}-{platform.name}.{platform.fmt}"


def release_zip_name(version: str) -> str:
    """Backward-compatibility alias for :func:`bundle_name` with the linux platform.

    .. deprecated::
        Use ``bundle_name(version, platform)`` for new code.
        Retained so old callers that reference the single-zip name continue to
        compile without changes.
    """
    linux = RELEASE_PLATFORMS[0]  # linux-amd64
    return bundle_name(version, linux)


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
# Thin I/O: write_release_bundles
# ──────────────────────────────────────────────────────────────────────────────

#: The 5 device artifact archive paths hashed into the manifest.
#: Identical across all platforms — the manifest is OS-independent.
_DEVICE_ARCNAMES: frozenset[str] = frozenset({
    "firmware/katapult.bin",
    "firmware/klipper.bin",
    "host/c_helper.so",
    "host/klipper.elf",
    "host/klipper.dict",
})


def _write_zip_bundle(
    path: Path,
    member_bytes: list[tuple[str, bytes]],
    manifest_json: bytes,
    fixed_mtime: tuple[int, int, int, int, int, int],
) -> None:
    """Write a deterministic zip bundle at *path*."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, data in member_bytes:
            info = zipfile.ZipInfo(arcname, date_time=fixed_mtime)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
        manifest_info = zipfile.ZipInfo("manifest.json", date_time=fixed_mtime)
        zf.writestr(manifest_info, manifest_json)


def _write_tar_xz_bundle(
    path: Path,
    member_bytes: list[tuple[str, bytes]],
    manifest_json: bytes,
    source_date_epoch: int,
    cli_arcname: str,
) -> None:
    """Write a deterministic tar.xz bundle at *path*.

    Every TarInfo is built manually — never via ``gettarinfo`` — so that real
    filesystem metadata (mtime, uid, gid, mode) is never captured.  This is the
    key to byte-for-byte reproducibility regardless of the host environment.

    Member order mirrors the zip helper so both archives have an identical,
    predictable layout.
    """
    with tarfile.open(path, "w:xz", format=tarfile.GNU_FORMAT) as tf:
        for arcname, data in member_bytes:
            ti = tarfile.TarInfo(name=arcname)
            ti.size = len(data)
            ti.mtime = source_date_epoch
            ti.uid = 0
            ti.gid = 0
            ti.uname = ""
            ti.gname = ""
            # CLI binary gets executable bits; everything else is a regular file.
            ti.mode = 0o755 if arcname == cli_arcname else 0o644
            tf.addfile(ti, io.BytesIO(data))
        # Append manifest.json with the same fixed metadata
        mti = tarfile.TarInfo(name="manifest.json")
        mti.size = len(manifest_json)
        mti.mtime = source_date_epoch
        mti.uid = 0
        mti.gid = 0
        mti.uname = ""
        mti.gname = ""
        mti.mode = 0o644
        tf.addfile(mti, io.BytesIO(manifest_json))


def write_release_bundles(
    repo_root: Path,
    out_dir: Path,
    *,
    version: str,
    commit: str,
    source_date_epoch: int,
    toolchain: dict,
    reproducible: bool,
    platforms: Sequence[ReleasePlatform] = RELEASE_PLATFORMS,
    _submodule_provenance: Optional[Callable] = None,
) -> list[Path]:
    """Build and write one self-contained bundle per platform.

    The manifest is computed ONCE from the 5 device artifacts (which are
    identical across all platforms) and embedded byte-for-byte into every
    bundle and into the standalone ``out_dir/manifest.json``.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.
    out_dir:
        Directory in which to write bundles and the standalone manifest.json.
    version:
        Release version string (bare semver, no 'v' prefix).
    commit:
        Repository HEAD SHA.
    source_date_epoch:
        Unix timestamp (int) used for deterministic timestamps in archives.
    toolchain:
        Toolchain version dict (``{"mips": {...}, "arm": {...}}``).
    reproducible:
        Whether this build has been verified reproducible.
    platforms:
        Sequence of :class:`ReleasePlatform` instances to package.
        Defaults to :data:`RELEASE_PLATFORMS` (linux + windows).
    _submodule_provenance:
        Injectable override for :func:`submodule_provenance` (unit tests pass a
        stub; omit in production to use real git calls).

    Returns
    -------
    list[Path]
        Absolute paths to the written bundle files, one per platform, in the
        same order as *platforms*.

    Raises
    ------
    ReleaseError
        If any platform's CLI source file is missing from the repo tree.
        The error message names both the missing file and the platform.
    """
    repo_root = Path(repo_root)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Validate CLI sources up-front (fail fast, informative message) ──────
    for plat in platforms:
        cli_path = repo_root / plat.cli_source
        if not cli_path.exists():
            raise ReleaseError(
                f"CLI binary for platform {plat.name!r} not found: {cli_path} "
                f"(expected at {plat.cli_source!r} relative to repo root). "
                f"Build the Nim CLI before packaging."
            )

    # ── 2. Resolve submodule provenance ─────────────────────────────────────
    _prov_fn = _submodule_provenance or submodule_provenance
    submodules = _prov_fn(repo_root)

    # ── 3. Read device-artifact bytes once via the linux plan ────────────────
    # The device artifacts are platform-independent.  We read them once using
    # any platform's member plan (linux chosen by convention); the CLI entry
    # is excluded from hashing via _DEVICE_ARCNAMES.
    linux_platform = RELEASE_PLATFORMS[0]  # linux-amd64
    linux_members = release_members(repo_root, version=version, platform=linux_platform)

    # (arcname -> (src_path, bytes)) for all members in the linux plan.
    # This preserves the canonical order for zip-format bundles.
    linux_member_bytes: list[tuple[str, bytes]] = []
    artifact_dicts: list[dict] = []

    for src, arcname in linux_members:
        data = src.read_bytes()
        linux_member_bytes.append((arcname, data))
        if arcname in _DEVICE_ARCNAMES:
            sha = hashlib.sha256(data).hexdigest()
            artifact_dicts.append({
                "name": src.name,
                "path": arcname,
                "sha256": sha,
                "size": len(data),
            })

    # ── 4. Build and validate the shared manifest ────────────────────────────
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
    manifest_json_str = json.dumps(manifest, indent=2, sort_keys=True)
    manifest_json: bytes = manifest_json_str.encode("utf-8")

    # ── 5. Write the standalone manifest.json ────────────────────────────────
    (out_dir / "manifest.json").write_bytes(manifest_json)

    # ── 6. Deterministic mtime tuple (for zip format) ────────────────────────
    epoch_dt = datetime.fromtimestamp(source_date_epoch, tz=timezone.utc)
    fixed_mtime = (
        epoch_dt.year, epoch_dt.month, epoch_dt.day,
        epoch_dt.hour, epoch_dt.minute, epoch_dt.second,
    )

    # ── 7. Write one bundle per platform ─────────────────────────────────────
    written: list[Path] = []

    # Build an index of linux bytes by arcname for O(1) lookup when assembling
    # per-platform member lists (we replace the CLI entry).
    linux_bytes_by_arcname: dict[str, bytes] = dict(linux_member_bytes)

    for plat in platforms:
        # Assemble this platform's member list: same as linux except CLI entry.
        plat_members = release_members(repo_root, version=version, platform=plat)
        plat_member_bytes: list[tuple[str, bytes]] = []
        for src, arcname in plat_members:
            if arcname == plat.cli_arcname:
                # Read CLI bytes directly (may differ from linux CLI bytes)
                data = src.read_bytes()
            else:
                # Reuse already-read device/doc/license bytes — no second read
                data = linux_bytes_by_arcname[arcname]
            plat_member_bytes.append((arcname, data))

        name = bundle_name(version, plat)
        out_path = out_dir / name

        if plat.fmt == "zip":
            _write_zip_bundle(out_path, plat_member_bytes, manifest_json, fixed_mtime)
        elif plat.fmt == "tar.xz":
            _write_tar_xz_bundle(
                out_path, plat_member_bytes, manifest_json,
                source_date_epoch, plat.cli_arcname,
            )
        else:
            raise ReleaseError(
                f"Unsupported bundle format {plat.fmt!r} for platform {plat.name!r}."
            )

        written.append(out_path)

    return written


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
    """Backward-compatibility shim: write all platform bundles and return the linux path.

    .. deprecated::
        New code should call :func:`write_release_bundles` directly.
        This shim is retained so existing tests and callers that expected a single
        returned :class:`~pathlib.Path` continue to work without modification.

    Returns the path to the ``linux-amd64.tar.xz`` bundle.
    """
    paths = write_release_bundles(
        repo_root,
        out_dir,
        version=version,
        commit=commit,
        source_date_epoch=source_date_epoch,
        toolchain=toolchain,
        reproducible=reproducible,
        _submodule_provenance=_submodule_provenance,
    )
    # Return the linux bundle path (first in RELEASE_PLATFORMS order)
    return next(p for p in paths if "linux-amd64" in p.name)
