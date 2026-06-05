## `v3ke rollout` — switch the printer to mainline Klipper. Composes the pieces with hard gates,
## stopping on any failure:
##   1. verify  — local artifacts match the device ABI
##   2. deploy  — push host artifacts to staging + validate they load on the device
##   3. flash   — back up + flash the motion MCU over SWD (needs ST-Link)
##   4. (manual, for now) install mainline klippy + swap init.d + restart services
##
## Step 4 — the live host-side system migration in DEPLOY.md — is intentionally NOT automated yet:
## it's irreversible-ish and entangled with the probe/CR-Touch decision, so it stays a deliberate
## manual step until we script it with proper backups/rollback.
import common, verify, deploy, flash

proc rolloutCmd*(args: seq[string]): int =
  echo "=== v3ke rollout: switch the Ender 3 V3 KE to mainline Klipper ==="

  note("step 1/3: verify local artifacts match the device ABI")
  if verifyCmd(@[]) != 0: fail("artifacts failed ABI verification — aborting before any change")

  note("step 2/3: deploy + validate host artifacts on the device (non-destructive)")
  if deployCmd(@[]) != 0: fail("on-device validation failed — aborting before flashing")

  note("step 3/3: flash the motion MCU (requires ST-Link wired to the SWD pads)")
  if flashCmd(@["all"]) != 0: fail("flash failed")

  warn("Remaining (manual): install mainline klippy, swap /etc/init.d, restart klipper+moonraker")
  warn("per DEPLOY.md — the deliberate live-system step, once steps 1-3 are green.")
  okBanner("rollout: artifacts verified + deployed, MCU flashed. Finish the host swap per DEPLOY.md.")
  return 0
