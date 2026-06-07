"""Harness smoke test (slice A0).

Proves the pytest dev loop runs (in the v3ke-dev podman image) before any real suites
exist — A1 onward replace/extend this. Kept as a liveness check, not a behavior test.
"""


def test_pytest_harness_is_live():
    assert True
