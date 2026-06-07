# Deploying mainline Klipper to the Ender-3 V3 KE

This is the end-to-end procedure for replacing Creality's modified Klipper fork
with this **mainline Klipper** port. It folds together the per-component
installation notes (`mcu-firmware/*.md`, `klipper/klipper-installation.md`) into
one ordered workflow built around the `v3ke` CLI, and adds the parts that were
missing: the live host swap, rollback, and troubleshooting.

> **Printing is not gated on the load cell.** The CR-Touch alone homes Z and
> meshes the bed on mainline. Hands-free auto-Z-offset (the stock "PR-touch"
> behaviour, using the corner load cell) is a separate, deferred enhancement —
> see issue #1 / RFC #6. After this deploy you set Z-offset once by hand
> (`PROBE_CALIBRATE` / paper) and print.

## The machine has two processors

You are flashing/installing **two independent things**, and the order matters:

1. **Motion MCU** — GD32F303RET6 (built as STM32F103), on the mainboard.
   Flashed **over SWD** with an ST-Link (`v3ke flash`). This is the only step
   that needs hardware wiring, and you do it **once** (Katapult bootloader makes
   future updates serial-only).
2. **Host** — the Klipper *host* process (`klippy.py` + `c_helper.so` +
   `klipper_mcu.elf`) runs on the **Nebula Pad** (MIPS). Installed by copying
   files to `/usr/data` and patching `/etc/init.d` (`v3ke deploy` stages it;
   the live swap is manual — this guide).

`v3ke rollout` chains the safe, automatable parts (`verify` → `deploy` →
`flash all`) and then hands off to **Step 4** of this guide for the host swap.

## Filesystem & reversibility model

- **`/usr/data`** (`mmcblk0p10`) is real flash and **survives a firmware
  update / factory reset.** Mainline Klipper source, configs, and the v3ke
  staging dir live here.
- **`/` `/etc` `/root`** are an **overlayfs** — edits persist across *reboots*
  but a **firmware update / factory reset wipes them.** The `init.d` patches
  live here, so they must be **re-applied after any firmware update.**
