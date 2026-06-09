"""Tests for image --push helpers: extract_pushed_digest, write_image_digest, cmd_image.

Slice D1 (original): parse_repo_digest, write_image_digest, command construction.
Slice D2 (new):      extract_pushed_digest, new push path (podman --digestfile / docker stdout),
                     no inspect step, comment-header cosmetic fix.

TDD order (each RED→GREEN):
  D1-1:  parse_repo_digest — valid line → sha256 token
  D1-2:  parse_repo_digest — whitespace tolerance
  D1-3:  parse_repo_digest — list/bracket form Docker may emit
  D1-4:  parse_repo_digest — raises ValueError on garbage / missing / wrong-length hex
  D1-5:  write_image_digest — writes a grep-matchable file
  D1-6:  write_image_digest — round-trips through a temp path
  D1-7:  write_image_digest — raises ValueError on malformed digest
  D1-8:  command construction — build, push sequence (no inspect) via fake runner
  D2-1:  extract_pushed_digest — digestfile path (valid/garbage/uppercase/short → ValueError)
  D2-2:  extract_pushed_digest — docker-stdout path (parses digest:, raises on absent)
  D2-3:  extract_pushed_digest — digestfile takes precedence over stdout
  D2-4:  command construction — podman issues --digestfile; docker does not
  D2-5:  write_image_digest comment header contains {64} not {{64}}
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers imported under test — not yet defined; RED will fail here first.
# ---------------------------------------------------------------------------

from build_main import extract_pushed_digest, write_image_digest, cmd_image

# A valid 64-hex-char sha256 digest for reuse across tests.
_VALID_DIGEST = "sha256:" + "a" * 64
_FULL_IMAGE = "ghcr.io/coreyleavitt/v3ke-toolchain:v0.1.0"

_GREP_PAT = re.compile(r"^sha256:[0-9a-f]{64}$", re.MULTILINE)


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
    """cmd_image with --push issues build → push (no inspect) then writes IMAGE_DIGEST.

    The digest is captured from the push itself:
      - podman: via --digestfile <tmp>; the fake runner returns the bare digest as push stdout.
      - docker: from push stdout (``digest: sha256:<64hex> size: …``).
    No ``inspect`` command is issued.
    """

    def _run_cmd_image(self, runner, digest_path, **ns_kwargs):
        """Helper: invoke cmd_image with a fake runner and temp IMAGE_DIGEST path."""
        a = _make_namespace(**ns_kwargs)
        cmd_image(a, runner=runner, digest_path=digest_path)

    def _make_podman_runner(self, hex64="a" * 64):
        """Podman runner: push returns bare ``sha256:<hex>`` (simulates --digestfile read)."""
        push_stdout = f"sha256:{hex64}"
        outputs = {"push": push_stdout}
        return FakeRunner(outputs=outputs), hex64

    def _make_docker_runner(self, hex64="a" * 64):
        """Docker runner: push returns docker-style ``digest: sha256:<hex> size: 1234``."""
        push_stdout = f"latest: digest: sha256:{hex64} size: 1234"
        outputs = {"push": push_stdout}
        return FakeRunner(outputs=outputs), hex64

    # --- build command is always first ---

    def test_build_command_is_first(self, tmp_path):
        runner, _ = self._make_podman_runner()
        self._run_cmd_image(runner, tmp_path / "IMAGE_DIGEST")
        assert runner.calls[0][0] == "podman"
        assert "build" in runner.calls[0]

    def test_build_tags_image(self, tmp_path):
        runner, _ = self._make_podman_runner()
        self._run_cmd_image(runner, tmp_path / "IMAGE_DIGEST")
        build_cmd = runner.calls[0]
        assert "-t" in build_cmd
        assert _FULL_IMAGE in build_cmd

    # --- push command follows build ---

    def test_push_command_issued_after_build(self, tmp_path):
        runner, _ = self._make_podman_runner()
        self._run_cmd_image(runner, tmp_path / "IMAGE_DIGEST")
        push_cmd = runner.calls[1]
        assert "push" in push_cmd
        assert _FULL_IMAGE in push_cmd

    def test_push_uses_correct_runtime_podman(self, tmp_path):
        runner, _ = self._make_podman_runner()
        self._run_cmd_image(runner, tmp_path / "IMAGE_DIGEST", runtime="podman")
        push_cmd = runner.calls[1]
        assert push_cmd[0] == "podman"

    def test_push_uses_correct_runtime_docker(self, tmp_path):
        image = "ghcr.io/coreyleavitt/v3ke-toolchain:v0.2.0"
        runner, _ = self._make_docker_runner()
        self._run_cmd_image(
            runner, tmp_path / "IMAGE_DIGEST", runtime="docker", image=image
        )
        push_cmd = runner.calls[1]
        assert push_cmd[0] == "docker"

    # --- no inspect command ever issued ---

    def test_no_inspect_command_for_podman(self, tmp_path):
        runner, _ = self._make_podman_runner()
        self._run_cmd_image(runner, tmp_path / "IMAGE_DIGEST", runtime="podman")
        commands = [" ".join(str(x) for x in c) for c in runner.calls]
        assert not any("inspect" in c for c in commands), (
            f"inspect must NOT be called; commands: {commands}"
        )

    def test_no_inspect_command_for_docker(self, tmp_path):
        image = "ghcr.io/coreyleavitt/v3ke-toolchain:v0.2.0"
        runner, _ = self._make_docker_runner()
        self._run_cmd_image(
            runner, tmp_path / "IMAGE_DIGEST", runtime="docker", image=image
        )
        commands = [" ".join(str(x) for x in c) for c in runner.calls]
        assert not any("inspect" in c for c in commands), (
            f"inspect must NOT be called; commands: {commands}"
        )

    # --- IMAGE_DIGEST written after push ---

    def test_image_digest_file_written_podman(self, tmp_path):
        path = tmp_path / "IMAGE_DIGEST"
        runner, _ = self._make_podman_runner()
        self._run_cmd_image(runner, path)
        assert path.exists(), "IMAGE_DIGEST must be written after push"

    def test_image_digest_file_contains_correct_digest_podman(self, tmp_path):
        path = tmp_path / "IMAGE_DIGEST"
        hex64 = "b" * 64
        runner, _ = self._make_podman_runner(hex64=hex64)
        self._run_cmd_image(runner, path)
        text = path.read_text()
        matches = _GREP_PAT.findall(text)
        assert matches == [f"sha256:{hex64}"]

    def test_image_digest_file_contains_correct_digest_docker(self, tmp_path):
        path = tmp_path / "IMAGE_DIGEST"
        image = "ghcr.io/coreyleavitt/v3ke-toolchain:v0.2.0"
        hex64 = "c" * 64
        runner, _ = self._make_docker_runner(hex64=hex64)
        self._run_cmd_image(runner, path, runtime="docker", image=image)
        text = path.read_text()
        matches = _GREP_PAT.findall(text)
        assert matches == [f"sha256:{hex64}"]

    def test_exactly_two_commands_issued_podman(self, tmp_path):
        runner, _ = self._make_podman_runner()
        self._run_cmd_image(runner, tmp_path / "IMAGE_DIGEST")
        assert len(runner.calls) == 2, (
            f"Expected 2 commands (build, push); got {len(runner.calls)}: {runner.calls}"
        )

    def test_exactly_two_commands_issued_docker(self, tmp_path):
        image = "ghcr.io/coreyleavitt/v3ke-toolchain:v0.2.0"
        runner, _ = self._make_docker_runner()
        self._run_cmd_image(runner, tmp_path / "IMAGE_DIGEST", runtime="docker", image=image)
        assert len(runner.calls) == 2, (
            f"Expected 2 commands (build, push); got {len(runner.calls)}: {runner.calls}"
        )

    # --- no push when --push not set ---

    def test_no_push_flag_skips_push(self, tmp_path):
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
# D2-1 to D2-3: extract_pushed_digest
# ===========================================================================


class TestExtractPushedDigest:
    """extract_pushed_digest(push_stdout, digestfile_text=None) -> str.

    D2-1: digestfile path — valid, garbage, uppercase, short all handled.
    D2-2: docker-stdout path — parses 'digest: sha256:…', raises on absent.
    D2-3: digestfile takes precedence over stdout.
    """

    # --- D2-1: digestfile path ---

    def test_digestfile_valid_bare_digest(self):
        digest = "sha256:" + "a" * 64
        result = extract_pushed_digest("ignored stdout", digestfile_text=digest)
        assert result == digest

    def test_digestfile_with_trailing_newline(self):
        digest = "sha256:" + "b" * 64
        result = extract_pushed_digest("", digestfile_text=digest + "\n")
        assert result == digest

    def test_digestfile_garbage_raises(self):
        with pytest.raises(ValueError, match="sha256"):
            extract_pushed_digest("", digestfile_text="not-a-digest")

    def test_digestfile_uppercase_hex_raises(self):
        with pytest.raises(ValueError):
            extract_pushed_digest("", digestfile_text="sha256:" + "A" * 64)

    def test_digestfile_short_hex_raises(self):
        with pytest.raises(ValueError):
            extract_pushed_digest("", digestfile_text="sha256:" + "a" * 63)

    def test_digestfile_empty_string_falls_through_to_stdout(self):
        """Empty digestfile_text means 'not provided'; must fall through to stdout path."""
        hex64 = "d" * 64
        stdout = f"latest: digest: sha256:{hex64} size: 9999"
        result = extract_pushed_digest(stdout, digestfile_text="")
        assert result == f"sha256:{hex64}"

    def test_digestfile_none_falls_through_to_stdout(self):
        """digestfile_text=None also falls through to stdout path."""
        hex64 = "e" * 64
        stdout = f"digest: sha256:{hex64}"
        result = extract_pushed_digest(stdout, digestfile_text=None)
        assert result == f"sha256:{hex64}"

    # --- D2-2: docker-stdout path ---

    def test_stdout_parses_docker_digest_line(self):
        hex64 = "f" * 64
        stdout = f"v0.1.0: digest: sha256:{hex64} size: 4321"
        result = extract_pushed_digest(stdout)
        assert result == f"sha256:{hex64}"

    def test_stdout_parses_bare_sha256_in_output(self):
        """Any sha256:<64hex> token in stdout is accepted."""
        hex64 = "1" * 64
        stdout = f"sha256:{hex64}"
        result = extract_pushed_digest(stdout)
        assert result == f"sha256:{hex64}"

    def test_stdout_tolerates_surrounding_text(self):
        hex64 = "2" * 64
        stdout = f"Pushing image…\nlatest: digest: sha256:{hex64} size: 1234\nDone."
        result = extract_pushed_digest(stdout)
        assert result == f"sha256:{hex64}"

    def test_stdout_raises_when_no_digest_present(self):
        with pytest.raises(ValueError, match="sha256"):
            extract_pushed_digest("Pushing…\nPush complete.\n")

    def test_stdout_raises_on_empty_string(self):
        with pytest.raises(ValueError):
            extract_pushed_digest("")

    def test_stdout_raises_on_uppercase_hex(self):
        """Uppercase hex in stdout is not valid."""
        with pytest.raises(ValueError):
            extract_pushed_digest(f"digest: sha256:{'A' * 64}")

    # --- D2-3: digestfile takes precedence over stdout ---

    def test_digestfile_wins_over_stdout(self):
        """When digestfile_text is non-empty and valid, stdout is ignored."""
        df_hex = "a" * 64
        stdout_hex = "b" * 64
        result = extract_pushed_digest(
            f"digest: sha256:{stdout_hex}",
            digestfile_text=f"sha256:{df_hex}",
        )
        assert result == f"sha256:{df_hex}"


# ===========================================================================
# D2-4: podman uses --digestfile; docker does not
# ===========================================================================


class TestPushCommandDigestCapture:
    """Verify --digestfile presence/absence in the push command per runtime.

    These tests use a FakeRunner that captures commands and returns a
    controlled push stdout so cmd_image can complete end-to-end.
    """

    def _run(self, runner, tmp_path, runtime, image=_FULL_IMAGE):
        import types
        a = types.SimpleNamespace(
            runtime=runtime,
            image=image,
            ctng_version=None,
            push=True,
        )
        cmd_image(a, runner=runner, digest_path=tmp_path / "IMAGE_DIGEST")

    def _podman_runner(self, hex64="a" * 64):
        """Returns push stdout = bare digest (simulates --digestfile content being read)."""
        return FakeRunner(outputs={"push": f"sha256:{hex64}"}), hex64

    def _docker_runner(self, hex64="a" * 64):
        """Returns docker-style push stdout with digest line."""
        return FakeRunner(outputs={"push": f"digest: sha256:{hex64} size: 123"}), hex64

    def test_podman_push_includes_digestfile_flag(self, tmp_path):
        runner, _ = self._podman_runner()
        self._run(runner, tmp_path, "podman")
        push_cmd = runner.calls[1]
        assert "--digestfile" in push_cmd, (
            f"podman push must include --digestfile; cmd: {push_cmd}"
        )

    def test_podman_push_digestfile_path_is_string(self, tmp_path):
        runner, _ = self._podman_runner()
        self._run(runner, tmp_path, "podman")
        push_cmd = runner.calls[1]
        idx = push_cmd.index("--digestfile")
        digestfile_arg = push_cmd[idx + 1]
        assert isinstance(digestfile_arg, str) and len(digestfile_arg) > 0, (
            f"--digestfile must be followed by a non-empty path; got: {digestfile_arg!r}"
        )

    def test_docker_push_excludes_digestfile_flag(self, tmp_path):
        image = "ghcr.io/coreyleavitt/v3ke-toolchain:v0.2.0"
        runner, _ = self._docker_runner()
        self._run(runner, tmp_path, "docker", image=image)
        push_cmd = runner.calls[1]
        assert "--digestfile" not in push_cmd, (
            f"docker push must NOT include --digestfile; cmd: {push_cmd}"
        )

    def test_podman_digest_written_from_push_stdout(self, tmp_path):
        hex64 = "9" * 64
        runner, _ = self._podman_runner(hex64=hex64)
        self._run(runner, tmp_path, "podman")
        text = (tmp_path / "IMAGE_DIGEST").read_text()
        assert f"sha256:{hex64}" in text

    def test_docker_digest_written_from_push_stdout(self, tmp_path):
        image = "ghcr.io/coreyleavitt/v3ke-toolchain:v0.2.0"
        hex64 = "8" * 64
        runner, _ = self._docker_runner(hex64=hex64)
        self._run(runner, tmp_path, "docker", image=image)
        text = (tmp_path / "IMAGE_DIGEST").read_text()
        assert f"sha256:{hex64}" in text


# ===========================================================================
# D2-5: comment header cosmetic fix — {64} not {{64}}
# ===========================================================================


class TestWriteImageDigestCommentHeader:
    """The comment header must contain literal {64} so the grep example is correct."""

    def test_comment_contains_literal_brace_64(self, tmp_path):
        """Emitted header must show grep -E '^sha256:[0-9a-f]{64}$', not {{64}}."""
        path = tmp_path / "IMAGE_DIGEST"
        write_image_digest(path, _VALID_DIGEST)
        text = path.read_text()
        assert "{64}" in text, (
            "Comment header must contain literal '{64}', got:\n" + text
        )

    def test_comment_does_not_contain_double_brace(self, tmp_path):
        """Confirm {{64}} is NOT present — that was the escaping artifact."""
        path = tmp_path / "IMAGE_DIGEST"
        write_image_digest(path, _VALID_DIGEST)
        text = path.read_text()
        assert "{{64}}" not in text, (
            "Comment header must NOT contain '{{64}}' (str.format artifact); got:\n" + text
        )


# ===========================================================================
# Integration marker — real network / runtime (excluded by default)
# ===========================================================================


@pytest.mark.integration
def test_image_push_integration_real_runtime():
    """Real push: requires a logged-in runtime and network. Excluded by default."""
    pytest.skip("integration test — run explicitly with -m integration")
