## Harness smoke test (slice A0) — proves `nimble test` runs in the v3ke-dev podman image
## (C backend; gcc present) before the real Nim suites land (A1c onward).
import std/unittest

suite "harness":
  test "nim test runner is live":
    check true
