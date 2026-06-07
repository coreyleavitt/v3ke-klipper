#!/usr/bin/env bash
# scripts/repro-check.sh — local reproducibility verification for B4.
#
# Builds the four device artifacts TWICE using the real build pipeline
# (tools/build.py artifacts), captures the sha256 of each artifact after
# each build, and asserts that both sets are byte-identical.
#
# On mismatch: prints which artifact diverged, runs diffoscope (if available)
# on the differing pair, and exits 1.
# On full match: prints the 4 matching sha256 digests and exits 0.
#
# This is the RUNNABLE LOCAL PROOF for B4.  The CI repro job runs the same
# logic on two independent GitHub Actions runners pulling the same image digest.
#
# Usage:
#   ./scripts/repro-check.sh [OPTIONS]
#
#   Options:
#     --runtime RUNTIME   Container runtime: podman (default) or docker.
#     --image IMAGE       Toolchain image name/ref (default: v3ke-toolchain).
#                         Pass a digest-pinned ref for CI:
#                           ghcr.io/coreyleavitt/v3ke-toolchain@sha256:<hex>
#     -h, --help          Show this help and exit.
#
# Environment:
#   RUNTIME               Same as --runtime (overridden by the flag).
#   IMAGE                 Same as --image (overridden by the flag).
#
# Requirements:
#   - The toolchain image must be present locally (pulled or built).
#   - The four artifacts produced by the build are:
#       external/katapult/out/katapult.bin
#       mcu-firmware/klipper.bin
#       external/klipper/klippy/chelper/c_helper.so
#       external/klipper/out/klipper.elf
#   - diffoscope is optional: used for detailed mismatch diagnosis when available.
#
# B4 design note (cache isolation):
#   Each build invocation runs in a fresh container (--rm).  The Python build
#   module starts with make clean for each subproject, so no object-file cache
#   can survive from the first build to the second.  No ccache or buildx cache
#   mounts are passed.  This ensures the second build is a true cold rebuild,
#   not a cache hit masquerading as reproducibility.

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults (overridable via flags or env)
# ---------------------------------------------------------------------------
RUNTIME="${RUNTIME:-podman}"
IMAGE="${IMAGE:-v3ke-toolchain}"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
usage() {
    sed -n '3,35p' "$0" | sed 's/^# \?//'
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --runtime)   RUNTIME="$2"; shift 2 ;;
        --runtime=*) RUNTIME="${1#*=}"; shift ;;
        --image)     IMAGE="$2"; shift 2 ;;
        --image=*)   IMAGE="${1#*=}"; shift ;;
        -h|--help)   usage ;;
        *) echo "Unknown option: $1" >&2; usage ;;
    esac
done

# ---------------------------------------------------------------------------
# Locate repo root (script lives in scripts/, two levels up from tools/)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TOOLS_DIR="$REPO_ROOT/tools"

# ---------------------------------------------------------------------------
# Canonical artifact paths (relative to REPO_ROOT)
# ---------------------------------------------------------------------------
# These are the four artifacts produced by build.py artifacts / build.orchestrate.
# Must stay in sync with orchestrate.py's step sequence.
ARTIFACTS=(
    "external/katapult/out/katapult.bin"
    "mcu-firmware/klipper.bin"
    "external/klipper/klippy/chelper/c_helper.so"
    "external/klipper/out/klipper.elf"
)

# ---------------------------------------------------------------------------
# Staging directories (inside repo_root so the bind mount reaches them)
# ---------------------------------------------------------------------------
STAGE_DIR="$REPO_ROOT/.repro-check"
STAGE1="$STAGE_DIR/build1"
STAGE2="$STAGE_DIR/build2"
SHA1_FILE="$STAGE_DIR/sha256.build1"
SHA2_FILE="$STAGE_DIR/sha256.build2"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { echo "[repro-check] $*"; }
warn() { echo "[repro-check] WARN: $*" >&2; }
fail() { echo "[repro-check] ERROR: $*" >&2; exit 1; }

run_build() {
    local label="$1"
    log "--- Build $label starting ---"
    log "  runtime : $RUNTIME"
    log "  image   : $IMAGE"

    # Drive the build through the real pipeline (build.py artifacts).
    # --runtime and --image pass through so CI can use docker + a digest ref.
    # No cache mounts are passed; each run is a fresh container (--rm).
    python3 "$TOOLS_DIR/build.py" \
        --runtime "$RUNTIME" \
        --image   "$IMAGE"   \
        artifacts

    log "--- Build $label complete ---"
}