- The **stock MCU firmware backup** (`stock-mcu-backup.bin`) lives on *your*
  machine, not the printer. Keep it — it's your only path back to stock motion
  firmware (see [Rollback](#rollback)).

---

## Prerequisites

- **Hardware:** ST-Link V3SET wired to the mainboard SWD pads — see
  [`mcu-firmware/swd-wiring.md`](mcu-firmware/swd-wiring.md). **Do the wiring first.**
- **Host tools:** `openocd` ≥ 0.12, an `ssh v3ke` alias that authenticates
  (ECDSA P-256 key — see the top-level `CLAUDE.md`), and the built `v3ke` binary.
- **Artifacts:** either build them (`tools/build.py image` once, then
  `tools/build.py artifacts`) or unzip a release. You need:
  `mcu-firmware/{katapult,klipper}.bin` (ARM MCU) and the MIPS host pieces
  `c_helper.so` / `klipper_mcu.elf` / `klipper.dict`.
- **Serial baud is 230400, not Klipper's 250000 default.** The shipped configs
  already set this; don't change it — 250000 fails outright.

---

## Step 0 — Back up everything (do not skip)

```bash
# Stock motion-MCU firmware → keep this file forever (your only route back to stock).
./v3ke flash backup            # writes stock-mcu-backup.bin

# Pull the current on-device host config (the printer is the source of truth).
P=10.70.0.130:7125
for f in printer.cfg gcode_macro.cfg moonraker.conf printer_params.cfg sensorless.cfg; do
  curl -s "http://$P/server/files/config/$f" -o "device-backup/$f"
done

# Back up the stock init.d scripts you're about to patch (overlay; for host rollback).
ssh v3ke 'cp /etc/init.d/S55klipper_service /usr/data/S55klipper_service.stock 2>/dev/null; \
          cp /etc/init.d/S57klipper_mcu     /usr/data/S57klipper_mcu.stock 2>/dev/null; \
          cp /etc/init.d/S14mcu_update      /usr/data/S14mcu_update.stock 2>/dev/null'
```

If `v3ke flash backup` warns the dump is all `0xFF`/empty, the stock firmware has
**read-out protection** — you can proceed, but you won't be able to restore
Creality's firmware. Decide before erasing.

---

## Step 1 — Build / obtain and verify artifacts

```bash
# Build (skip if you unzipped a release):
tools/build.py image        # one-time, ~20-40 min (container toolchain)
tools/build.py artifacts    # ARM MCU fw + MIPS host pieces + ABI check

# Assert the MIPS host artifacts match the device ABI BEFORE touching the printer.
./v3ke verify               # mipsel / nan2008 / o32 / mips32r2 / fp64, loader ld-linux-mipsn8.so.1
```

`v3ke verify` must print `PASSED`. A `FAILED` here means the host binaries won't
load on the Nebula Pad — fix the build before going further.

---

## Step 2 — Flash the motion MCU over SWD

With the ST-Link wired and the printer powered on:

```bash
# V3SET is the default. For an ST-Link V2: export V3KE_INTERFACE=interface/stlink.cfg
./v3ke flash all            # backup + erase + Katapult @0x08000000 + Klipper @0x08002000
```

`flash all` backs up, prompts for confirmation, erases, flashes both images, and
read-back-verifies each. You can also run the stages individually
(`flash katapult`, then `flash klipper`). When it prints
`Done — power-cycle the printer.`, power-cycle.

To flash by hand (or debug a bad connection): wire per
[`mcu-firmware/swd-wiring.md`](mcu-firmware/swd-wiring.md), then run the openocd
commands in [`mcu-firmware/katapult-installation.md`](mcu-firmware/katapult-installation.md)
and [`mcu-firmware/klipper-installation.md`](mcu-firmware/klipper-installation.md).

---

## Step 3 — Stage & validate the host artifacts (non-destructive)

```bash
./v3ke deploy               # pushes to /usr/data/v3ke-staging, validates on-device
```

This streams `c_helper.so` and `klipper_mcu.elf` to `/usr/data/v3ke-staging/`
(old dropbear has no scp/sftp, so it pipes over `ssh … 'cat > …'`), then proves
on the device that:

- `c_helper.so` **loads into the device's Python** (`ctypes.CDLL(...)` → `LOADED`),
- `klipper_mcu.elf` **passes the device loader** (`ld-linux-mipsn8.so.1 --verify`).

It does **not** touch the live install. A green `deploy` means the host swap in
Step 4 is safe.

---

## Step 4 — Install the mainline host (the live swap)

This is the manual part `v3ke rollout` hands off to. It replaces the host-side
Klipper. Run from the repo root.

```bash
# 4a. Host config files
rsync -vr --times klipper/printer-config-files/*.cfg v3ke:/usr/data/printer_data/config/

# 4b. Mainline Klipper source (keep it lean)
rsync -vr --times --exclude='.idea' --exclude='*.iml' --exclude='out/' \
  external/klipper/ v3ke:/usr/data/klipper-mainline/
ssh v3ke 'cd /usr/data/klipper-mainline && \
  git remote set-url origin https://github.com/Klipper3d/klipper.git'   # old dropbear: HTTPS not SSH

# 4c. The cross-compiled host binaries (use the staged, already-validated copies)
ssh v3ke 'cp /usr/data/v3ke-staging/c_helper.so     /usr/data/klipper-mainline/klippy/chelper/c_helper.so; \
          cp /usr/data/v3ke-staging/klipper_mcu.elf /usr/data/klipper-mainline/klipper_mcu.elf; \
          chmod +x /usr/data/klipper-mainline/klipper_mcu.elf'

# 4d. init.d patches — point services at the mainline install, disable auto-update
rsync -vr --times klipper/printer-filesystem-patches/etc/init.d/ v3ke:/etc/init.d/

# 4e. Mainsail config (web UI)
ssh v3ke 'cd /usr/data && [ -d mainsail-config ] || \
  git clone https://github.com/mainsail-crew/mainsail-config.git'
```

What the init.d patches do (shipped in
`klipper/printer-filesystem-patches/etc/init.d/`):

- **`S55klipper_service`** — runs `/usr/data/klipper-mainline/klippy/klippy.py`
  (instead of stock `/usr/share/klipper/...`), and disables the stock
  `copy_config()` step.
- **`S57klipper_mcu`** — runs `/usr/data/klipper-mainline/klipper_mcu.elf -r`
  (the host MCU), socket `/tmp/klipper_host_mcu`.
- **`S14mcu_update`** — disabled, so Creality's auto-updater can't clobber the
  mainline firmware.

> **Config dir note:** the shipped `S55klipper_service` reads
> `/usr/data/printer_data/config`. The upstream install notes also mention a
> parallel `config-mainline/` dir used for side-by-side manual debugging — if you
> want to test without overwriting the stock config, copy into a separate dir and
> pass it explicitly in the manual-start command below. For a committed switch,
> the standard `config/` dir (4a) is correct.

---

## Step 5 — Bring it up

```bash
# Optional dry run in the foreground first (Ctrl-C to stop; watch for "Printer is ready"):
ssh v3ke 'ps waux | grep [k]lipper | awk "{print \$1}" | xargs kill; \
          /usr/share/klippy-env/bin/python /usr/data/klipper-mainline/klippy/klippy.py \
            /usr/data/printer_data/config/printer.cfg'

# Then the real thing via init.d:
ssh v3ke 'ps waux | grep [k]lipper | awk "{print \$1}" | xargs kill; \
          /etc/init.d/S57klipper_mcu restart; \
          /etc/init.d/S55klipper_service restart'
```

Verify it came up:

```bash
P=10.70.0.130:7125
curl -s http://$P/printer/info | jq '.result.state, .result.state_message'   # want "ready"
ssh v3ke 'tail -n 40 /usr/data/printer_data/logs/klippy.log'
```

A bad config leaves Klipper in a **shutdown** state rather than failing at upload
time — `klippy.log` is the first stop.

---

## Step 6 — Smoke test

```bash
P=10.70.0.130:7125
curl -s "http://$P/printer/gcode/script?script=G28" | jq .          # home (CR-Touch homes Z)
curl -s "http://$P/printer/gcode/script?script=BED_MESH_CALIBRATE" | jq .
curl -s "http://$P/printer/objects/query?print_stats" | jq .
```

Then set Z-offset once (`PROBE_CALIBRATE` → `SAVE_CONFIG`, or paper-gauge +
`Z_OFFSET_APPLY_PROBE`) and run a first-layer test. Hands-free auto-Z via the
corner load cell is **not** part of this deploy — tracked in issue #1 / RFC #6.

Optional low-level serial check of the freshly flashed motion MCU:

```bash
ssh v3ke 'ps waux | grep [k]lipper | awk "{print \$1}" | xargs kill; \
          /usr/share/klippy-env/bin/python /usr/data/klipper-mainline/klippy/console.py \
            -b 230400 /dev/ttyS1'
```

---

## Rollback

The two processors roll back **independently.**

**Host → stock Creality Klipper** (overlay change):

```bash
# Restore the stock init.d scripts you saved in Step 0, then restart.
ssh v3ke 'cp /usr/data/S55klipper_service.stock /etc/init.d/S55klipper_service; \
          cp /usr/data/S57klipper_mcu.stock     /etc/init.d/S57klipper_mcu; \
          cp /usr/data/S14mcu_update.stock      /etc/init.d/S14mcu_update; \
          ps waux | grep [k]lipper | awk "{print \$1}" | xargs kill; \
          /etc/init.d/S57klipper_mcu restart; /etc/init.d/S55klipper_service restart'
```

(A firmware update / factory reset also restores the stock overlay — but wipes
your patches, so you'd re-run Step 4 to get mainline back.)

**Motion MCU → stock firmware** (SWD, needs the ST-Link again):

```bash
openocd -f interface/stlink-dap.cfg -f target/stm32f3x.cfg \
  -c "adapter speed 100; init; reset halt; \
      flash write_image erase stock-mcu-backup.bin 0x08000000; reset run; exit"
```

This only works if your Step 0 backup was a real dump (not RDP-blocked).

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| MCU never connects, timeouts | Baud is `250000` somewhere — must be **`230400`**. Check `[mcu] baud` in `printer.cfg`. |
| openocd: "no device found" / can't init | Wrong interface cfg (V3SET → `stlink-dap.cfg`, V2 → `stlink.cfg`), bad SWD wiring, or board not powered. See [`mcu-firmware/swd-wiring.md`](mcu-firmware/swd-wiring.md). |
| openocd can't `halt` the core | Use connect-under-reset (`srst`); see the wiring guide's boot/reset note. Rare on a stock/our-build MCU. |
| `v3ke flash backup` dumps all `0xFF` | Stock firmware is read-protected (RDP). You can flash mainline but can't restore stock. |
| `v3ke verify` FAILED | Host artifacts don't match the device ABI — rebuild (`tools/build.py artifacts`), don't deploy. |
| `c_helper.so` won't load on device (`deploy` fails) | ABI mismatch or wrong glibc — re-run `verify`/`artifacts`; confirm the toolchain image. |
| Klipper state `shutdown` after restart | Config error — read `/usr/data/printer_data/logs/klippy.log`; fix the cfg, `firmware_restart`. |
| Web UI / Moonraker missing config | `mainsail-config` not cloned (Step 4e) or Moonraker not restarted. |

---

## What's automated vs manual

| Stage | Command | Touches hardware? |
|---|---|---|
| ABI check | `v3ke verify` | no |
| Stage + on-device validate | `v3ke deploy` | no (non-destructive) |
| Flash motion MCU | `v3ke flash {backup\|katapult\|klipper\|all}` | **yes — SWD** |
| Verify → deploy → flash, chained | `v3ke rollout` | yes (then hands off to Step 4) |
| Host swap (configs, source, init.d) | **manual — Step 4** | overlay + `/usr/data` |
