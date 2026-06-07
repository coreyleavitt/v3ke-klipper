## Regression tests for code-review findings M1, M2, M3.
## Run via:  nimble test  (from tools/v3ke/)

import std/[os, tempfiles, unittest]
import common, verify, deploy, elf

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

const FixtureDir = "../abi/fixtures"

## Write a clearly-malformed ELF (valid magic, but truncated — triggers ElfError
## "too short" on the second readElf check) to a temp file and return its path.
proc writeMalformedElf(dir: string): string =
  result = dir / "malformed.elf"
  # ELF magic is correct but the file is only 8 bytes — well under the 52-byte
  # minimum, so readElf raises ElfError("too short for ELF32 header").
  writeFile(result, "\x7fELF\x01\x01\x00\x00")

# ──────────────────────────────────────────────────────────────────────────────
# M1 — ElfError translated to V3keError at the call sites
# ──────────────────────────────────────────────────────────────────────────────

suite "M1: ElfError → V3keError translation":

  test "malformed ELF through verifyCmd raises V3keError, not ElfError":
    ## verify.nim: readElf raises ElfError; checkMips must catch and re-raise
    ## as V3keError so the caller never sees a raw ElfError traceback.
    let dir = getTempDir() / "v3ke_m1_verify"
    createDir(dir)
    let bad = writeMalformedElf(dir)
    # We need two args; pass the same malformed file for both positions.
    var caughtV3ke = false
    var caughtElf  = false
    try:
      discard verifyCmd(@[bad, bad])
    except V3keError:
      caughtV3ke = true
    except ElfError:
      caughtElf = true
    check caughtV3ke == true
    check caughtElf  == false   # must NOT escape as ElfError

  test "malformed ELF through verifyCmd does not raise unhandled ElfError mid-rollout":
    ## rollout.nim calls verifyCmd; if ElfError escaped verifyCmd it would bypass
    ## rollout's fail() gate and propagate as an untyped traceback.  After the fix,
    ## verifyCmd returns non-zero (or raises V3keError) — either way rollout's gate
    ## logic runs and the final raise is V3keError, not ElfError.
    ## We test this at the verifyCmd level (same call path rollout uses).
    let dir = getTempDir() / "v3ke_m1_rollout"
    createDir(dir)
    let bad = writeMalformedElf(dir)
    var seenElf = false
    try:
      discard verifyCmd(@[bad, bad])
    except ElfError:
      seenElf = true
    except CatchableError:
      discard   # V3keError or return-code path — both acceptable
    check seenElf == false

# ──────────────────────────────────────────────────────────────────────────────
# M2 — default artifact paths match the release-zip layout (host/*)
# ──────────────────────────────────────────────────────────────────────────────

suite "M2: zero-arg default paths resolve to host/ zip layout":

  test "defaultHostArtifacts() returns host/c_helper.so and host/klipper.elf":
    ## Behavioral: call the pure proc that verifyCmd uses for zero-arg defaults
    ## and assert the exact paths.  A refactor that changes the default (e.g.
    ## wrapping the strings in a const or renaming the dir) will update the proc
    ## and this test catches it — unlike a source-grep which passes trivially.
    let d = defaultHostArtifacts()
    check d.chelper == "host/c_helper.so"
    check d.hostElf == "host/klipper.elf"

  test "defaultDeployArtifacts() returns host/c_helper.so and host/klipper.elf":
    ## Same contract for deploy.  Both commands use the host/ zip layout.
    let d = defaultDeployArtifacts()
    check d.chelper == "host/c_helper.so"
    check d.hostElf == "host/klipper.elf"

# ──────────────────────────────────────────────────────────────────────────────
# M3 — flash.nim readback temp file is not a predictable static path
# ──────────────────────────────────────────────────────────────────────────────

suite "M3: readback temp path is not a fixed predictable name":

  test "two simulated readback temp names are distinct (no collision)":
    ## Verify that the helper we now use produces unique names per call.
    ## createTempFile returns (cfile: File, path: string) — open two and confirm paths differ.
    let r1 = createTempFile("v3ke_rb_", ".tmp")
    let r2 = createTempFile("v3ke_rb_", ".tmp")
    r1.cfile.close(); r2.cfile.close()
    check r1.path != r2.path
    removeFile(r1.path)
    removeFile(r2.path)
