#!/usr/bin/env python3
"""Build orchestrator for the Ender 3 V3 KE toolchain + artifacts (host side; drives podman).
Cross-platform (Linux/CI, and Windows where podman runs). Hardware-side ops live in the v3ke CLI.

  build.py image                 build the toolchain image (toolchain baked in)  ~20-40 min cold
  build.py snapshot [backup|restore]
                                 copy the baked toolchain into a named volume (dev backup)
  build.py artifacts             build all device artifacts in the image + verify their ABI

Canonical sequence to go from zero to verified artifacts:
  1.  build.py image       — build (or rebuild) the toolchain container image
  2.  build.py artifacts   — run the Python build module inside the image; produces
                             katapult.bin, klipper.bin, c_helper.so, klipper_mcu.elf
                             and verifies MIPS ABI (fp_abi=6 / FP64) on the ELF outputs.

Levers: --image, --ctng-version (image build-arg), --xtools-vol.

Note: the 'all' alias (image + artifacts) has been removed because it would either
silently exclude 'release' or wrongly include a tag-requiring step.  Run the two
commands sequentially when you need a fresh image + artifacts build.
"""
import argparse, subprocess, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent          # tools/build.py -> repo root
TOOLCHAIN = REPO / "toolchain"
INSTALLED_GCC = "/opt/x-tools/mipsel-buildroot-linux-gnu/bin/mipsel-buildroot-linux-gnu-gcc"

# Pure helpers + updated cmd_image live in build_main so they are unit-testable
# without importing this CLI entry-point.
from build_main import cmd_image, write_image_digest  # noqa: E402


def run(cmd):
    print("+ " + " ".join(map(str, cmd)), flush=True)
    subprocess.run(cmd, check=True)


def image_exists(image, runtime="podman"):
    """Check whether *image* exists locally using the given container runtime."""
    if runtime == "podman":
        return subprocess.run(["podman", "image", "exists", image]).returncode == 0
    # docker: exit 0 if the image is present, non-zero otherwise.
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        capture_output=True,
    )
    return result.returncode == 0


def require_image(image, runtime="podman"):
    if not image_exists(image, runtime=runtime):
        sys.exit(f"image '{image}' not found — run: build.py image")



def cmd_snapshot(a):
    runtime = getattr(a, "runtime", "podman")
    require_image(a.image, runtime=runtime)
    if a.action == "backup":
        run([runtime, "run", "--rm", "-v", f"{a.xtools_vol}:/backup", a.image,
             "sh", "-c", "rm -rf /backup/* && cp -a /opt/x-tools/. /backup/"])
        print(f"toolchain backed up: image '{a.image}' -> volume '{a.xtools_vol}'")
    else:  # restore = sanity-check the volume holds a *working* toolchain (compiles + right loader),
           # not just that the gcc binary exists — a sysroot-less snapshot would pass `gcc --version`.
        run([runtime, "run", "--rm", "-v", f"{a.xtools_vol}:/opt/x-tools:ro", a.image,
             "ct-build", "--target", "mips", "verify"])


def cmd_artifacts(a):
    """Build all device artifacts in one container run via the Python build module.

    Invokes ``python -m build.orchestrate`` inside the v3ke-toolchain image with
    the repo mounted at /work.  The orchestrate module assembles and executes all
    11 build steps (6 ARM MCU + 1 capture + 4 host) in canonical order, performs
    per-step ABI verification on the ELF outputs (fp_abi=6 / FP64), and exits
    non-zero on any failure or violation.

    No bash scripts are invoked; the Python module is the single authority.

    --runtime selects the container runtime (default: podman for local dev;
    CI passes --runtime docker because GitHub-hosted runners ship Docker, not podman).
    """
    runtime = getattr(a, "runtime", "podman")
    require_image(a.image, runtime=runtime)
    # Mount the repo at /work; working dir is /work/tools so that python3 -m
    # build.orchestrate finds the tools/ package (build/, abi/) on sys.path.
    # The orchestrate module hardcodes /work as the repo_root mount point.
    mounts = ["-v", f"{REPO}:/work", "-w", "/work/tools"]
    if getattr(a, "xtools_vol", None):                  # iterate against a snapshot volume
        mounts += ["-v", f"{a.xtools_vol}:/opt/x-tools:ro"]
    # One container run: the Python orchestrate module handles build + ABI verify in a
    # single sequenced pass with per-step StepResult reporting.
    run([
        runtime, "run", "--rm",
        *mounts,
        a.image,
        "python3", "-m", "build.orchestrate",
    ])