capture_artifacts() {
    local stage_dir="$1"
    local label="$2"

    mkdir -p "$stage_dir"
    for rel in "${ARTIFACTS[@]}"; do
        src="$REPO_ROOT/$rel"
        if [[ ! -f "$src" ]]; then
            fail "Build $label: artifact not found: $src"
        fi
        dst="$stage_dir/$(basename "$rel")"
        cp "$src" "$dst"
    done
    log "Captured artifacts for build $label into $stage_dir"
}

write_sha256() {
    local stage_dir="$1"
    local sha_file="$2"

    # Compute sha256 of each captured artifact in a stable, basename-keyed order.
    # Output format (one line per artifact): <sha256hex>  <basename>
    : > "$sha_file"
    for rel in "${ARTIFACTS[@]}"; do
        fname="$(basename "$rel")"
        fpath="$stage_dir/$fname"
        sha256sum "$fpath" | awk "{print \$1 \"  $fname\"}" >> "$sha_file"
    done
    log "sha256 manifest written: $sha_file"
    cat "$sha_file"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    log "========================================"
    log "B4 reproducibility check"
    log "  RUNTIME : $RUNTIME"
    log "  IMAGE   : $IMAGE"
    log "  REPO    : $REPO_ROOT"
    log "========================================"

    # Clean any previous staging area to avoid false positives.
    rm -rf "$STAGE_DIR"
    mkdir -p "$STAGE_DIR"

    # ---- Build 1 --------------------------------------------------------
    log ""
    log "=== BUILD 1 OF 2 ==="
    run_build "1"
    capture_artifacts "$STAGE1" "1"
    write_sha256 "$STAGE1" "$SHA1_FILE"

    # ---- Build 2 --------------------------------------------------------
    log ""
    log "=== BUILD 2 OF 2 ==="
    run_build "2"
    capture_artifacts "$STAGE2" "2"
    write_sha256 "$STAGE2" "$SHA2_FILE"

    # ---- Compare --------------------------------------------------------
    log ""
    log "=== COMPARING sha256 MANIFESTS ==="
    log "Build 1:"
    cat "$SHA1_FILE"
    log "Build 2:"
    cat "$SHA2_FILE"

    if diff -u "$SHA1_FILE" "$SHA2_FILE" > /dev/null 2>&1; then
        log ""
        log "RESULT: MATCH — all 4 artifacts are byte-identical across both builds."
        log ""
        log "Artifacts (sha256):"
        cat "$SHA1_FILE"
        log ""
        log "Reproducibility PROVEN."
        exit 0
    fi

    # Mismatch — identify the diverging artifacts and optionally run diffoscope.
    echo "" >&2
    echo "[repro-check] RESULT: MISMATCH — the following artifacts diverged:" >&2
    echo "" >&2

    mismatched=()
    for rel in "${ARTIFACTS[@]}"; do
        fname="$(basename "$rel")"
        sha1="$(grep "  $fname$" "$SHA1_FILE" | awk '{print $1}')"
        sha2="$(grep "  $fname$" "$SHA2_FILE" | awk '{print $1}')"
        if [[ "$sha1" != "$sha2" ]]; then
            echo "  MISMATCH: $fname" >&2
            echo "    build1: $sha1" >&2
            echo "    build2: $sha2" >&2
            mismatched+=("$fname")
        else
            echo "  OK      : $fname  ($sha1)" >&2
        fi
    done

    echo "" >&2
    echo "[repro-check] ${#mismatched[@]} artifact(s) diverged: ${mismatched[*]}" >&2
    echo "" >&2

    # Run diffoscope on each mismatched pair (if available).
    if command -v diffoscope > /dev/null 2>&1; then
        echo "[repro-check] Running diffoscope on mismatched artifact(s)..." >&2
        for fname in "${mismatched[@]}"; do
            f1="$STAGE1/$fname"
            f2="$STAGE2/$fname"
            echo "--- diffoscope: $fname ---" >&2
            diffoscope "$f1" "$f2" >&2 || true
        done
    else
        warn "diffoscope is not installed — skipping detailed diff."
        warn "Install with: pip install diffoscope  OR  apt install diffoscope"
        warn "To manually inspect a mismatch:"
        for fname in "${mismatched[@]}"; do
            warn "  diffoscope $STAGE1/$fname $STAGE2/$fname"
        done
        warn ""
        warn "Common causes of non-determinism (see RFC §6 / §3 A3/A4):"
        warn "  - SOURCE_DATE_EPOCH not set → timestamps in ELF debug info"
        warn "  - Missing -ffile-prefix-map / -fdebug-prefix-map → embedded paths differ"
        warn "  - Klipper cleanbuild=False → buildcommands.py embeds strftime+hostname"
        warn "  - Non-deterministic ar/ranlib → archive member ordering"
    fi

    exit 1
}

main "$@"
