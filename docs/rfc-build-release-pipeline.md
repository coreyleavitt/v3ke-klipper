# RFC: Reproducible build & release pipeline

**Status:** Draft (rfc-flow Stage 2 — architecture review, round 2 applied)
**Scope:** Architect candidates #1 (shell→Python build collapse + ABI-checker unification),
#5 (test harness), #3 (CI + reproducibility proof), #4 (release packaging + versioning).
**Out of scope (separate RFCs):** #2 (host-swap / rollback / DEPLOY.md — the device-deployment
path), #6 (load-cell PRTouch Z-homing — Klipper-config/hardware bringup).

---

## 1. Motivation

The fork already replaced the *outer* build layer with a Python orchestrator (`tools/build.py`)
and the *hardware-ops* layer with a Nim single-binary CLI (`tools/v3ke/`). But the **middle build
layer is still bash**, and three things follow from that:

1. **A bash seam the Python builder can't see into.** `build.py artifacts` fires
   `bash -c "./build-bootloader-mcu-and-host-firmware.sh && ./verify-artifacts.sh"`. Those scripts
   in turn call `build-chelper.sh` + `build-klipper-host-mcu.sh`. `build.py` can't report per-step
   status, can't be unit-tested, and can't be driven step-by-step. The tell: `build-chelper.sh`
   already **embeds a `python3` `ast` heredoc** to parse klipper's `SOURCE_FILES` — a bash script
   doing its real work in Python wants to *be* Python.

2. **Three ABI checkers, kept in sync by a comment.** `verify-artifacts.sh` (readelf + grep),
   `tools/v3ke/verify.nim` (native ELF parser), and `klipper/read-elf-infos.sh` (diagnostic) are
   three implementations of "inspect a MIPS ELF and check the device ABI." The CI gate runs the
   *weakest* one (shell). There is no single authority — and none of the three checks the
   **FP ABI** at all, so a binary with the wrong floating-point ABI passes every existing gate. The
   subtlety (confirmed against the real on-device binaries, both `e_flags=0x70001407`): the legacy
   `EF_MIPS_FP64` bit in `e_flags` is **not emitted when fp64 is the toolchain default**, so checking
   that bit is vacuous. The authoritative FP ABI lives in the **`.MIPS.abiflags` section** (`fp_abi`
   byte). None of the three readers parses section headers — they read program headers only — so the
   FP ABI is structurally invisible to all of them (see §3 A1).

3. **The core value — reproducibility — is asserted, never verified.** Nothing builds twice and
   compares hashes; the container base is an unpinned rolling tag (`opensuse/tumbleweed:latest`)
   whose `zypper dup` re-floats every package on each build; cache mounts could mask
   non-determinism. There is **no CI at all**, **no release artifact** (the project's premise is
   "most users just take the release zip with binaries" — that zip does not exist), **no
   versioning** (`v3ke.nimble` is hardcoded `0.1.0`, no git tags, no provenance), and **zero
   automated tests** (the pure ELF reader, `shQuote`, `checkOcdPath`, the config-assert logic — all
   unverified except by ad-hoc review).

This RFC makes the build **authoritative** (one Python module, no bash seam), **tested**,
**reproducibility-verified** (CI proves byte-identical rebuilds *across independent environments*),
and **shippable** (a versioned, checksummed, license-complete release zip).

## 2. Goals / Non-goals

**Goals**
- G1. One Python build module is the single authority for producing all four artifacts
  (`katapult.bin`, `klipper.bin`, `c_helper.so`, `klipper_mcu.elf`), runnable inside the container,
  with per-step **structured** status (`StepResult`) and no bash build scripts.
- G2. One ABI specification (a checked-in golden-fixture set + an expected-ABI constant table under
  `tools/abi/`) that **both** the Python build-side checker and the Nim operator-side checker are
  tested against — eliminating silent drift. The check covers **machine, endianness (ELFDATA2LSB),
  nan2008, o32, mips32r2 (all from `e_flags`/`e_ident`), the FP ABI (`fp_abi` from the
  `.MIPS.abiflags` section, not the vacuous `e_flags` bit), and the loader**. The accepted `fp_abi`
  value set is a named constant per artifact kind (see §3 A1 / open question). Delete
  `verify-artifacts.sh` and `read-elf-infos.sh`.
- G3. A real test harness: `pytest` for the Python, `nimble test` for the Nim, both runnable in the
  lightweight `v3ke-dev` image (**not** the cross-toolchain image) locally and in CI; pure functions (ELF reader, ABI check, source parse,
  `shQuote`, `checkOcdPath`, config-assert) covered with golden/property tests, **and** the
  subprocess-orchestrating code (Python `artifacts.py`, Nim `flash.nim`/`sshdev.nim`) made testable
  via an injectable runner seam.
- G4. CI that runs the build + tests on every push, **proves reproducibility** by building twice on
  **two independent runner instances pulling the same image digest** (cache disabled) and asserting
  byte-identical artifacts, and pins the container base coherently.
