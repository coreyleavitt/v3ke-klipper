# Wiring the ST-Link for SWD flashing

The GD32F303 motion MCU has **no serial/USB bootloader exposed on a stock KE**, so
the only way to put Katapult + mainline Klipper onto it the first time is **SWD**,
using an ST-Link wired to the five SWD pads on the mainboard. You do this **once**
— after Katapult is installed, future Klipper updates go over serial through the
bootloader and the ST-Link isn't needed again.

This doc covers the **physical hookup and interface selection**. For the actual
flash, use the `v3ke` CLI (easiest) or the per-component openocd commands in
[`katapult-installation.md`](katapult-installation.md) and
[`klipper-installation.md`](klipper-installation.md). End-to-end deployment is in
[`../DEPLOY.md`](../DEPLOY.md). The SWD pad → MCU pin map is in
[`../pinout/creality-mainboard-pinout.md`](../pinout/creality-mainboard-pinout.md#swd-pinout).

## What you need

- **ST-Link V3SET** (what this project targets) or an ST-Link V2/V2-1 clone.
- 5 jumper wires (Dupont/pogo). The SWD pads are small — short leads help; SWD is
  tolerant of moderate length at the 100 kHz adapter speed used here.
- The printer **powered on normally** during flashing (its 24 V PSU, or the Nebula
  Pad's USB power if the board is out). The ST-Link's `VCC`/`VTREF` line is a
  **voltage reference, not a power source** — do **not** power the board from the
  ST-Link.

## Wiring: ST-Link → mainboard SWD pads

Connect **by signal**, not by connector pin number — the ST-Link V3SET uses a
14-pin STDC14 connector (plus a legacy 20-pin via its adapter board), so match the
**signal labels** silk-screened on the adapter / documented in ST **UM2448** to the
mainboard pads:

| Mainboard pad | MCU pin | Signal | ST-Link signal to connect |
|---------------|---------|--------|---------------------------|
| `DIO`         | PA13    | SWDIO  | SWDIO / `TMS_SWDIO`       |
| `CLK`         | PA14    | SWCLK  | SWCLK / `TCK_SWCLK`       |
| `MRST`        | NRST    | nRESET | `NRST` / `RESET` (`T_NRST`) |
| `VCC`         | +3V3    | Vref   | `VTREF` (target voltage **reference**) |
| `GND`         | GND     | Ground | `GND`                     |

> **TODO (photo):** annotate `../pinout/img/creality-mainboard-ender-3-v3-ke.png`
> (or add a close-up) marking the physical location of the five SWD pads on the
> board, and a shot of the ST-Link V3SET STDC14 connector with its signal labels.
> These are the two images this doc is missing.

## Boot / reset — no BOOT0 needed

SWD flashing does **not** use the BOOT0 jumper (that's only for the serial/DFU ROM
bootloader, which we aren't using). The ST-Link halts the Cortex core directly over
SWD, so `openocd … reset halt` is all that's required — no button to hold, no jumper
to move.

This works because this build leaves **SWD enabled at runtime**:
`CONFIG_STM32F103GD_DISABLE_SWD is not set` in both `katapult.config` and
`klipper.config`. That GigaDevice-clone option, if enabled, would free PA13/PA14 for
GPIO but can make the pads unreliable to re-attach to — we don't use those pins, so
we keep SWD always available for re-flashing.

If openocd ever **can't halt the core** (firmware reconfigured the SWD pins, or a
brown-out), use openocd **connect-under-reset**: hold `NRST` low, `init`, then
release (`reset_config srst_only srst_nogate` + the `stlink-dap.cfg`
`connect_assert_srst` behaviour). On a stock or our-build MCU you won't need this.

## openocd interface file: V3 vs V2

| ST-Link   | openocd interface file       | Notes                                         |
|-----------|------------------------------|-----------------------------------------------|
| **V3SET** | `interface/stlink-dap.cfg`   | DAP driver — **default** (`V3KE_INTERFACE`, and what the install docs use) |
| V2 / V2-1 | `interface/stlink.cfg`       | legacy driver; set `V3KE_INTERFACE=interface/stlink.cfg` or substitute in the openocd commands |

The target is always `target/stm32f3x.cfg` (the GD32F303 speaks the STM32F3 debug
protocol over SWD), driven at a conservative `adapter speed 100` (kHz) for
reliability over short flying leads.

## Flashing (once wired)

- **Easiest — `v3ke` CLI** (wraps backup + erase + Katapult + Klipper with read-back
  verify): `v3ke flash all`. See [`../DEPLOY.md`](../DEPLOY.md) Step 2.
- **By hand — openocd:** follow [`katapult-installation.md`](katapult-installation.md)
  then [`klipper-installation.md`](klipper-installation.md). Flash base `0x08000000`;
  Katapult at `0x08000000` (8 KiB), Klipper at `0x08002000`.
- **Restore stock firmware:** see [`../DEPLOY.md`](../DEPLOY.md) → Rollback (writes
  your `stock-mcu-backup.bin` back over the chip).
