#!/bin/bash

set -euo pipefail

cd "$(dirname "$0")"

# Repo root is two levels up from klipper/c_helper. The crosstool-ng install is mounted/baked at
# CROSS_TOOLCHAIN by the container; tools/build.py sets it. Require it — there is no in-repo
# fallback toolchain (the old Bootlin tarball is gone), so don't pretend one exists.
REPO_ROOT="$(cd ../.. && pwd)"
export TOOLCHAIN="${CROSS_TOOLCHAIN:?CROSS_TOOLCHAIN not set — build via tools/build.py artifacts (it runs this in the container)}"
export SYSROOT=${TOOLCHAIN}/mipsel-buildroot-linux-gnu/sysroot
export PATH="$TOOLCHAIN/bin:$PATH"

CC="${TOOLCHAIN}/bin/mipsel-buildroot-linux-gnu-gcc"
if [ ! -x "$CC" ]; then
  echo "MIPS gcc missing at ${CC}. Build via tools/build.py image && tools/build.py artifacts." >&2
  exit 2
fi
echo "Using CC: $CC"
echo ""

CHELPER_DIR="${REPO_ROOT}/external/klipper/klippy/chelper"

echo -n "Building c_helper from klipper commit: "
git -C "$CHELPER_DIR" rev-parse --short HEAD 2>/dev/null || echo "(git unavailable)"

if [ -f "${CHELPER_DIR}/c_helper.so" ]; then
  echo -n "Remove old file: "
  rm -v "${CHELPER_DIR}/c_helper.so"
fi
echo ""

# Read the source list from klipper's chelper/__init__.py (SOURCE_FILES) at build time so it can't
# silently drift from the pinned submodule when klipper is bumped. Parse with ast.literal_eval (not
# a regex) so it's robust to quote style, line wrapping, or comments in the list. python3 is in the image.
echo "Reading SOURCE_FILES from chelper/__init__.py ..."
SRCS=$(cd "$CHELPER_DIR" && python3 - <<'PY'
import ast
tree = ast.parse(open("__init__.py").read())
files = None
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "SOURCE_FILES" for t in node.targets):
        files = ast.literal_eval(node.value)
if not files:
    raise SystemExit("SOURCE_FILES not found in chelper/__init__.py")
print("\n".join(files))
PY
) || { echo "failed to read SOURCE_FILES from chelper/__init__.py" >&2; exit 6; }

# Absolute source paths in an array (not word-splitting) so a space in the repo path stays safe.
mapfile -t SRC_REL <<< "$SRCS"
SRC_PATHS=()
for f in "${SRC_REL[@]}"; do [ -n "$f" ] && SRC_PATHS+=("${CHELPER_DIR}/$f"); done
if [ "${#SRC_PATHS[@]}" -eq 0 ]; then echo "empty SOURCE_FILES list" >&2; exit 6; fi
echo "  ${#SRC_PATHS[@]} source files"

echo "Building c_helper ..."
"$CC" --sysroot="$SYSROOT" \
  -shared -fPIC -O2 -Wall \
  -mips32r2 -mabi=32 -mhard-float -mfp64 \
  -mnan=2008 -Wa,-mnan=2008 \
  -o "${CHELPER_DIR}/c_helper.so" \
  "${SRC_PATHS[@]}"
echo "OK"

echo "Result file:"
ls -la "${CHELPER_DIR}/c_helper.so"
echo ""

echo "Copy file from klipper to current dir ..."
cp -v "${CHELPER_DIR}/c_helper.so" .
md5sum ./c_helper.so
echo ""

echo "ELF infos:"
../read-elf-infos.sh "${CHELPER_DIR}/c_helper.so" 2>&1 | tail -n 5
echo ""

echo "Finished"