def cmd_release(a):
    """Produce a versioned release zip + schema-validated manifest.json.

    If --toolchain-versions is omitted, runs `ct-build emit-versions` inside
    the container image to capture the pinned toolchain versions.
    """
    import json
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))

    from build.release import (
        resolve_version,
        submodule_provenance,
        write_release_bundles,
    )
    from build.arm_mcu import resolve_source_date_epoch

    runtime = getattr(a, "runtime", "podman")
    out_dir = Path(getattr(a, "out_dir", "dist")).resolve()

    # 1. Resolve version from git describe
    version = resolve_version(REPO)

    # 2. Resolve SOURCE_DATE_EPOCH from git HEAD
    epoch = resolve_source_date_epoch(REPO)

    # 3. Resolve HEAD commit SHA
    commit = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # 4. Toolchain versions — from file or by running ct-build in the image
    if getattr(a, "toolchain_versions", None):
        toolchain = json.loads(Path(a.toolchain_versions).read_text())
    else:
        require_image(a.image, runtime=runtime)
        proc = subprocess.run(
            [runtime, "run", "--rm",
             "-v", f"{REPO}:/work", "-w", "/work",
             a.image, "python3", "toolchain/ct_build.py", "emit-versions"],
            capture_output=True, text=True, check=True,
        )
        toolchain = json.loads(proc.stdout)

    reproducible = getattr(a, "reproducible", False)

    bundle_paths = write_release_bundles(
        repo_root=REPO,
        out_dir=out_dir,
        version=version,
        commit=commit,
        source_date_epoch=epoch,
        toolchain=toolchain,
        reproducible=reproducible,
    )
    for p in bundle_paths:
        print(f"release: {p}")


def main():
    p = argparse.ArgumentParser(prog="build.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--image", default="v3ke-toolchain")
    p.add_argument(
        "--runtime",
        default="podman",
        choices=["podman", "docker"],
        help="container runtime to use (default: podman; CI passes --runtime docker)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("image", help="build the toolchain image")
    pi.add_argument("--ctng-version", help="override crosstool-ng version (Containerfile build-arg)")
    pi.add_argument(
        "--push", action="store_true", default=False,
        help=(
            "after building, push the image to the registry, capture the manifest "
            "digest from the push output (podman: --digestfile; docker: stdout), "
            "and write it to toolchain/IMAGE_DIGEST (for digest-pinned CI pulls)"
        ),
    )

    ps = sub.add_parser("snapshot", help="copy baked toolchain <-> named volume")
    ps.add_argument("action", nargs="?", choices=["backup", "restore"], default="backup")
    ps.add_argument("--xtools-vol", default="v3ke-xtools")

    pa = sub.add_parser("artifacts", help="build device artifacts in the image + verify ABI")
    pa.add_argument("--xtools-vol", help="use a snapshot volume instead of the image's toolchain")

    pr = sub.add_parser("release", help="produce versioned release zip + manifest.json")
    pr.add_argument("--out-dir", default="dist", help="output directory for the zip (default: dist/)")
    pr.add_argument(
        "--reproducible", action="store_true", default=False,
        help="mark the manifest as reproducible (pass after repro verification in C3)",
    )
    pr.add_argument(
        "--no-reproducible", dest="reproducible", action="store_false",
        help="mark the manifest as not reproducible (default)",
    )
    pr.add_argument(
        "--toolchain-versions", default=None, metavar="PATH",
        help="path to JSON file from 'ct-build emit-versions'; "
             "if omitted, runs ct-build emit-versions inside --image",
    )

    a = p.parse_args()
    {
        "image": cmd_image,
        "snapshot": cmd_snapshot,
        "artifacts": cmd_artifacts,
        "release": cmd_release,
    }[a.cmd](a)


if __name__ == "__main__":
    main()
