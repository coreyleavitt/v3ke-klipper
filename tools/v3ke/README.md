# v3ke — Ender 3 V3 KE mainline toolkit (hardware side)

A single Nim binary for the **hardware-interacting** steps. The build side (toolchain +
artifacts) lives elsewhere and is Python/podman; this tool is what you run on a PC to put the
built artifacts onto the printer.

## Commands
- `v3ke verify [<c_helper.so> <klipper_mcu.elf>]` — assert the MIPS host artifacts match the
  device ABI (mipsel · nan2008 · o32 · mips32r2, loader `ld-linux-mipsn8.so.1`). Parses the ELF
  natively — **no `readelf`/binutils needed**, so it runs on a bare machine (incl. Windows).
- `v3ke deploy [<c_helper.so> <klipper_mcu.elf>]` — push host artifacts to a staging dir on the
  device and validate they load against the device's own glibc/Python. **Non-destructive.**
  Needs `ssh` reachable to the printer. Env: `V3KE_HOST` (default the `v3ke` ssh-config alias).
- `v3ke flash {backup [file] | katapult | klipper | all}` — flash the motion MCU (GD32F303) over
  SWD via openocd + ST-Link. `all` backs up the stock firmware (with RDP/blank detection),
  confirms, erases, flashes, and **read-back-verifies** each image. Needs `openocd` on PATH.
  - Env: `FW_DIR` (default `mcu-firmware`), `V3KE_INTERFACE` (default `interface/stlink-dap.cfg`;
    use `interface/stlink.cfg` for an ST-Link V2).
- `v3ke rollout` — switch to mainline: `verify` → `deploy` → `flash`, stopping on any failure.
  The final live-system swap (install klippy, init.d, restart services) stays manual per DEPLOY.md.

## Build
Needs Nim ≥ 2.0 (only to build; the result is a dependency-free binary):
```
nim c -d:release --opt:size -o:v3ke v3ke.nim      # or: nimble build
```
No host Nim? Build in a container:
```
podman run --rm -v "$PWD":/work -w /work <nim-image> \
  nim c -d:release --opt:size -o:v3ke v3ke.nim
```
The compiled `v3ke` binary is gitignored; ship it in the release zip alongside the prebuilt
`firmware/` + `host/` artifacts.
