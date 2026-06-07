## Version module tests (slice C1) — TDD, one behavior at a time.
## Run (from tools/v3ke/ inside the v3ke-dev container):
##   nim c --hints:off --path:. -r tests/tversion.nim

import std/[strutils, unittest]
import version

suite "resolveVersion":
  test "exact tag":
    check resolveVersion("v0.1.0") == "v0.1.0"

  test "tag + commits ahead + sha preserved":
    check resolveVersion("v0.1.0-5-gabcdef123456") == "v0.1.0-5-gabcdef123456"

  test "trailing newline stripped":
    check resolveVersion("v0.1.0\n") == "v0.1.0"

  test "empty string raises VersionError with actionable message":
    var caught = false
    try:
      discard resolveVersion("")
    except VersionError as e:
      caught = true
      check e.msg.contains("v0.1.0")
      check e.msg.contains("git tag")
      check e.msg.contains("git push")
      check not e.msg.contains("unknown")
    check caught

  test "whitespace-only raises VersionError":
    var caught = false
    try:
      discard resolveVersion("   \n ")
    except VersionError:
      caught = true
    check caught

  test "non-v* hash raises VersionError mentioning malformed":
    var caught = false
    try:
      discard resolveVersion("deadbeef")
    except VersionError as e:
      caught = true
      check e.msg.contains("malformed")
    check caught

suite "no hardcoded version":
  test "version.nim uses git describe and has no bare 0.1.0 literal":
    let src = readFile("version.nim")
    check src.contains("resolveVersion")
    check src.contains("git describe")
    check not src.contains("\"0.1.0\"")

  test "v3ke.nimble uses git describe and has no hardcoded 0.1.0 as source of truth":
    let src = readFile("v3ke.nimble")
    check src.contains("git describe")
    check not src.contains("version       = \"0.1.0\"")
    check not src.contains("\"0.1.0\"")
