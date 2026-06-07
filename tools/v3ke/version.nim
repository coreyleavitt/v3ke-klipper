## Version resolution for the v3ke CLI.
## resolveVersion is pure (test seam: git output is passed as a string).
## The stamped version is injected at build time via -d:v3keVersion:<output>.
import std/strutils
import common

type VersionError* = object of V3keError
  ## Raised when the binary has no usable git-derived version.

proc resolveVersion*(describeOutput: string): string =
  ## Turn `git describe --match 'v*' --abbrev=12` output into the CLI version string.
  ## Pure: takes the raw describe text (the test seam — git is mocked by passing a string).
  ## Raises VersionError (loud, actionable) when describe yielded nothing usable.
  let raw = describeOutput.strip()
  if raw.len == 0:
    raise newException(VersionError,
      "v3ke has no version: `git describe --match 'v*'` returned nothing.\n" &
      "Create and push the one-time bootstrap tag, then rebuild:\n" &
      "    git tag -a v0.1.0 -m \"bootstrap\" && git push origin v0.1.0")
  if not raw.startsWith("v"):
    raise newException(VersionError,
      "v3ke version is malformed (expected a v* tag, got " & raw.escape & ").\n" &
      "Check the bootstrap tag: it must be named like v0.1.0.")
  return raw

const overrideVersion {.strdefine: "v3keVersion".} = ""
  ## Build-time override: nim c -d:v3keVersion:$(git describe --match 'v*' --abbrev=12) ...
const stampedVersionRaw =
  when overrideVersion.len > 0: overrideVersion
  else: staticExec("git describe --match 'v*' --abbrev=12 2>/dev/null")

proc version*(): string =
  ## The CLI version. Raises VersionError if the binary wasn't stamped.
  ## Fails loud with a bootstrap-tag action message — never falls back to a fake string.
  resolveVersion(stampedVersionRaw)
