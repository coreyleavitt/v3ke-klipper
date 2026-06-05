#!/usr/bin/env python3
"""Build orchestrator for the Ender 3 V3 KE toolchain + artifacts (host side; drives podman).
Cross-platform (Linux/CI, and Windows where podman runs). Hardware-side ops live in the v3ke CLI.

  build.py image                 build the toolchain image (toolchain baked in)  ~20-40 min cold
  build.py snapshot [backup|restore]
                                 copy the baked toolchain into a named volume (dev backup)
  build.py artifacts             build all device artifacts in the image + verify their ABI
  build.py all                   image + artifacts

Levers: --image, --ctng-version (image build-arg), --xtools-vol.
"""
import argparse, subprocess, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent          # tools/build.py -> repo root
TOOLCHAIN = REPO / "toolchain"
INSTALLED_GCC = "/opt/x-tools/mipsel-buildroot-linux-gnu/bin/mipsel-buildroot-linux-gnu-gcc"


def run(cmd):
    print("+ " + " ".join(map(str, cmd)), flush=True)
    subprocess.run(cmd, check=True)


def image_exists(image):
    return subprocess.run(["podman", "image", "exists", image]).returncode == 0


def require_image(image):
    if not image_exists(image):
        sys.exit(f"image '{image}' not found — run: build.py image")


def cmd_image(a):
    cmd = ["podman", "build", "-t", a.image, "-f", str(TOOLCHAIN / "Containerfile")]
    if getattr(a, "ctng_version", None):
        cmd += ["--build-arg", f"CTNG_VERSION={a.ctng_version}"]
    cmd.append(str(TOOLCHAIN))
    run(cmd)


def cmd_snapshot(a):
    require_image(a.image)
    if a.action == "backup":
        run(["podman", "run", "--rm", "-v", f"{a.xtools_vol}:/backup", a.image,
             "sh", "-c", "rm -rf /backup/* && cp -a /opt/x-tools/. /backup/"])
        print(f"toolchain backed up: image '{a.image}' -> volume '{a.xtools_vol}'")
    else:  # restore = sanity-check the volume holds a working toolchain
        run(["podman", "run", "--rm", "-v", f"{a.xtools_vol}:/opt/x-tools:ro", a.image,
             INSTALLED_GCC, "--version"])


def cmd_artifacts(a):
    require_image(a.image)
    mounts = ["-v", f"{REPO}:/work", "-w", "/work"]
    if getattr(a, "xtools_vol", None):                  # iterate against a snapshot volume
        mounts += ["-v", f"{a.xtools_vol}:/opt/x-tools:ro"]
    run(["podman", "run", "--rm", *mounts, a.image,
         "bash", "-c", "./build-bootloader-mcu-and-host-firmware.sh && ./verify-artifacts.sh"])


def main():
    p = argparse.ArgumentParser(prog="build.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--image", default="v3ke-toolchain")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("image", help="build the toolchain image")
    pi.add_argument("--ctng-version", help="override crosstool-ng version (Containerfile build-arg)")

    ps = sub.add_parser("snapshot", help="copy baked toolchain <-> named volume")
    ps.add_argument("action", nargs="?", choices=["backup", "restore"], default="backup")
    ps.add_argument("--xtools-vol", default="v3ke-xtools")

    pa = sub.add_parser("artifacts", help="build device artifacts in the image + verify ABI")
    pa.add_argument("--xtools-vol", help="use a snapshot volume instead of the image's toolchain")

    px = sub.add_parser("all", help="image + artifacts")
    px.add_argument("--ctng-version")
    px.add_argument("--xtools-vol")

    a = p.parse_args()
    {"image": cmd_image, "snapshot": cmd_snapshot, "artifacts": cmd_artifacts,
     "all": lambda a: (cmd_image(a), cmd_artifacts(a))}[a.cmd](a)


if __name__ == "__main__":
    main()
