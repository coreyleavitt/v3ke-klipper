# Source Code Offer

This release includes software licensed under the GNU General Public License (GPL).
In accordance with those licenses, the corresponding source code is available.

## Components and Upstream Sources

### Klipper

- **License:** GNU General Public License v3 (see `LICENSES/klipper.LICENSE`)
- **Upstream:** <https://github.com/Klipper3d/klipper.git>
- **Built commit:** recorded in `manifest.json` under `sources.klipper.commit`

### Katapult

- **License:** GNU General Public License v3 (see `LICENSES/katapult.LICENSE`)
- **Upstream:** <https://github.com/Arksine/katapult.git>
- **Built commit:** recorded in `manifest.json` under `sources.katapult.commit`

### Mainsail-config

- **License:** see `LICENSES/mainsail-config.LICENSE`
- **Upstream:** <https://github.com/mainsail-crew/mainsail-config.git>
- **Built commit:** recorded in `manifest.json` under `sources.mainsail-config.commit`

## Obtaining the Source

The exact commit SHA for each component built into this release is recorded in
`manifest.json`. To obtain the corresponding source:

```
git clone <upstream-url>
git checkout <commit-sha-from-manifest>
```

The toolchain used to cross-compile host artifacts (glibc, gcc, binutils, linux
kernel headers — versions in `manifest.json` under `build.toolchain`) was produced
by [crosstool-ng](https://github.com/crosstool-ng/crosstool-ng) from the pinned
sources recorded in `toolchain/Containerfile` in this repository.

If you have difficulty obtaining a copy of the source, contact:
<corey@knurl.io>
