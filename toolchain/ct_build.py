#!/usr/bin/env python3
"""Configure + build the Ender 3 V3 KE cross toolchain. Runs INSIDE the builder image
(invoked by toolchain/Containerfile); ported from ct-build.sh.

Subcommands group the stages so the slow build is separable from the fast config:
    ct-build configure   seed sample + splice fragment + olddefconfig + assert  (no compile)
    ct-build build        ct-ng build  (dumps build.log on failure)
    ct-build verify       prove the toolchain DEFAULTS to the device loader
    ct-build all          configure + build + verify   (what the Containerfile runs)

Versions/paths are levers, not source edits — see EXPECT below and the --flags.
"""
import argparse, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

# Lines stripped from the seeded sample before splicing our fragment in, so our selections win
# cleanly under olddefconfig (mirrors the sed in the old ct-build.sh).
STRIP_RE = re.compile(
    r"^(# )?(CT_EXPERIMENTAL|CT_OBSOLETE|CT_ARCH_FLOAT|CT_ARCH_ARCH=|CT_ARCH_mips_o32|"
    r"CT_ARCH_mips_n32|CT_ARCH_mips_n64|CT_GLIBC_V_|CT_GLIBC_KERNEL_VERSION_|CT_GLIBC_MIN_KERNEL|"
    r"CT_GCC_V_|CT_BINUTILS_V_|CT_LINUX_V_|CT_TARGET_VENDOR|CT_TARGET_CFLAGS|"
    r"CT_CC_GCC_CORE_EXTRA_CONFIG_ARRAY|CT_CC_GCC_EXTRA_CONFIG_ARRAY|CT_PREFIX_DIR|CT_DEBUG_|"
    r"CT_LOCAL_TARBALLS_DIR|CT_SAVE_TARBALLS)"
)

# The fragment's intent must take effect — catches wrong ct-ng symbol names silently defaulting.
# This is the contract for the device ABI; bump here when the target moves.
EXPECT = {
    "CT_GLIBC_VERSION":   "2.29",
    "CT_GCC_VERSION":     "8.5.0",
    "CT_BINUTILS_VERSION": "2.32",
    "CT_LINUX_VERSION":   "4.14.329",
    "CT_GLIBC_MIN_KERNEL": "4.4.0",
    "CT_ARCH_ARCH":       "mips32r2",
    "CT_TARGET_CFLAGS":   "-mnan=2008 -mfp64",
    "CT_ARCH_FLOAT":      "hard",
}


def run(cmd, *, cwd=None, env=None):
    print("+ " + " ".join(map(str, cmd)), flush=True)
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def host_env(args):
    """gcc-13 host compiler (Tumbleweed's default gcc can't build 2019-era sources) + ccache,
    wired via PATH so ct-ng's bare `gcc`/`g++` resolve to it and route through ccache."""
    cc = shutil.which(args.host_cc)
    cxx = shutil.which(args.host_cc.replace("gcc", "g++"))
    if not cc or not cxx:
        sys.exit(f"host compiler {args.host_cc} / g++ not found")
    bind = Path(args.build_dir).parent / "hostcc"
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
    subprocess.run(["ccache", "-M", args.ccache_size], env=env, check=False)
    return env


def configure(args):
    bd = Path(args.build_dir)
    bd.mkdir(parents=True, exist_ok=True)
    cfg = bd / ".config"
    cfg.unlink(missing_ok=True)
    run(["ct-ng", args.sample], cwd=bd)
    kept = [ln for ln in cfg.read_text().splitlines(keepends=True) if not STRIP_RE.match(ln)]
    cfg.write_text("".join(kept) + Path(args.fragment).read_text())
    run(["ct-ng", "olddefconfig"], cwd=bd)
    text = cfg.read_text()
    missing = [f'{k}="{v}"' for k, v in EXPECT.items() if f'{k}="{v}"' not in text]
    if missing:
        sys.exit("CONFIG ASSERT FAILED:\n  " + "\n  ".join(missing))
    print("config OK: " + ", ".join(f"{k}={v}" for k, v in EXPECT.items()))


def build(args):
    bd = Path(args.build_dir)
    try:
        run(["ct-ng", "build"], cwd=bd, env=host_env(args))
    except subprocess.CalledProcessError:
        log = bd / "build.log"
        if log.exists():
            print("=== ct-ng build FAILED — tail of build.log ===")
            print("\n".join(log.read_text().splitlines()[-120:]))
        raise


def verify(args):
    """Prove the toolchain DEFAULTS to the device ABI: flag-less compile must emit the device
    loader (ld-linux-mipsn8.so.1 => nan2008+fp64). Catches a regressed compiler default."""
    with tempfile.TemporaryDirectory() as d:
        src, out = Path(d) / "t.c", Path(d) / "t"
        src.write_text("int main(void){return 0;}\n")
        run([args.gcc, str(src), "-o", str(out)])
        rl = subprocess.run(["readelf", "-l", str(out)], text=True, capture_output=True).stdout
    if args.device_loader not in rl:
        interp = next((l.strip() for l in rl.splitlines() if "interpreter" in l), "?")
        sys.exit(f"LOADER MISMATCH (default ABI is not nan2008/fp64): {interp}")
    print(f"toolchain OK -> default loader {args.device_loader}")


def main():
    cfg_args = argparse.ArgumentParser(add_help=False)
    cfg_args.add_argument("--build-dir", default="/home/ct/build")
    cfg_args.add_argument("--fragment", default="/opt/ctng-cfg/crosstool-ng.fragment")
    cfg_args.add_argument("--sample", default="mipsel-unknown-linux-gnu")

    build_args = argparse.ArgumentParser(add_help=False)
    build_args.add_argument("--host-cc", default="gcc-13")
    build_args.add_argument("--ccache-dir", default="/home/ct/.ccache")
    build_args.add_argument("--ccache-size", default="5G")

    verify_args = argparse.ArgumentParser(add_help=False)
    verify_args.add_argument(
        "--gcc",
        default="/opt/x-tools/mipsel-buildroot-linux-gnu/bin/mipsel-buildroot-linux-gnu-gcc")
    verify_args.add_argument("--device-loader", default="ld-linux-mipsn8.so.1")

    p = argparse.ArgumentParser(
        prog="ct-build", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("configure", parents=[cfg_args], help="seed + splice fragment + assert")
    sub.add_parser("build", parents=[cfg_args, build_args], help="ct-ng build")
    sub.add_parser("verify", parents=[verify_args], help="prove default ABI matches device")
    sub.add_parser("all", parents=[cfg_args, build_args, verify_args],
                   help="configure + build + verify")

    args = p.parse_args()
    if args.cmd in ("configure", "all"):
        configure(args)
    if args.cmd in ("build", "all"):
        build(args)
    if args.cmd in ("verify", "all"):
        verify(args)


if __name__ == "__main__":
    main()
