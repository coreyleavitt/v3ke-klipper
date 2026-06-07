# Installation

This zip is flash-ready for the Ender 3 V3 KE running the mainline Klipper port.

## Contents

| Path | Description |
|---|---|
| `firmware/katapult.bin` | Katapult bootloader (SWD-flash via v3ke) |
| `firmware/klipper.bin` | Klipper MCU firmware (GD32F303RET6 / STM32F103) |
| `host/c_helper.so` | Klipper C helper extension (MIPS, cross-compiled for the Nebula Pad) |
| `host/klipper.elf` | Klipper host MCU ELF (ABI-verified: nan2008/fp64) |
| `host/klipper.dict` | Klipper data dictionary |
| `v3ke` | Hardware-ops CLI: flash, verify, backup |
| `INSTALL.md` | This file |
| `SOURCES.md` | GPL source offer |
| `manifest.json` | Build-provenance attestation (sha256, toolchain versions, submodule commits) |
| `LICENSES/` | License texts for each component |

## Flashing

Use the `v3ke` CLI (requires STLINK-V3SET connected via SWD):

```
./v3ke verify          # ABI-check the host artifacts before touching the printer
./v3ke flash backup    # save the stock firmware first (strongly recommended)
./v3ke flash katapult  # flash Katapult bootloader
./v3ke flash klipper   # flash Klipper MCU firmware via Katapult
./v3ke flash all       # backup + katapult + klipper in one shot
```

## Deployment

The full end-to-end procedure — SWD-flashing the motion MCU, staging and
validating the host artifacts, the live host swap (config, init.d, Moonraker),
rollback, and troubleshooting — is in **DEPLOY.md** in the repository. Wiring the
ST-Link to the board is covered in `mcu-firmware/swd-wiring.md`.

## Provenance

Exact pinned source commits and component versions are recorded in `manifest.json`
and summarised in `SOURCES.md`. Every sha256 in the manifest was computed over the
artifact bytes as they appear in this zip.
