#!/usr/bin/env bash
# Build the cross toolchain into /opt/x-tools (a named volume). Runs inside the builder image
# as user `ct`. Caches: downloads in /home/ct/src, compiles via ccache in /home/ct/.ccache
# (both named volumes). Validates config and the produced ABI before declaring success.
set -euxo pipefail

# Build with an era-appropriate HOST compiler. Tumbleweed's default gcc (15/16) is too new to
# compile the 2019-era binutils/gcc/glibc sources (gcc 14+ makes implicit-int a hard error and
# defaults to C23). gcc-13 still defaults to gnu17 and accepts them. Symlinks let crosstool-ng's
# bare `gcc`/`g++` resolve to 13; the ccache dir comes first so it still wraps them.
HOSTCC="$(command -v gcc-13 || true)"
HOSTCXX="$(command -v g++-13 || true)"
[ -x "$HOSTCC" ] && [ -x "$HOSTCXX" ] || { echo "gcc-13/g++-13 not found (need pkg gcc13 gcc13-c++)"; exit 2; }
mkdir -p /home/ct/hostcc
ln -sf "$HOSTCC" /home/ct/hostcc/gcc; ln -sf "$HOSTCC" /home/ct/hostcc/cc
ln -sf "$HOSTCXX" /home/ct/hostcc/g++; ln -sf "$HOSTCXX" /home/ct/hostcc/c++
export PATH="/usr/lib64/ccache:/home/ct/hostcc:${PATH}"
export CCACHE_DIR="${CCACHE_DIR:-/home/ct/.ccache}"
ccache -M "${CCACHE_MAXSIZE:-5G}" >/dev/null 2>&1 || true

mkdir -p /home/ct/src "$CCACHE_DIR" /home/ct/build
cd /home/ct/build
rm -f .config

# Seed the mipsel/glibc sample, splice in our ABI + version overrides, normalize.
ct-ng mipsel-unknown-linux-gnu
sed -i -E '/^(# )?(CT_EXPERIMENTAL|CT_OBSOLETE|CT_ARCH_FLOAT|CT_ARCH_ARCH=|CT_ARCH_mips_o32|CT_ARCH_mips_n32|CT_ARCH_mips_n64|CT_GLIBC_V_|CT_GLIBC_KERNEL_VERSION_|CT_GLIBC_MIN_KERNEL|CT_GCC_V_|CT_BINUTILS_V_|CT_LINUX_V_|CT_TARGET_VENDOR|CT_TARGET_CFLAGS|CT_CC_GCC_CORE_EXTRA_CONFIG_ARRAY|CT_CC_GCC_EXTRA_CONFIG_ARRAY|CT_PREFIX_DIR|CT_DEBUG_|CT_LOCAL_TARBALLS_DIR|CT_SAVE_TARBALLS)/d' .config
cat /opt/ctng-cfg/crosstool-ng.fragment >> .config
ct-ng olddefconfig

# Fail fast if any pin/ABI symbol didn't take.
for kv in \
    'CT_GLIBC_VERSION="2.29"' 'CT_GCC_VERSION="8.5.0"' 'CT_BINUTILS_VERSION="2.32"' \
    'CT_LINUX_VERSION="4.14.329"' 'CT_ARCH_ARCH="mips32r2"' \
    'CT_GLIBC_MIN_KERNEL="4.4.0"' \
    'CT_TARGET_CFLAGS="-mnan=2008 -mfp64"' 'CT_ARCH_FLOAT="hard"'; do
  grep -qF "$kv" .config || { echo "CONFIG ASSERT FAILED: $kv"; exit 1; }
done

# On failure, surface the real compile error (build.log is otherwise lost with the container).
if ! ct-ng build; then
  echo "=================== ct-ng build FAILED — tail of build.log ==================="
  tail -n 120 /home/ct/build/build.log 2>/dev/null \
    || find /home/ct/build -maxdepth 2 -name build.log -exec tail -n 120 {} +
  exit 1
fi

# Prove the toolchain DEFAULTS to the device ABI: compile with NO explicit flags and confirm
# it emits the device's loader (ld-linux-mipsn8.so.1 => nan2008+fp64). Compiling flag-less also
# exercises the gnu/stubs header that broke klipper's host-MCU build.
PFX=/opt/x-tools/mipsel-buildroot-linux-gnu/bin/mipsel-buildroot-linux-gnu-
echo 'int main(void){return 0;}' > /tmp/t.c
"${PFX}gcc" /tmp/t.c -o /tmp/t
readelf -l /tmp/t | grep -q 'ld-linux-mipsn8.so.1' \
  || { echo "LOADER MISMATCH (default ABI is not nan2008/fp64):"; readelf -l /tmp/t | grep -i interpreter; exit 1; }
rm -f /tmp/t /tmp/t.c
echo "TOOLCHAIN OK -> /opt/x-tools/mipsel-buildroot-linux-gnu"
