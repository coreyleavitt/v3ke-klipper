"""D1 unit tests — image --push: parse_repo_digest, write_image_digest, command construction.

Slice D1: pure Python, offline. No container, no network, no real registry.

TDD order (each RED→GREEN):
  D1-1:  parse_repo_digest — valid line → sha256 token
  D1-2:  parse_repo_digest — whitespace tolerance
  D1-3:  parse_repo_digest — list/bracket form Docker may emit
  D1-4:  parse_repo_digest — raises ValueError on garbage / missing / wrong-length hex
  D1-5:  write_image_digest — writes a grep-matchable file
  D1-6:  write_image_digest — round-trips through a temp path
  D1-7:  write_image_digest — raises ValueError on malformed digest
  D1-8:  command construction — build, push, inspect sequence via fake runner
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable

import pytest

# ---------------------------------------------------------------------------
# Helpers imported under test — not yet defined; RED will fail here first.
# ---------------------------------------------------------------------------

from build_main import parse_repo_digest, write_image_digest, cmd_image

# A valid 64-hex-char sha256 digest for reuse across tests.
_VALID_DIGEST = "sha256:" + "a" * 64
_VALID_HEX = "a" * 64
_FULL_IMAGE = "ghcr.io/coreyleavitt/v3ke-toolchain:v0.1.0"
_DIGEST_LINE = f"{_FULL_IMAGE}@{_VALID_DIGEST}"

_GREP_PAT = re.compile(r"^sha256:[0-9a-f]{64}$", re.MULTILINE)


# ===========================================================================
# D1-1 to D1-4: parse_repo_digest
# ===========================================================================


class TestParseRepoDigest:
    """parse_repo_digest extracts a bare sha256:<64-hex> token from inspect output."""

    # D1-1: canonical single-line output
    def test_valid_line_returns_sha256_token(self):
        line = f"ghcr.io/coreyleavitt/v3ke-toolchain@sha256:{'b' * 64}"
        result = parse_repo_digest(line)
        assert result == f"sha256:{'b' * 64}"

    # D1-2: whitespace tolerance — leading/trailing spaces, newline
    def test_strips_surrounding_whitespace(self):
        line = f"  ghcr.io/coreyleavitt/v3ke-toolchain@sha256:{'c' * 64}  \n"
        result = parse_repo_digest(line)
        assert result == f"sha256:{'c' * 64}"

    def test_newline_only_after_token(self):
        line = f"repo/image@sha256:{'d' * 64}\n"
        result = parse_repo_digest(line)
        assert result == f"sha256:{'d' * 64}"

    # D1-3: bracket / list form — podman sometimes emits ["repo@sha256:..."]
    def test_bracket_form_single_element(self):
        hex64 = "e" * 64
        line = f'[ghcr.io/coreyleavitt/v3ke-toolchain@sha256:{hex64}]'
        result = parse_repo_digest(line)
        assert result == f"sha256:{hex64}"

    def test_bracket_form_with_quotes(self):
        hex64 = "f" * 64
        line = f'["ghcr.io/coreyleavitt/v3ke-toolchain@sha256:{hex64}"]'
        result = parse_repo_digest(line)
        assert result == f"sha256:{hex64}"

    def test_bare_digest_no_registry_prefix(self):
        """Just sha256:<hex64> alone (no image prefix) is also accepted."""
        line = f"sha256:{'1' * 64}"
        result = parse_repo_digest(line)
        assert result == f"sha256:{'1' * 64}"

    # D1-4: error cases
    def test_raises_on_empty_string(self):
        with pytest.raises(ValueError, match="sha256"):
            parse_repo_digest("")

    def test_raises_on_garbage(self):
        with pytest.raises(ValueError):
            parse_repo_digest("not a digest at all")

    def test_raises_on_short_hex(self):
        """sha256: followed by only 63 hex chars must be rejected."""
        short = "sha256:" + "a" * 63
        with pytest.raises(ValueError, match="sha256"):
            parse_repo_digest(short)

    def test_raises_on_uppercase_hex(self):
        """Uppercase hex is not valid — registry digests are always lowercase."""
        upper = f"ghcr.io/image@sha256:{'A' * 64}"
        with pytest.raises(ValueError):
            parse_repo_digest(upper)

    def test_raises_on_missing_sha256_prefix(self):
        with pytest.raises(ValueError):
            parse_repo_digest(f"ghcr.io/image@md5:{'a' * 32}")

    def test_raises_on_too_long_hex(self):
        """65-char hex after sha256: is also invalid."""
        with pytest.raises(ValueError):
            parse_repo_digest("sha256:" + "a" * 65)


# ===========================================================================
# D1-5 to D1-7: write_image_digest
# ===========================================================================


class TestWriteImageDigest:
    """write_image_digest writes a grep-matchable file with a single sha256 line."""

    def test_grep_pattern_matches_written_digest(self, tmp_path):
        """The canonical CI grep must match exactly the digest line."""
        path = tmp_path / "IMAGE_DIGEST"
        write_image_digest(path, _VALID_DIGEST)
        text = path.read_text()
        matches = _GREP_PAT.findall(text)
        assert len(matches) == 1
        assert matches[0] == _VALID_DIGEST

    def test_grep_pattern_matches_only_one_line(self, tmp_path):
        """Exactly one ^sha256:<64hex>$ line — no duplicates, no comment contamination."""
        path = tmp_path / "IMAGE_DIGEST"
        write_image_digest(path, _VALID_DIGEST)
        text = path.read_text()
        assert _GREP_PAT.findall(text) == [_VALID_DIGEST]

    def test_round_trips_digest(self, tmp_path):
        """write then re-read via grep yields the same digest token."""
        path = tmp_path / "IMAGE_DIGEST"
        write_image_digest(path, _VALID_DIGEST)
        text = path.read_text()
        matches = _GREP_PAT.findall(text)
        assert matches[0] == _VALID_DIGEST

    def test_comment_header_is_present(self, tmp_path):
        """File must contain a comment explaining its provenance."""
        path = tmp_path / "IMAGE_DIGEST"
        write_image_digest(path, _VALID_DIGEST)
        text = path.read_text()
        assert "#" in text, "File must include a comment header line"

    def test_digest_line_is_bare_sha256_format(self, tmp_path):
        """The matching line must be exactly sha256:<64hex> with no surrounding chars."""
        path = tmp_path / "IMAGE_DIGEST"
        write_image_digest(path, _VALID_DIGEST)
        text = path.read_text()
        for line in text.splitlines():
            if line.startswith("sha256:"):
                assert re.fullmatch(r"sha256:[0-9a-f]{64}", line), (
                    f"sha256 line must be exactly 'sha256:<64hex>', got: {line!r}"
                )

    def test_raises_on_missing_sha256_prefix(self, tmp_path):
        path = tmp_path / "IMAGE_DIGEST"
        with pytest.raises(ValueError, match="sha256"):
            write_image_digest(path, "a" * 64)

    def test_raises_on_short_hex(self, tmp_path):
        path = tmp_path / "IMAGE_DIGEST"
        with pytest.raises(ValueError):
            write_image_digest(path, "sha256:" + "a" * 63)

    def test_raises_on_uppercase_hex(self, tmp_path):
        path = tmp_path / "IMAGE_DIGEST"
        with pytest.raises(ValueError):
            write_image_digest(path, "sha256:" + "A" * 64)

    def test_raises_on_extra_suffix(self, tmp_path):
        """sha256:<64hex>:extra is not a valid digest."""
        path = tmp_path / "IMAGE_DIGEST"
        with pytest.raises(ValueError):
            write_image_digest(path, "sha256:" + "a" * 64 + ":extra")

    def test_overwrites_existing_file(self, tmp_path):
        """write_image_digest must replace an existing file, not append."""
        path = tmp_path / "IMAGE_DIGEST"
        path.write_text("old content\n")
        new_digest = "sha256:" + "b" * 64
        write_image_digest(path, new_digest)
        text = path.read_text()
        assert "old content" not in text
        matches = _GREP_PAT.findall(text)
        assert matches == [new_digest]


# ===========================================================================
# D1-8: command construction — fake runner
# ===========================================================================


class FakeRunner:
    """Records all commands issued and returns configurable per-command outputs."""

    def __init__(self, outputs: dict[str, str] | None = None):
        self.calls: list[list[str]] = []
        self._outputs = outputs or {}

    def __call__(self, cmd: list[str]) -> str | None:
        key = " ".join(str(c) for c in cmd)
        self.calls.append(list(cmd))
        # Return configured output if present; default to None (like run() -> None).
        for pattern, output in self._outputs.items():
            if pattern in key:
                return output
        return None


def _make_namespace(**kwargs):
    """Build a simple argparse-like namespace."""
    import types
    ns = types.SimpleNamespace()
    defaults = {
        "image": _FULL_IMAGE,
        "runtime": "podman",
        "ctng_version": None,
        "push": True,
    }
    defaults.update(kwargs)
    for k, v in defaults.items():
        setattr(ns, k, v)
    return ns


class TestImagePushCommandConstruction:
    """cmd_image with --push issues build → push → inspect in order, then writes IMAGE_DIGEST."""

    def _run_cmd_image(self, runner, digest_path, **ns_kwargs):
        """Helper: invoke cmd_image with a fake runner and temp IMAGE_DIGEST path."""
        a = _make_namespace(**ns_kwargs)
        cmd_image(a, runner=runner, digest_path=digest_path)

    def _make_runner_with_inspect_output(self, runtime, image, hex64="a" * 64):
        inspect_output = f"{image}@sha256:{hex64}"
        outputs = {"inspect": inspect_output}
        return FakeRunner(outputs=outputs), hex64

    # --- build command is always first ---

    def test_build_command_is_first(self, tmp_path):
        runner, _ = self._make_runner_with_inspect_output("podman", _FULL_IMAGE)
        self._run_cmd_image(runner, tmp_path / "IMAGE_DIGEST")
        assert runner.calls[0][0] == "podman"
        assert "build" in runner.calls[0]

    def test_build_tags_image(self, tmp_path):
        runner, _ = self._make_runner_with_inspect_output("podman", _FULL_IMAGE)
        self._run_cmd_image(runner, tmp_path / "IMAGE_DIGEST")
        build_cmd = runner.calls[0]
        assert "-t" in build_cmd
        assert _FULL_IMAGE in build_cmd

    # --- push command follows build ---

    def test_push_command_issued_after_build(self, tmp_path):
        runner, _ = self._make_runner_with_inspect_output("podman", _FULL_IMAGE)
        self._run_cmd_image(runner, tmp_path / "IMAGE_DIGEST")
        push_cmd = runner.calls[1]
        assert "push" in push_cmd
        assert _FULL_IMAGE in push_cmd

    def test_push_uses_correct_runtime_podman(self, tmp_path):
        runner, _ = self._make_runner_with_inspect_output("podman", _FULL_IMAGE)
        self._run_cmd_image(runner, tmp_path / "IMAGE_DIGEST", runtime="podman")
        push_cmd = runner.calls[1]
        assert push_cmd[0] == "podman"

    def test_push_uses_correct_runtime_docker(self, tmp_path):
        image = "ghcr.io/coreyleavitt/v3ke-toolchain:v0.2.0"
        runner, _ = self._make_runner_with_inspect_output("docker", image)
        self._run_cmd_image(runner, tmp_path / "IMAGE_DIGEST", runtime="docker", image=image)
        push_cmd = runner.calls[1]
        assert push_cmd[0] == "docker"

    # --- inspect command follows push ---

    def test_inspect_command_issued_after_push(self, tmp_path):
        runner, _ = self._make_runner_with_inspect_output("podman", _FULL_IMAGE)
        self._run_cmd_image(runner, tmp_path / "IMAGE_DIGEST")
        inspect_cmd = runner.calls[2]
        assert "inspect" in inspect_cmd
        assert _FULL_IMAGE in inspect_cmd

    def test_inspect_uses_repo_digests_format(self, tmp_path):
        runner, _ = self._make_runner_with_inspect_output("podman", _FULL_IMAGE)
        self._run_cmd_image(runner, tmp_path / "IMAGE_DIGEST")
        inspect_cmd = runner.calls[2]
        assert any("RepoDigests" in str(arg) for arg in inspect_cmd)

    def test_inspect_uses_correct_runtime(self, tmp_path):
        runner, _ = self._make_runner_with_inspect_output("podman", _FULL_IMAGE)
        self._run_cmd_image(runner, tmp_path / "IMAGE_DIGEST", runtime="podman")
        inspect_cmd = runner.calls[2]
        assert inspect_cmd[0] == "podman"

    # --- IMAGE_DIGEST written after inspect ---

    def test_image_digest_file_written(self, tmp_path):
        path = tmp_path / "IMAGE_DIGEST"
        runner, hex64 = self._make_runner_with_inspect_output("podman", _FULL_IMAGE)
        self._run_cmd_image(runner, path)
        assert path.exists(), "IMAGE_DIGEST must be written after push+inspect"

    def test_image_digest_file_contains_correct_digest(self, tmp_path):
        path = tmp_path / "IMAGE_DIGEST"
        hex64 = "b" * 64
        runner, _ = self._make_runner_with_inspect_output("podman", _FULL_IMAGE, hex64=hex64)
        self._run_cmd_image(runner, path)
        text = path.read_text()
        matches = _GREP_PAT.findall(text)
        assert matches == [f"sha256:{hex64}"]

    def test_exactly_three_commands_issued(self, tmp_path):
        runner, _ = self._make_runner_with_inspect_output("podman", _FULL_IMAGE)
        self._run_cmd_image(runner, tmp_path / "IMAGE_DIGEST")
        assert len(runner.calls) == 3, (
            f"Expected 3 commands (build, push, inspect); got {len(runner.calls)}: {runner.calls}"
        )

    # --- no push when --push not set ---

    def test_no_push_flag_skips_push_and_inspect(self, tmp_path):
        runner = FakeRunner()
        self._run_cmd_image(runner, tmp_path / "IMAGE_DIGEST", push=False)
        commands = [" ".join(c) for c in runner.calls]
        assert not any("push" in c for c in commands), (
            f"push must not be called when --push is False; commands: {commands}"
        )
        assert not any("inspect" in c for c in commands)

    def test_no_push_flag_does_not_write_digest(self, tmp_path):
        path = tmp_path / "IMAGE_DIGEST"
        runner = FakeRunner()
        self._run_cmd_image(runner, path, push=False)
        assert not path.exists(), "IMAGE_DIGEST must not be written when --push is False"


# ===========================================================================
# Integration marker — real network / runtime (excluded by default)
# ===========================================================================


@pytest.mark.integration
def test_image_push_integration_real_runtime():
    """Real push: requires a logged-in runtime and network. Excluded by default."""
    pytest.skip("integration test — run explicitly with -m integration")
