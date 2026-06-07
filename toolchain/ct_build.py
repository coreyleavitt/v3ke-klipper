#!/usr/bin/env python3
"""Configure + build a cross toolchain inside the builder image (invoked by toolchain/Containerfile).

Two targets:
  --target mips   the MIPS host toolchain (glibc 2.29, device ABI: nan2008/fp64/o32) — DEFAULT
  --target arm    the arm-none-eabi bare-metal toolchain (newlib) for the motion-MCU firmware

Subcommands separate the slow build from the fast config:
  ct-build [--target T] configure   seed sample + splice fragment + olddefconfig + assert
  ct-build [--target T] build        ct-ng build  (dumps build.log on failure)
  ct-build [--target T] verify       prove the toolchain came out right
  ct-build [--target T] all          configure + build + verify  (the Containerfile runs both targets)
"""
import argparse, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

# Lines stripped from the seeded sample before splicing our fragment in (so our selections win).
MIPS_STRIP = re.compile(
    r"^(# )?(CT_EXPERIMENTAL|CT_OBSOLETE|CT_ARCH_FLOAT|CT_ARCH_ARCH=|CT_ARCH_mips_o32|"
    r"CT_ARCH_mips_n32|CT_ARCH_mips_n64|CT_GLIBC_V_|CT_GLIBC_KERNEL_VERSION_|CT_GLIBC_MIN_KERNEL|"
    r"CT_GCC_V_|CT_BINUTILS_V_|CT_LINUX_V_|CT_TARGET_VENDOR|CT_TARGET_CFLAGS|"
    r"CT_CC_GCC_CORE_EXTRA_CONFIG_ARRAY|CT_CC_GCC_EXTRA_CONFIG_ARRAY|CT_PREFIX_DIR|CT_DEBUG_|"
    r"CT_LOCAL_TARBALLS_DIR|CT_SAVE_TARBALLS)")
ARM_STRIP = re.compile(
    r"^(# )?(CT_GCC_V_|CT_CC_GCC_MULTILIB_LIST|CT_PREFIX_DIR|CT_DEBUG_|CT_LOCAL_TARBALLS_DIR|"
    r"CT_SAVE_TARBALLS)")

# Per-target spec. `expect` is the resolved-config contract (catches wrong ct-ng symbol names that
# silently default). `verify`: loader = default-flags compile must request the device loader;
# armobj = compile a Cortex-M3 object and confirm it's ARM.
TARGETS = {
    "mips": dict(
        sample="mipsel-unknown-linux-gnu",
        fragment="/opt/ctng-cfg/crosstool-ng.fragment",
        strip=MIPS_STRIP,
        expect={
            "CT_GLIBC_VERSION": "2.29", "CT_GCC_VERSION": "8.5.0", "CT_BINUTILS_VERSION": "2.32",
            "CT_LINUX_VERSION": "4.14.329", "CT_GLIBC_MIN_KERNEL": "4.4.0",
            "CT_ARCH_ARCH": "mips32r2", "CT_TARGET_CFLAGS": "-mnan=2008 -mfp64",
            "CT_ARCH_FLOAT": "hard",
        },
        gcc="/opt/x-tools/mipsel-buildroot-linux-gnu/bin/mipsel-buildroot-linux-gnu-gcc",
        verify="loader",
    ),
    "arm": dict(
        sample="arm-none-eabi",
        fragment="/opt/ctng-cfg/crosstool-ng-arm.fragment",
        strip=ARM_STRIP,
        expect={"CT_GCC_VERSION": "14.3.0", "CT_CC_GCC_MULTILIB_LIST": "rmprofile"},
        gcc="/opt/x-tools/arm-none-eabi/bin/arm-none-eabi-gcc",
        verify="armobj",
        post="nanomerge",
    ),
}
DEVICE_LOADER = "ld-linux-mipsn8.so.1"


def run(cmd, *, cwd=None, env=None):
    print("+ " + " ".join(map(str, cmd)), flush=True)
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def build_dir(args):
    return Path(args.build_dir or f"/home/ct/build-{args.target}")


def host_env(args):
    """gcc-13 host compiler (Tumbleweed's default is too new for 2019-era sources) + ccache,
    wired via PATH so ct-ng's bare `gcc`/`g++` resolve to it and route through ccache."""
    cc = shutil.which(args.host_cc)
    cxx = shutil.which(args.host_cc.replace("gcc", "g++"))
    if not cc or not cxx:
        sys.exit(f"host compiler {args.host_cc} / g++ not found")
    bind = Path("/home/ct/hostcc")
    bind.mkdir(parents=True, exist_ok=True)
    for name, target in (("gcc", cc), ("cc", cc), ("g++", cxx), ("c++", cxx)):
        link = bind / name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)
    env = dict(os.environ)
    env["CCACHE_DIR"] = args.ccache_dir
    env["PATH"] = f"/usr/lib64/ccache:{bind}:{env['PATH']}"
    Path(args.ccache_dir).mkdir(parents=True, exist_ok=True)
    if subprocess.run(["ccache", "-M", args.ccache_size], env=env).returncode != 0:
        print(f"[{args.target}] warning: `ccache -M {args.ccache_size}` failed; building without a resized cache")
    return env


def configure(args, spec):
    bd = build_dir(args)
    bd.mkdir(parents=True, exist_ok=True)
    cfg = bd / ".config"
    cfg.unlink(missing_ok=True)
    run(["ct-ng", args.sample or spec["sample"]], cwd=bd)
    kept = [ln for ln in cfg.read_text().splitlines(keepends=True) if not spec["strip"].match(ln)]
    cfg.write_text("".join(kept) + Path(args.fragment or spec["fragment"]).read_text())
    run(["ct-ng", "olddefconfig"], cwd=bd)
    text = cfg.read_text()
    missing = [f'{k}="{v}"' for k, v in spec["expect"].items() if f'{k}="{v}"' not in text]
    if missing:
        sys.exit("CONFIG ASSERT FAILED:\n  " + "\n  ".join(missing))
    print(f"[{args.target}] config OK: " + ", ".join(f"{k}={v}" for k, v in spec["expect"].items()))