- G5. A `release` command + CI release job that emits a versioned zip (firmware + host artifacts +
  config files + the `v3ke` binary + `INSTALL.md` + `LICENSES/`) with a schema-versioned,
  checksummed, provenance-bearing `manifest.json`, plus a cosign-signed `SHA256SUMS` that **covers
  `manifest.json` itself** (so provenance is signed, not just artifact bytes), attached to a GitHub
  Release on tag.

**Non-goals**
- The live host-side migration (`v3ke host-swap`, init.d swap, rollback, `DEPLOY.md`) — RFC #2.
  *(This RFC's release zip ships an `INSTALL.md` stub that points forward to RFC #2; see §3 C.)*
- Load-cell PRTouch Z-homing / printer.cfg probe swap — RFC #6.
- Re-enabling Creality's `S14mcu_update`, touchscreen/KlipperScreen integration.
- Changing the toolchain ABI, pins, or the crosstool-ng design (settled; this RFC consumes it).
  *(B1's base-image fix is a build-input pin, not an ABI change.)*

## 3. Design

Three stages, each a clean boundary; the sequence is load-bearing (you can't CI-verify or package
a build that's still scattered across bash). A `tools/abi/` directory is introduced first as the
language-neutral home for the shared ABI ground truth (fixtures + constant table), referenced by
both the Python and Nim checkers.

### Stage A — The Python build module (collapse + unify)

Introduce a Python package (`tools/build/`; `tools/build.py` becomes its thin CLI entry). The split
separates **command construction (pure) from command execution (I/O)** so the testable parts have
no side effects, and keeps each module *deep* (a small interface over real logic) rather than a
one-function file:

- `tools/abi/abi_spec.py` — the **shared spec**: a **table-driven** `DEVICE_ABI` — a list of
  `(field_name, e_flags_mask, expected_value)` rows for the `e_flags` checks (nan2008 / o32 /
  mips32r2), plus `EM_MIPS`, `ELFDATA2LSB`, the loader name, and the **accepted `fp_abi` set per
  `ArtifactKind`** (the FP ABI read from `.MIPS.abiflags`, not `e_flags`). Table-driven so adding a
  7th flag is a one-row edit, not a new `if` in two languages. `tools/abi/fixtures/` holds the
  golden ELFs (§G2). This directory is the single source of truth both language checkers test against.
- `build/elf.py` — **pure**: `inspect_elf(data: bytes) -> ElfInfo` and
  `check_abi(info: ElfInfo, kind: ArtifactKind) -> AbiResult`. `inspect_elf` parses **both program
  headers (for `PT_INTERP`) and section headers (to locate `.MIPS.abiflags`, `sh_type ==
  0x7000002A`, and read its `fp_abi`/`cpr1_size` bytes)** — the prior readers' PHDR-only parse is
  why the FP ABI was invisible. `ElfInfo` therefore carries `fp_abi` and `cpr1_size` alongside
  `machine`, `data` (endianness), `flags`, `etype`, `interp`. `check_abi` walks the `abi_spec` table
  plus the `fp_abi`/loader checks. `AbiResult` is a frozen dataclass with a tuple of violations and
  an `.ok` property, plus an `.applicable: bool` (false for `RAW_FIRMWARE`, see below) — **not** a
  list of human strings; a `format_violations()` in the CLI layer renders them. Violations are
  structured and **typed for machine use**: `AbiViolation(field: str, expected: int, actual: int)`
  for the bitmask/enum checks (hex rendering pushed to the CLI), and a separate
  `LoaderViolation(expected_suffix: str, actual: str)` for the categorically-different PT_INTERP
  string check. `ArtifactKind` has **three** values — `SHARED_LIBRARY` (DYN, no loader, MIPS ABI
  checked), `EXECUTABLE` (EXEC, loader required, MIPS ABI checked), and `RAW_FIRMWARE` (the ARM
  `katapult.bin`/`klipper.bin` — not ELFs; `check_abi` returns `AbiResult(applicable=False)` rather
  than raising "not an ELF"). This is the build-side ABI authority, pinned to `verify.nim` by G2.
- `build/arm_mcu.py` — **pure**: the **ARM MCU firmware** boundary — katapult + Klipper-MCU `make`
  command builders (their kconfig paths, clean/olddefconfig/build sequences, ARM flags). Both
  produce raw `.bin`s flashed over SWD; grouping them reflects the real architectural seam (ARM
  toolchain → raw firmware).
- `build/host.py` — **pure**: the **MIPS host artifacts** boundary — `chelper_sources(init_py) ->
  list[str]` (the `ast` extraction, a static-analysis function cohesive with the host build that
  consumes it) plus the `c_helper.so` gcc command builder (with the MIPS ABI flags `-mips32r2
  -mabi=32 -mhard-float -mfp64 -mnan=2008`) and the `klipper_mcu.elf` cross-`make` builder
  (`CROSS_PREFIX`). Every function here touches the cross-build, so the A4 ABI-flag test is
  self-contained. *(This replaces the round-1 `klipper.py`/`mcu.py` split, which cut on the wrong
  axis — both held `make` builders, and `chelper_sources` (static analysis) did not cohere with
  command construction. The real boundary is ARM-firmware vs. MIPS-host, not "klipper vs. mcu".)*
- `build/artifacts.py` — **I/O runner**: pure builders return a typed `BuildStep(name, cmd:
  list[str], output_path: Path, kind: ArtifactKind)`; `artifacts.py` executes them via an injectable
  `runner: Callable[[list[str]], RunResult]` where `RunResult(returncode, stdout, stderr, elapsed)`
  is the **thin** seam — the runner only knows "ran a command, here's the exit/output/timing". The
  default is a named `subprocess_runner` wrapper (not `subprocess.run` directly — that returns a
  `CompletedProcess`, not a `RunResult`). `artifacts.py` assembles the `StepResult(name, ok,
  duration, abi: Optional[AbiResult], detail)` from the `RunResult` + the `BuildStep` + a guarded
  `check_abi(read(output_path), kind)` (only when the step succeeded **and** `kind != RAW_FIRMWARE`
  **and** the output exists). Execution is **fail-fast**: a failed step raises, and the returned
  `list[StepResult]` holds the steps completed up to (and including) the failure. A `FakeRunner`
  that returns `RunResult(0, b"", b"", 0.0)` drives unit tests with no podman/make and no fake
  StepResult fields to construct. Per-artifact **sha256 for the manifest is computed separately** by
  the release path (walking `BuildStep.output_path`s) — it is *not* a `StepResult` field, keeping
  `StepResult` from becoming a kitchen sink.
- `build.py` (CLI) — `image` / `snapshot` / `artifacts` / `release`; thin dispatch + a
  `format_violations`/`StepResult` renderer. `image` and `snapshot` **remain thin podman/subprocess
  calls in the CLI layer** — they are not ported to `tools/build/` module logic (they orchestrate
  the container, not artifact construction). The `all` alias is **removed** (it would either
  silently exclude `release` or wrongly include a tag-requiring step); the canonical sequence is
  documented in `--help`.

**Determinism is proactive, not reactive.** The command builders set `SOURCE_DATE_EPOCH`,
`-ffile-prefix-map`/`-fdebug-prefix-map` (and deterministic `ar`/sort where any archiving occurs)
**from the start** in A3/A4 — not as an after-the-fact patch once a diff shows up. **The
`SOURCE_DATE_EPOCH` value is the commit time** (`git -C /work log -1 --format=%ct HEAD`, resolved
once **inside the container** against the mounted repo and threaded through every make/gcc
invocation) — *not* the wall clock, which would make B4 diverge every run. Additionally, Klipper's
`scripts/buildcommands.py` embeds a live `strftime` timestamp + `gethostname()` into
`klipper_mcu.elf` whenever `cleanbuild` is false (dirty submodule, or unreadable tool versions); A4
asserts `cleanbuild` held (`strings klipper_mcu.elf` carries no `YYYYMMDD_HHMMSS` suffix), and this
is a documented B4 failure mode. The MIPS host-MCU and ARM-MCU builds both invoke `make` inside
`external/klipper/`; `artifacts.py` runs them **sequentially** (or against separate build dirs) —
parallelizing them races on `external/klipper/out/`.

Deleted at the end of Stage A: `build-bootloader-mcu-and-host-firmware.sh`,
`klipper/c_helper/build-chelper.sh`, `klipper/klipper_host_mcu/build-klipper-host-mcu.sh`,
`verify-artifacts.sh`, `klipper/read-elf-infos.sh`. The Nim side is de-tangled **early** (in A1c, not
A5b): `common.nim`'s pure ELF reader splits into `tools/v3ke/elf.nim` (gaining `.MIPS.abiflags`
parsing) with `verify.nim` re-pointed to `import elf`, leaving terminal-output helpers behind, so
`nimble test` can exercise the reader without styled-output side effects. A5b is then only the bash
deletions.

**The Python/Nim duplication is deliberate and bounded.** `verify.nim` must stay — it ships in the
release zip and runs on a bare operator machine with no readelf/Python. So the ABI check exists in
two languages *by deployment necessity* (build/CI side = Python; operator side = Nim). G2's
mechanism prevents drift: the **`tools/abi/` golden fixtures** (a known-good ELF plus a
**per-flag** set of known-bad ELFs — one wrong in exactly machine / endianness / nan2008 / o32 /
mips32r2 / fp_abi / loader, where the bad-fp_abi fixture has `fp_abi=1` (legacy double) or `5`
(FPXX) in its `.MIPS.abiflags`) are the shared observable truth; the Python test (`elf.py`) and the
Nim test (`elf.nim`) both assert against the *same fixture files*, so both checkers are pinned to the
same behavior on every flag, not just a single good/bad pair. Both readers are **table-driven** over
the same `(field, mask, expected)` shape, so the parallel edit surface for a new flag stays visibly
isomorphic and minimal (one `abi_spec` row + one Nim tuple + one fixture). *(O1 resolved in round 1:
shelling out to the Nim binary would pull Nim into the build image and couple build checks to the
operator binary's release cadence; generating one language from the other over-engineers a 30-line
reader. Golden-pinned double stands.)*

### Stage B — CI + reproducibility proof

- **Base-image coherence (B1).** Pinning `FROM …@sha256:` while `zypper dup` re-floats every
  package on each build is incoherent — the dup defeats the pin. Resolution: **drop the
  image-layer reproducibility claim** and make the from-source toolchain the sole reproducibility
  anchor. Concretely, pin `FROM` to a digest *and* document that the image is a *cache*, not a
  proof; the reproducibility job (B4) rebuilds the toolchain **from source in a fresh environment**
  and that — not the base pin — is what proves determinism. (Optionally pin a Tumbleweed *snapshot*
  tag to keep `zypper dup` honest; not required for the proof.)
- **Toolchain image publish (B0).** A dedicated CI workflow builds the toolchain image and pushes
  it to `ghcr.io` on Containerfile/toolchain change (and manual dispatch), so artifact CI pulls it
  instead of rebuilding the 20–40 min toolchain every run. Requires `permissions: packages: write`.
  B0 **emits the pushed image's `sha256` digest** as a workflow output / commits it to a digest file;
  the build & repro jobs pull **by digest, never by mutable tag** (a tag could be re-pushed under the
  same name, breaking both the repro story and the supply-chain pin). The image is published
  **publicly readable** so a contributor can `podman pull` it without CI credentials. The published
  image is a **cache**; its trust is bounded by B4 rebuilding from source.
- **Trigger matrix.** Which workflow fires on what is explicit:
  | job | PR | push→main | tag `v*` | manual |
  |-----|----|-----------|----------|--------|
  | test (pytest+nimble) | ✓ | ✓ | ✓ | ✓ |
  | build artifacts + ABI verify | ✓ | ✓ | ✓ | ✓ |
  | reproducibility (2× build) | — | ✓ | ✓ | ✓ |
  | toolchain-image publish (B0) | — | on toolchain change | — | ✓ |
  | release (C3) | — | — | ✓ | — |
  The 2× repro job is kept off per-PR (expensive) and runs on main-push + tags; a contributor can
  trigger it manually. Each workflow sets `concurrency: {group: <wf>-<ref>, cancel-in-progress:
  true}` for build/repro (rapid pushes shouldn't queue 20–40 min jobs); the test job does not cancel.
- **Submodule integrity gate (test/build job).** A step asserts `git submodule status --recursive`
  shows no `+`/`-` prefix (no out-of-sync or uninitialized submodule) and that each submodule HEAD
  matches the committed gitlink — so a stray `--remote` bump or a force-pushed upstream tag can't
  silently swap pinned source. (The cosign signature covers the sums; this covers the *inputs*.)
- **GitHub Actions** (each job declares least-privilege `permissions:`):
  - **test job** — `pytest` (Python) + `nimble test` (Nim) in the lightweight `v3ke-dev` image
    (nim + gcc + uv + python; *not* the cross-toolchain image), on every push/PR. Fast.
  - **build job** — pull the ghcr toolchain image **by digest**, `build.py artifacts`, run the
    Python ABI verify. **Fallback:** if the digest pull 404s/errors (B0 not yet run, ghcr outage),
    the job builds the image inline (`build.py image`) rather than hard-failing every run — slow but
    self-healing.
  - **reproducibility job** — build the artifacts **twice on two independent runner instances**
    (GitHub Actions `strategy.matrix`), both pulling the **same image digest**, both at the same
    commit, build cache disabled; assert byte-identical sha256 for all four; on mismatch, surface a
    `diffoscope` diff. *(Only this two-runner/same-digest form — **not** a fresh-from-source image
    rebuild vs. the cache: that would re-float Tumbleweed packages via `zypper dup` and diverge on
    package-release days, testing image reproducibility, not artifact reproducibility, and
    contradicting B1. Image-reproducibility, if ever wanted, is a separate named job.)* This is the
    actual proof the reproducibility claim has lacked.
- Determinism hardening lives in A3/A4 (proactive). **B5 shrinks to a residual-investigation
  slice**: only if B4 still diverges after the proactive flags, scope the specific remaining source.

### Stage C — Release packaging + versioning

- **Versioning (C1).** Single source: `git describe --tags --match 'v*' --abbrev=12` (the
  `--abbrev` is **pinned** — git ≥ 2.36's adaptive default can vary the short-hash length between
  runs, changing the stamped version of byte-identical source). Robust against the known failure
  modes: CI checks out with `fetch-depth: 0` (tags must be visible — applies to **any job that
  stamps a version**, not only the release job); build outputs are `.gitignore`d so the tree isn't
  spuriously `--dirty`; a bootstrap tag (`v0.1.0`) gives `describe` an anchor on first release.
  **The bootstrap tag is a one-time manual prerequisite** (`git tag v0.1.0 <commit> && git push
  --tags`), created before C1 lands; C1's unit test mocks `git describe` (so it runs tag-free), and
  the real `v3ke --version` behavior is the integration check. On `describe` failure (no reachable
  `v*` tag), `build.py` **errors loudly** with a "create the bootstrap tag" message — never a silent
  `"unknown"` fallback. Stamp into the `v3ke` binary (`nim c -d:v3keVersion=…`), the manifest, and
  `v3ke --version`. Retire the hardcoded `v3ke.nimble` `0.1.0`. The top-level version is the
  *v3ke repo* version; the *klipper/katapult* versions are the submodule commits in the manifest —
  the manifest makes that distinction explicit.
- **Release zip (C2).** `build.py release` produces `v3ke-<version>-linux-amd64.zip` containing: the
  `v3ke` binary, `mcu-firmware/{katapult,klipper}.bin`, `klipper/c_helper/c_helper.so`,
  `klipper/klipper_host_mcu/klipper_mcu.elf`,
  the `printer-config-files/`, **`klipper.dict`** (Klipper's data-protocol dictionary, emitted by
  the MCU `make` alongside `klipper.bin` — A3 must capture it into its `BuildStep.output_path`s or
  the zip assembly silently drops it), an **`INSTALL.md`** (points at RFC #2's DEPLOY.md; states
  plainly the zip is flash-ready but the host-swap procedure ships separately), a **`LICENSES/`**
  directory with the vendored klipper (GPL-3.0) + katapult license texts and a `SOURCES.md`
  (submodule URLs+commits) to meet the GPL source-offer obligation, and a schema-versioned
  **`manifest.json`**.
- **Manifest (C2).** A first-class build attestation, not an ad-hoc dict, validated against a
  checked-in JSON Schema at **`tools/build/manifest.schema.json`** (the C2 RED test validates the
  emitted manifest against it). v1 fields: `_type`/`schema_version`, a `build` block (id, timestamp,
  `reproducible: bool` set by the repro job, toolchain component versions), a `sources` map
  (klipper/katapult commit+url), and an `artifacts` array (name, path, sha256, size). **Toolchain
  versions are extracted via an explicit, testable step**: `ct-build --emit-versions` writes a
  structured `versions.json` to a known path *inside the container* during `build.py artifacts`
  (the only context that can see the baked ct-ng config); `build.py release` reads that file. The
  extraction is a `FakeRunner`-testable step in `artifacts.py`, not implicit in the release command,
  so a failed extraction surfaces rather than silently blanking the field.
- **CI release job (C3, on tag).** Build everything; **the repro job is a hard gate** — a tag-build
  that fails byte-identical reproducibility **blocks the release** (a published artifact is always
  `reproducible: true`; `reproducible: false` only ever appears on non-tag dev builds). Produce the
  zip + a `SHA256SUMS` that **includes `manifest.json`** (so the signature covers provenance, not
  just artifact bytes), **sign the sums with `cosign` keyless (OIDC, no key management) →
  `SHA256SUMS.sig`**; a **cosign signing failure aborts the release** (no unsigned release is ever
  published). Create the GitHub Release with `--generate-notes`, marked **pre-release for `v0.x`**
  tags. Releases are **immutable**: the job uses `gh release create --fail-if-exists`; re-shipping
  requires a new tag (a re-run would otherwise collide on the asset name and produce a different
  `build.id`/manifest under the same version). Requires `permissions: contents: write` +
  `id-token: write` (cosign OIDC).

## 4. Slices (rfc-flow Stage 1 → /tdd-sized)

Each is independently testable; RED test named. Dependencies noted. **Two slice classes:** *unit
slices* get a writable failing test in the normal pytest/nimble loop (the lightweight `v3ke-dev`
container — nim + gcc + uv + python — *not* the cross-toolchain image); *milestone
slices* (CI topology — B0/B3/B4/C3) cannot — there is no failing test you can author before a
workflow has ever run. For those, "RED" is the **absence of the capability** (no image, no workflow
file) and "GREEN" is the first successful run; the /tdd RED requirement is satisfied by their
**supporting contracts** (a workflow-YAML lint, a local sha256 double-build script, an `act` dry-run)
rather than a behavioral test. `act` (a dev tool installed in A0) drives local dry-runs **where it
works**: it shells to a container engine — usable with podman only via
`--container-daemon-socket unix://$XDG_RUNTIME_DIR/podman/podman.sock`, which is fiddle-prone, so the
**primary** validation path for milestone slices is the real CI run, with `act` as a best-effort
local pre-check. **Unit vs. integration split:** the FakeRunner/constraint tests, ELF/ABI, source
parse, and version-stamping tests need only the lightweight `v3ke-dev` image; any test that needs
the cross-toolchain to produce a real artifact is `@pytest.mark.integration` and runs only in the
toolchain image — `pytest -m "not integration"` is the first-clone loop. See §8 for the first-clone developer path.

**Stage A — build module**
- **A0.** Test scaffolding (precedes all of A1–A4). `pyproject.toml`/`pytest.ini` (with an
  `integration` marker registered) + `tools/tests/`, a `nimble test` task in `v3ke.nimble`,
  `tools/abi/` created, pinned test deps, **`act` pinned as a dev tool**. RED: `pytest --collect-only`
  exits 0 with 0 tests and `nimble test` runs green — both in the lightweight `v3ke-dev` image (no toolchain image).
- **A1.** `build/elf.py` pure `inspect_elf` (PHDR **and** section-header parse) + `check_abi`
  (→ `AbiResult` w/ `.applicable`, `AbiViolation`/`LoaderViolation`, `ArtifactKind` incl.
  `RAW_FIRMWARE`), checking machine / endianness / nan2008 / o32 / mips32r2 / **`fp_abi` (from
  `.MIPS.abiflags`)** / loader; table-driven `tools/abi/abi_spec.py` + the golden fixtures. RED:
  accepts the known-good fixture and rejects each **per-flag** known-bad fixture (one wrong field
  each — the bad-fp_abi fixture has `fp_abi=1`/`5` in its abiflags section, **not** a flipped
  `e_flags` bit); `RAW_FIRMWARE` input yields `applicable=False`, not a raise. Establishes the G2 spec.
- **A1c.** Split `common.nim`'s ELF reader into `tools/v3ke/elf.nim` (adding `.MIPS.abiflags`
  parsing), re-point `verify.nim` to `import elf`, then write the cross-language golden test: the Nim
  reader agrees with `elf.py` on every `tools/abi/` fixture. RED: `nimble test` exercises `elf.nim`
  and asserts the same accept/reject verdicts as A1. *(The Nim split moves **here** — A1c needs
  `elf.nim` to exist; deferring the split to A5b would make this slice reference a file that doesn't
  exist yet. A5b then only deletes bash.)*
- **A-spike (O6). ✅ DONE (2026-06-06).** *Investigation, not TDD.* **Result: uniform FP64, no build
  change.** The cross-gcc defaults to `-mfp64`; a fresh clean build of `klipper_mcu.elf` is FP64
  (`fp_abi=6`), the device userspace is FP64, and an FP64 `klipper.elf` ran on-device with no SIGILL.
  The FPXX binary that motivated O6 was a **stale old-toolchain artifact**. **Output:**
  `abi_spec.ACCEPTED_FP_ABI = {FP64=6}` for both kinds (set in `abi_spec.py` + `elf.nim`); host-MCU
  build gains **no** flag. Full rationale in §5 O6. A4/A5a assert `fp_abi=6` on real output.
- **A2.** `build/host.py` `chelper_sources()` (ast parse) — and its MIPS command builders. RED:
  returns the **expected filename set** (asserted against a checked-in `tools/tests/fixtures/
  chelper_sources.txt` snapshot, **not** a bare count — a count silently breaks on a Klipper
  submodule bump with a confusing "expected N, got N+1"; a set diff is reviewable) from a fixture
  `__init__.py`; raises on a malformed list.
- **A3.** `build/arm_mcu.py` + `build/artifacts.py` ARM-MCU-firmware steps (katapult + Klipper-MCU
  `make`), with the typed `BuildStep` handoff, the injectable `runner: -> RunResult` seam, and
  proactive `SOURCE_DATE_EPOCH` (= `git log -1 --format=%ct`) / `-ffile-prefix-map`, capturing
  `klipper.dict`. RED (**unit, `v3ke-dev` only — no toolchain image**): with a `FakeRunner`, the emitted `BuildStep`s resolve
  the **correct, existing** KCONFIG paths and carry the determinism flags (a *constraint* test, not
  arg-string mirroring); `check_abi` is **not** invoked for the `RAW_FIRMWARE` `.bin`s. *(Integration
  GREEN — real artifacts — is an **A5a** acceptance criterion, not A3's; A3 can't run the container.)*
  Depends on A1.
- **A4.** `build/artifacts.py` host steps (`klipper_mcu.elf` cross-make + `c_helper.so` gcc using
  A2's `host.py` sources), sequential w.r.t. the Klipper build tree. RED (**unit, `v3ke-dev` only — no toolchain image**):
  `FakeRunner` shows the **required ABI flags** (`-mips32r2 -mabi=32 -mhard-float -mfp64 -mnan=2008`)
  present and `cleanbuild` preconditions set. *(Integration — `check_abi` passes on real output, and
  `strings klipper_mcu.elf` carries no timestamp suffix — is an **A5a** acceptance criterion. Note:
  the `fp_abi` check passes on a **fresh** `klipper_mcu.elf` (the cross-gcc defaults to FP64 — A-spike
  confirmed; the old FPXX binary was stale). A5a must build clean, not reuse a stale artifact.)* Depends on A1 + A2.
- **A5a.** Wire `build.py artifacts` → the module end-to-end, one container run, per-step
  `StepResult`. RED: a unit test that `cmd_artifacts` no longer shells out to bash; **integration
  acceptance** (marked, container-required) confirms 4 artifacts, ABI-clean per the §5 `fp_abi`
  decision, reproducible determinism flags effective. This is where A3/A4's integration assertions
  actually run.
- **A5b.** Delete the 5 bash scripts. RED: a test asserting those paths are gone (`git ls-files`).
  **Gated on A5a green in an actual container run, validated by the B3 build job — so A5b's merge is
  sequenced after B3** (see the reordered list below; the `common.nim`→`elf.nim` split already
  happened in A1c).

**Stage B — CI + reproducibility** *(merge order: B0 → B1 → B2 → B3 → **A5b** → B4 → B5 — A5b lands
after B3 proves the container build, as its gate requires.)*
- **B0.** CI workflow: build the toolchain image + push to ghcr **publicly**, emit its `sha256`
  digest, on toolchain change / dispatch (`packages: write`). **Milestone.** Supporting RED: a
  workflow-YAML schema/lint test + `act -n` dry-run; GREEN is the first dispatch producing a pullable
  digest.
- **B1.** Pin base image by digest + drop the image-layer repro claim in the Containerfile/docs
  (from-source rebuild in B4 is the anchor). RED: a test asserting **both** that `FROM` is
  digest-pinned (no `:latest`) **and** that a Containerfile comment records `zypper dup` as
  acknowledged-non-reproducible (so a future contributor doesn't "fix" it and break the fast path).
- **B2.** CI **test job** wiring the A0 harness (pytest `-m "not integration"` + nimble test) +
  the submodule-integrity gate, in the `v3ke-dev` image (no toolchain image), least-priv permissions, `fetch-depth: 0`. RED (via
  `act push`): the test job runs the A1–A4 unit suites green; framed as `act` showing the expected
  job, not "push and hope."
- **B3.** CI **build job** (pull ghcr image **by digest**, inline-build fallback on pull failure,
  build artifacts, ABI verify). **Milestone** (`act workflow_dispatch` against the cached image, or
  first real run).
- **A5b.** *(merges here — see Stage A.)* Delete the bash scripts once B3 has proven the container
  build path.
- **B4.** CI **reproducibility job** — double build on **two independent runners pulling the same
  image digest**, cache disabled, assert identical sha256; diffoscope on mismatch. **Milestone** (the
  proof is the run). Supporting RED: a local `scripts/repro-check.sh` that builds twice and diffs
  sha256, runnable before CI exists.
- **B5.** *(residual investigation, only if B4 still diverges after A3/A4's proactive flags)* scope
  the specific remaining non-determinism. RED: B4 goes green.

**Stage C — release**
- **C1.** Version from `git describe --match 'v*' --abbrev=12` (fetch-depth 0, build outputs
  gitignored), stamped into `v3ke` + `--version`; retire nimble `0.1.0`. **Prerequisite (one-time,
  manual, before this slice):** create + push the bootstrap `v0.1.0` tag. RED: a unit test with
  `git describe` mocked asserts the stamped value + that no hardcoded version remains, and that a
  describe-failure raises a loud "create the bootstrap tag" error (not `"unknown"`); `v3ke --version`
  against the real tag is the integration check.
- **C2.** `build.py release` → zip (incl. `INSTALL.md`, `LICENSES/`, `SOURCES.md`, `klipper.dict`) +
  schema-versioned `manifest.json` (validated against `tools/build/manifest.schema.json`) with the
  `ct-build --emit-versions` toolchain-version step. RED: manifest validates against its schema,
  lists all expected members with correct sha256, and provenance fields (toolchain versions,
  submodule commits, `reproducible`) are populated; zip contains the license + install files.
- **C3.** CI release job on tag → repro-gated build → zip + `SHA256SUMS` (covering `manifest.json`)
  + cosign-keyless `SHA256SUMS.sig` (signing failure aborts) + GitHub Release (`--generate-notes`,
  pre-release for `v0.x`, `--fail-if-exists`) (`contents: write` + `id-token: write`). **Milestone**
  (`act` or a tag on a test ref produces the expected assets).

## 5. Open questions (for the architect rounds)

Resolved earlier: **O1** (golden-pinned double — kept, `tools/abi/`-anchored), **O3** (ghcr image as
a cache; B4 from-source rebuild is the trust guard), **O4** (`tools/build/` + `tools/abi/`),
**O2** (linux-only first — ship `v3ke-<version>-linux-amd64.zip`; win/mac matrix is a cheap Nim
cross-compile follow-up), **O5** (cosign keyless signature over `SHA256SUMS`; not plain-sums-only,
not full SLSA).

**O6 (raised round 2, spec-assumption escalation) → RESOLVED by A-spike (2026-06-06): uniform FP64,
option (b) — and the premise was a false alarm.** The spike (run with Corey's go-ahead, incl. an
on-device test) found the FPXX `klipper_mcu.elf` was a **stale artifact from the old pre-crosstool-ng
toolchain**, not current output. Evidence: (1) the crosstool-ng cross-gcc *defaults* to `-mfp64`
(`-mfpxx` disabled), so a plain compile already emits FP64; (2) a **fresh clean build** of
`klipper_mcu.elf` (Klipper adds no `-mfp*` flags) came out `fp_abi=6` (FP64), identical to
`out/klipper.elf`; (3) the device userspace is itself FP64 (`/lib/libc.so.6`, `ld-2.29.so` both
`fp_abi=6`); (4) an FP64 `klipper.elf` deployed to the printer's `/tmp` exec'd and ran with no SIGILL
(kernel FR=1). So **every artifact the current pipeline builds is FP64 with zero build changes** — no
Klipper-submodule patch, no `-mfp64` injection (the explicit `-mfp64` in the c_helper build is now
redundant belt-and-suspenders). `abi_spec.ACCEPTED_FP_ABI = {FP64=6}` for both kinds: an FPXX or
legacy-DOUBLE binary now means a stale/wrong-toolchain build, exactly the regression the ABI checker
exists to catch. CLAUDE.md's stated fp64 device ABI is **confirmed, not contradicted** — the contradiction
was an artifact of comparing a fresh FP64 lib against a stale FPXX executable. A4/A5a's integration
acceptance asserts `fp_abi=6` on real output. *(Option (a) — accept `{FPXX,FP64}` — was considered and
rejected: FPXX's FR=0/FR=1 portability is moot on a single fixed FR=1 device, and a permissive set would
blunt the regression guard for no benefit.)*

## 6. Risks

- **Reproducibility across independent environments may expose latent non-determinism** beyond the
  proactive flags → scoped to B5; the two-environment proof is more expensive (2× build) than a
  same-container check but is the only honest proof. Mitigated by tackling it behind a real test.
- **Deleting the bash scripts changes the in-container build path** → A5a must be green end-to-end
  (live `build.py artifacts`) and validated by the B3 build job before A5b's deletions land.
- **ghcr image publish adds a supply-chain surface** → B4 rebuilding the toolchain from source is
  the guard; the published image is a cache, never the source of truth.
- **GPL source-offer obligation** for the vendored klipper/katapult binaries → met by `LICENSES/` +
  `SOURCES.md` (submodule URLs+commits) in the zip; flagged so it isn't missed at release time.
- **CI permissions** are least-privilege per job; a misconfigured `GITHUB_TOKEN` scope silently
  fails ghcr push / release creation → each job's `permissions:` block is part of its slice.
- **Release-time failure modes have explicit fail-closed policies** so the implementer doesn't guess:
  a repro mismatch on a tag **blocks** the release (published artifacts are always
  `reproducible: true`); a cosign signing failure **aborts** (no unsigned release); a ghcr digest
  pull failure in the build job **falls back to an inline image build** rather than hard-failing.
- **~~`klipper_mcu.elf` may be the wrong FP ABI (FPXX, not fp64)~~** → **resolved (A-spike):** a fresh
  build is FP64 (cross-gcc default); the FPXX binary was a stale old-toolchain artifact. Checker now
  requires `fp_abi=6`, so a future FPXX regression (e.g. a toolchain swap) is *caught*, not silently shipped.
- **Klipper embeds a timestamp+hostname when `cleanbuild` is false** (dirty submodule / unreadable
  tool versions) → A4 asserts `cleanbuild` held; a violation is a named B4 failure mode, not a
  mystery diff.
- **`act` may not drive podman cleanly** → milestone slices are validated primarily by real CI runs;
  `act` is a best-effort local pre-check, not the gate.

## 7. Alternatives considered

- **`just`/`make` + language-native tools instead of a Python orchestrator module.** A `justfile`
  calling `nimble`, `pytest`, `podman` is thinner, but the build's real complexity is the ABI check
  + source extraction + structured per-step status — logic, not just task-running. That logic wants
  a tested language (Python), not recipe glue; the orchestrator earns its place. A `just`/`make`
  thin top-layer over `build.py` remains compatible and is not precluded.
- **Nix / flakes for the reproducible toolchain.** First-class binary-reproducibility, but replaces
  the settled, already-built crosstool-ng container design (a non-goal to revisit) and adds a Nix
  expertise burden. Rejected for this RFC; the from-source ct-ng build + B4 proof gives the
  reproducibility guarantee without the rewrite.
- **Bazel/Buck2.** Hermetic builds, but vast overkill for four artifacts + a CLI; the hermeticity we
  need is delivered by the container + B4. Rejected.

## 8. Developer experience (first clone)

The harness must be runnable on a fresh clone without the 20–40 min toolchain build. The documented
path (in `CONTRIBUTING.md`, authored alongside A0):

1. `git clone --recurse-submodules …` (or `git submodule update --init --recursive` — `external/` is
   empty otherwise, and the A2 source-parse + integration tests need the real klipper tree).
2. `podman build -t v3ke-dev tools/` — the lightweight dev/test image (`tools/Containerfile`: the
   pinned nim image + `uv`; gives nim + gcc + uv + python). Per the project convention, devtools run
   in podman, never on the host.
3. `podman run --rm -v "$PWD":/w:Z -w /w/tools v3ke-dev uv run pytest -m "not integration"` +
   `… -w /w/tools/v3ke v3ke-dev nimble test` — the **unit loop** (`uv` resolves/locks deps from
   `uv.lock`); this is what a contributor runs on every change and what CI's test job runs. It needs
   only `v3ke-dev`, **not** the multi-GB cross-toolchain image.
4. *(optional, for integration/repro work)* `podman pull ghcr.io/<org>/v3ke-toolchain@<digest>` —
   the publicly-readable cached image (no CI credentials needed) — then `pytest -m integration` or
   `build.py artifacts`. Only this step needs the heavy toolchain image.
