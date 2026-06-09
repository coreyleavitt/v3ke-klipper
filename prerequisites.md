# OS

This is tested on ubuntu linux.

# Install openocd

    $ openocd -v
    Open On-Chip Debugger 0.12.0-01004-g9ea7f3d64-dirty (2026-01-03-13:55)

# Submodules (klipper, katapult, mainsail-config under external/)

    $ git submodule update --init --recursive

# Reproducible cross-toolchain (podman)

The MIPS cross toolchain (host MCU + c_helper.so) and the arm-none-eabi MCU toolchain are
built from source via crosstool-ng inside a container — pinned to the device ABI
(glibc 2.29 · kernel 4.4 · mipsel · mips32r2 · o32 · hard-float · fp64 · nan2008). Needs only
podman + python3 on the build host (the rest is in the image):

    $ ./tools/build.py image       # ~20-40 min first time: bakes the toolchain into the image
    $ ./tools/build.py snapshot    # optional: back the toolchain up to a named volume (dev safety net)
    $ ./tools/build.py artifacts   # build all device artifacts using the image + verify ABI

The toolchain is baked into the image (self-contained / distributable); build-time cache mounts
make rebuilds cheap, and the snapshot volume preserves a known-good toolchain across image
rebuilds while iterating. See toolchain/Containerfile for the full ABI rationale.

## Publishing the toolchain image to ghcr (CI bootstrap)

CI pulls the toolchain image strictly by the digest in `toolchain/IMAGE_DIGEST` — it does not
build the image itself. The operator builds and publishes the image locally, then commits the
updated digest file so CI can pull it. See `docs/toolchain-image.md` for the full procedure.

# root printer with helper script

https://guilouz.github.io/Creality-Helper-Script-Wiki/firmwares/install-and-update-rooted-firmware-ender3/
