import std/strutils

proc pkgVersion(): string =
  let raw = staticExec("git describe --match 'v*' --abbrev=12 2>/dev/null").strip()
  if raw.len == 0: return "0.0.0"   # untagged working copy (bootstrap tag deferred)
  raw.strip(chars = {'v'}, leading = true, trailing = false).split('-')[0]

version     = pkgVersion()
author      = "corey"
description = "Ender 3 V3 KE mainline toolkit (hardware side): flash + verify"
license     = "MIT"
srcDir      = "."
bin         = @["v3ke"]

requires "nim >= 2.0.0"

# Unit tests run inside the v3ke-dev podman image (nim + gcc + uv) — never a host install.
# C backend (the shipped binary's backend), so ELF-byte parsing is exercised as it really runs.
# --path:. exposes elf.nim (in tools/v3ke/) to tests/ without copying.
task test, "Run Nim unit tests (C backend)":
  exec "nim c --hints:off --path:. -r tests/tharness.nim"
  exec "nim c --hints:off --path:. -r tests/tabi.nim"
  exec "nim c --hints:off --path:. -r tests/tversion.nim"
  exec "nim c --hints:off --path:. -r tests/tfindings.nim"