def nano_merge():
    """ARM newlib-nano: crosstool-ng installs the *_nano.a libs under a separate newlib-nano/
    prefix, but the firmware links `-lc_nano` expecting them in the main lib tree (the ARM-official
    layout). Copy them across, preserving the multilib structure."""
    nano = Path("/opt/x-tools/arm-none-eabi/newlib-nano/arm-none-eabi/lib")
    dest = Path("/opt/x-tools/arm-none-eabi/arm-none-eabi/lib")
    # crosstool-ng renders the install read-only; we own it, so re-enable owner write for the copy.
    subprocess.run(["chmod", "-R", "u+w", str(dest)], check=True)
    n = 0
    for f in nano.rglob("*_nano.a"):
        out = dest / f.relative_to(nano)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, out)
        n += 1
    subprocess.run(["chmod", "-R", "a-w", str(dest)], check=True)   # restore crosstool-ng's read-only
    print(f"[arm] merged {n} *_nano.a libs into the main lib tree (restored read-only)")


def build(args, spec):
    bd = build_dir(args)
    try:
        run(["ct-ng", "build"], cwd=bd, env=host_env(args))
    except subprocess.CalledProcessError:
        log = bd / "build.log"
        if log.exists():
            print("=== ct-ng build FAILED — tail of build.log ===")
            print("\n".join(log.read_text().splitlines()[-120:]))
        raise
    if spec.get("post") == "nanomerge":
        nano_merge()


def verify(args, spec):
    gcc = spec["gcc"]
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "t.c"
        if spec["verify"] == "loader":
            out = Path(d) / "t"
            src.write_text("int main(void){return 0;}\n")
            run([gcc, str(src), "-o", str(out)])        # NO explicit flags => tests the default ABI
            rl = subprocess.run(["readelf", "-l", str(out)], text=True, capture_output=True).stdout
            if DEVICE_LOADER not in rl:
                interp = next((l.strip() for l in rl.splitlines() if "interpreter" in l), "?")
                sys.exit(f"LOADER MISMATCH (default ABI is not nan2008/fp64): {interp}")
            print(f"[mips] toolchain OK -> default loader {DEVICE_LOADER}")
        elif spec["verify"] == "armobj":  # bare metal, no loader; confirm it builds a Cortex-M3 object
            obj = Path(d) / "t.o"
            src.write_text("int f(int x){return x*x;}\n")
            run([gcc, "-mcpu=cortex-m3", "-mthumb", "-Os", "-c", str(src), "-o", str(obj)])
            mach = subprocess.run(["readelf", "-h", str(obj)], text=True, capture_output=True).stdout
            if "ARM" not in mach:
                sys.exit("verify failed: object is not ARM")
            print("[arm] toolchain OK -> compiles Cortex-M3 Thumb objects")
        else:
            sys.exit(f"unknown verify mode: {spec['verify']!r}")


def emit_versions() -> dict:
    """Return a dict of pinned toolchain component versions for all targets.

    Reads directly from the TARGETS dict — no toolchain invocation required.
    Runs anywhere with python3 (stdlib-only), offline.

    Returns
    -------
    dict
        ``{"mips": {"glibc": "2.29", "gcc": "8.5.0", ...},
            "arm":  {"gcc": "14.3.0", ...}}``

    The mips entry includes: glibc, gcc, binutils, linux, glibc_min_kernel, arch, float.
    The arm entry includes: gcc.
    """
    mips = TARGETS["mips"]["expect"]
    arm  = TARGETS["arm"]["expect"]
    return {
        "mips": {
            "glibc":            mips["CT_GLIBC_VERSION"],
            "gcc":              mips["CT_GCC_VERSION"],
            "binutils":         mips["CT_BINUTILS_VERSION"],
            "linux":            mips["CT_LINUX_VERSION"],
            "glibc_min_kernel": mips["CT_GLIBC_MIN_KERNEL"],
            "arch":             mips["CT_ARCH_ARCH"],
            "float":            mips["CT_ARCH_FLOAT"],
        },
        "arm": {
            "gcc": arm["CT_GCC_VERSION"],
        },
    }


def main():
    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("--target", choices=TARGETS, default="mips")
    base.add_argument("--build-dir", default=None, help="default: /home/ct/build-<target>")
    base.add_argument("--fragment", default=None, help="override the target's fragment")
    base.add_argument("--sample", default=None, help="override the target's ct-ng sample")
    base.add_argument("--host-cc", default="gcc-13")
    base.add_argument("--ccache-dir", default="/home/ct/.ccache")
    base.add_argument("--ccache-size", default="5G")

    p = argparse.ArgumentParser(prog="ct-build", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("configure", "build", "verify", "all"):
        sub.add_parser(name, parents=[base])
    sub.add_parser("emit-versions", help="print pinned toolchain versions as JSON (no toolchain required)")

    args = p.parse_args()
    if args.cmd == "emit-versions":
        import json as _json
        print(_json.dumps(emit_versions(), indent=2))
        return
    spec = TARGETS[args.target]
    if args.cmd in ("configure", "all"):
        configure(args, spec)
    if args.cmd in ("build", "all"):
        build(args, spec)
    if args.cmd in ("verify", "all"):
        verify(args, spec)


if __name__ == "__main__":
    main()
