# Reproducible build & release pipeline — handoff

- **Stage:** 3 (/tdd implementation) **— COMPLETE.**   •   **Slices: A0, A1, A1c, A2, A3, A-spike, A4, A5a, B0, B1, B2, B3, B4, C1, C2, C3 implemented; B5 n/a; A5b WAIVED** (all 18 resolved).
- **A5b REVERSED + scripts DELETED (Corey, 2026-06-07, commit `6f6e853`):** the 5 legacy bash build scripts (`build-bootloader-mcu-and-host-firmware.sh`, `klipper/c_helper/build-chelper.sh`, `klipper/klipper_host_mcu/build-klipper-host-mcu.sh`, `klipper/read-elf-infos.sh`, `verify-artifacts.sh`) are GONE. The Python pipeline is now the sole build path (proven A5a+B4; the rewritten workflows invoke only it). Provenance kept via git history + the "Mirrors `<script>.sh`" docstrings in `arm_mcu.py`/`host.py` + the `_BANNED_SCRIPTS` regression guard in `test_orchestrate_a5a.py`. `klipper/klipper_host_mcu/readme.md` repointed to `build.py artifacts`. (Supersedes the earlier 2026-06-06 "WAIVED — keep as manual fallback" decision.)
- **/loop status:** stopped — RFC functionally complete; nothing left to implement.
- **CI TEST JOB GREEN ON GITHUB (2026-06-07, after the workflow rewrite + Dependabot/security setup):** the rewritten `ci.yaml` `test` job (Python+Nim) now passes on a clean GitHub runner. Getting there exposed **5 latent pre-existing bugs** (none from the rewrite — CI had just never run far enough to hit them), fixed in sequence: (1) `docker build tools/` failed — dir has `Containerfile`, not `Dockerfile` (fixed, then mooted by #3); (2) `ghcr.io/coreyleavitt/nim:2.2.10` base was private → 401 (Corey made it **public**); (3) **simplified the test job** — dropped the v3ke-dev image build + `--network=none` entirely: Python now runs natively via `astral-sh/setup-uv@fac544c…` (pinned 0.11.19) + `uv run --frozen pytest`; Nim runs in the public `ghcr.io/coreyleavitt/nim:2.2.10` (exact pinned compiler, zero build); (4) golden ELF fixtures (`tools/abi/fixtures/*.elf`) were in **Git LFS** → arrived as pointer text on CI (`Bad ELF magic: b'vers'`) — **de-LFS'd** via narrow `.gitattributes` override + `git add --renormalize` (general `*.elf` LFS rule kept for firmware); (5) `TestHostStepsFakeRunner` depended on real build artifacts in-tree → made **hermetic** (tmp repo + materializing runner writing golden fixtures). Commits `619027d`→`98ea849` on `origin/main`. Decision on `--network=none`: dropped — no real security gain on an ephemeral runner w/ no secrets; deps already come from PyPI at install time.
- **CI status now:** `test` job GREEN per push/PR. `build`/`repro`/`release` still **fail-closed on the all-zeros `toolchain/IMAGE_DIGEST` placeholder** (by design — the bootstrap gate). To green them: Corey runs `docker login ghcr.io` + `build.py image --push` (see `docs/toolchain-image.md`). **OPEN OFFER to Corey:** alternatively gate `build`/`repro` to *skip* on the placeholder so `main` shows green pre-bootstrap (release.yaml still hard-fails). Awaiting his pick.
- **CODE-REVIEW FIX LOOP COMPLETE + COMMITTED + PUSHED (2026-06-07):** the `/code-review` fix loop hit the floor (0 Critical/High/Medium) across 3 rounds. All non-CI findings fixed + locked by behavioral regression tests; suites green (Python 330 passed/1 skipped; Nim 45 OK). Committed as **`3a3ba91`** ("Fix code-review findings…through Medium") and **pushed to `origin/main`** (50d4c20..3a3ba91, fast-forward — the feature-branch tip *was* origin/main) and `origin/reproducible-cross-toolchain`; local `main` moved forward too. Compiled Nim test ELF `tools/v3ke/tests/tfindings` gitignored (was missing from the list). Full per-finding ledger below (Round 1/2/3 blocks).
- **OPEN FORK — CI hardening (the 4 DEFERRED findings, walked through w/ Corey 2026-06-07):** C3/C4/H2/M7 remain deferred (CI surface, `.github/workflows/*.yaml` authored-but-never-run). Root blocker for C3/C4 = `toolchain/IMAGE_DIGEST` still the all-zeros placeholder (ghcr bootstrap TODO; needs a local `build.py image --push` to populate it — see `docs/toolchain-image.md`). **SUPERSEDED by the rewrite decision below** — Corey chose to rewrite all GH Actions from scratch rather than patch the four findings.
- **GITHUB ACTIONS REWRITE — DESIGN RESOLVED (2026-06-07, supersedes the patch-the-4-deferreds fork above):** Rewrite ALL workflows from scratch, pulling the **release model/style from `coreyleavitt/nopal`** but keeping v3ke's stronger supply-chain rigor. Resolved forks (AskUserQuestion): (1) **toolchain image = digest-pin** — build LOCALLY (Corey's ghcr creds), push to ghcr, record `sha256:…` in `toolchain/IMAGE_DIGEST`, CI pulls BY DIGEST and **hard-fails** (NO inline-build fallback); (2) **KEEP cosign keyless** signing of SHA256SUMS (covers manifest.json); (3) **full nopal dispatch-button release model** — `workflow_dispatch` with `bump`+`prerelease` inputs, `prepare-version` computes version, signed-commit+tag via GitHub Git Data API; (4) **`.yaml` not `.yml`** (see [[pref-yaml-extension]]).
  - **nopal model map (from sonnet recon):** Release=`build.yaml` dispatch-only, job graph `prepare-version → compile(4-arch matrix) → package-{ipk,apk}(workflow_call) → release`; version read/written from a source file; signed commit+tag via `gh api .../git/{blobs,trees,commits,refs}`; `gh release create --generate-notes [--prerelease]`; SHA256SUMS only (nopal has NO cosign — we ADD it back); toolchain pulled as `ghcr.io/coreyleavitt/nopal-toolchain:latest` (we use digest); `.yaml`, reusable `workflow_call` sub-workflows, but actions NOT SHA-pinned (we KEEP our SHA pins).
  - **Planned file inventory (all `.yaml`):** `ci.yaml` (rewrite ci.yml; push main+PR; jobs test→build+ABI→repro 2×→repro-compare, submodule gate in EVERY job) · `release.yaml` (rewrite release.yml; workflow_dispatch bump/prerelease; jobs prepare-version→build→repro-gate→package+cosign→release) · ~~DELETE `build-toolchain-image.yml`~~ **DONE** (deleted; image now built locally via `build.py image --push`; local flow documented in `docs/toolchain-image.md`).
  - **How the 4 deferreds die (removed, not patched):** C3+C4 → no fallback path exists (digest pull hard-fails); M7 → workflow creates the tag itself (no non-tag release possible); H2 → submodule gate in every job from line 1.
  - **Retained rigor (stronger than nopal):** SHA-pinned `uses:`, default-deny `permissions:{}`+per-job least-priv, concurrency groups, digest-pinned images, cosign keyless.
  - **My recommendation (proceed unless Corey objects):** version source = NEW top-level **`VERSION`** file, read/written by prepare-version, embedded into manifest.json + `v3ke --version`. **This changes RFC slice C1 (git-describe → VERSION-file)** — authorized by the nopal-model choice, flagged not silent.
  - **Bootstrap = Corey's local op:** build+push toolchain image + capture digest into `toolchain/IMAGE_DIGEST` (his ghcr creds). My deliverable: workflows + helper (extend `tools/build.py image` to `--push` + write IMAGE_DIGEST) + docs. Real CI-green stays gated behind that push.
  - **RESUME:** ~~awaiting Corey's "start writing" go~~ **COMPLETE + VERIFIED (2026-06-07).** Implemented in 5 TDD slices (R1 VERSION+`resolve_version` reads it · R2 `build.py image --push`+digest helpers in new `tools/build_main.py` · R3 `release.yaml` dispatch-button · R4 `ci.yaml` no-fallback+gate-every-job · R5 delete image workflow+docs). `release.yaml` + `ci.yaml` written; `build-toolchain-image.yml` deleted; local build-push-pin documented in `docs/toolchain-image.md`; `toolchain/IMAGE_DIGEST` header rewritten; `prerequisites.md` updated. **Python suite 398 passed/1 skipped/1 deselected.** Opus-verified both workflows by hand: release.yaml signed-commit/tag API chain correct + dispatch-only (no infinite loop); ci.yaml has 3 submodule gates (test+build+repro), all-zeros reject in build+repro, ZERO real continue-on-error/fallback, top `permissions:{}`+per-job least-priv. **All 4 deferreds (C3/C4/H2/M7) structurally eliminated.** **C1 versioning changed git-describe→VERSION-file** (bare semver; tag = `v`+version). **UNCOMMITTED** — awaiting Corey's commit decision. **Still gated on Corey's one-time bootstrap:** `docker login ghcr.io` + `build.py image --push` to populate `toolchain/IMAGE_DIGEST` (placeholder = CI fails closed by design until then). `v3ke --version` wiring to VERSION NOT done (build.py reads VERSION for manifest build.id; CLI version embedding is a separate follow-up if wanted).
- **Deploy + wiring guides WRITTEN (2026-06-07):** `DEPLOY.md` (repo root, NEW — end-to-end: backup → build+`v3ke verify` → `flash all` SWD → `deploy` stage/validate → manual host swap → bring-up → smoke test, + independent host/MCU rollback + troubleshooting table) and the **SWD-flashing section appended to `pinout/creality-mainboard-pinout.md`** (ST-Link→pad wiring by signal, no-BOOT0 note, V3SET `stlink-dap.cfg` vs V2 `stlink.cfg`, openocd cmds, stock-restore). `INSTALL.md` stub updated to point at DEPLOY.md. **Two inline TODOs left for Corey:** (1) annotated SWD-pad + ST-Link V3SET connector **photos** (placeholder in wiring doc); (2) ST-Link V3SET exact STDC14 pin numbers (mapped by signal, points at ST UM2448). Nothing committed.
  - **WIRING-DOC RESTRUCTURE DONE (2026-06-07, Corey picked the dedicated-doc option):** the SWD flashing section no longer lives in the pinout reference. (a) NEW **`mcu-firmware/swd-wiring.md`** = the wiring guide (ST-Link↔pad signal map, photo TODOs, no-BOOT0, V3SET-vs-V2 interface selection; points to v3ke for the easy path + the install docs for raw openocd). (b) `pinout/creality-mainboard-pinout.md` reverted to pure reference (SWD pad table + 1-line cross-link to swd-wiring.md; truncated to 164 lines). (c) openocd flash commands de-duped to ONE home = `mcu-firmware/{katapult,klipper}-installation.md`, both now `interface/stlink-dap.cfg` (V3SET) with a V2-`stlink.cfg` fallback note + swd-wiring.md cross-link. DEPLOY.md + INSTALL.md repointed to swd-wiring.md; DEPLOY stays v3ke-CLI-first. Verified: no dangling anchors, every residual `stlink.cfg` is a deliberate V2 note. **Photo TODOs still open for Corey** (SWD-pad + ST-Link V3SET connector shots).
- **COMMITTED + PUSHED (2026-06-07):** the whole uncommitted pile (RFC Stage A0/B/C pipeline impl + this session's docs) is now on **`origin/reproducible-cross-toolchain`** (new branch on coreyleavitt/v3ke-klipper) in 3 grouped commits: `d14c208` pipeline+untrack-binaries, `dbd7be4` RFC+handoff, `50d4c20` deploy+wiring docs. **Build-output binaries untracked + gitignored** (release-only, per Corey): `mcu-firmware/*.{bin,elf,dict}`, `klipper/c_helper/c_helper.so`, `klipper/klipper_host_mcu/klipper_mcu.elf` — rebuild via `tools/build.py`. Also gitignored the compiled Nim test binaries under `tools/v3ke/tests/` (kept .nim sources). `tools/abi/fixtures/*.elf` kept (intentional ABI test data). Working tree clean; no PR opened. **Next open fork (unchanged):** `/code-review` of the branch vs start the RFC #6 spike (standalone load-cell endstop tap, see issue #1).
- **Next (open fork, NOT loop work):** commit the two guides (awaiting Corey's go) **·** `/code-review` (Stage 4, worth doing before trusting firmware on hardware) **·** PRTouch load-cell auto-Z (**RFC #6** / issue #1 — polish, not a print blocker). See [[klipper-pipeline-priorities]].
- **RFC #6 DESIGN CRYSTALLIZED (2026-06-07, this session — supersedes issue #1's vaguer "Proposed approach"; not yet written into the issue):**
  - **Naming trap pinned:** **PR-touch = the LOAD CELL** (HX711, PA4/PC6), NOT the CR-Touch. CR-Touch = `[bltouch]` (PC14/PC13, the deployable-pin probe, does the mesh). Creality's `prtouch_v2` = the load-cell half. (Confirmed in our pinout doc line 65-67 + prtouch_v2.c `use_adc` path + 0xD34D using `hx711s`.)
  - **DO NOT merge the two sensors into one `[probe]`** — that's the trap (`Duplicate chip name 'probe'` + the bed-damage configs). Klipper `probe` is a load-bearing singleton (bed_mesh/G28-Z/QGL/PROBE_CALIBRATE all `lookup_object('probe')`). Architecture = **one probe + one non-probe endstop + one orchestration command**: (1) **CR-Touch stays THE `[probe]`** (unchanged from salami), owns mesh+Z-home; (2) **load cell = standalone Z-contact endstop, NEVER the probe** — built from mainline's *separable* `McuLoadCellProbe` + `LoadCellProbingMove`, skipping the `add_object('probe')` that's isolated to top-level `LoadCellPrinterProbe` (load_cell_probe.py:639); (3) **thin gcode-command extra reconciles**: `z_offset = nozzle_z(load-cell tap) − z_probe(CR-Touch) at same sensor XY`, apply via `SET_GCODE_OFFSET` + write back CR-Touch z_offset (= 0xD34D's `cmd_PRTOUCH_PROBE_ZOFFSET` flow). User-facing "merge" is at the macro layer (one `CALIBRATE_Z`/`homing_override`), not the object layer.
  - **Reference verdicts (from the CrealityOfficial KE issue #1 thread + dug):** Creality **open-sourced prtouch** Dec-2025 (`K1_Series_Klipper@e09f36e`, GPLv3-clean: `prtouch_v2.c` 793L, `prtouch_v2_wrapper.py` 2202L) — **reference only**. **0xD34D/klipper_ender3_v3_se** = the model: its **orchestration shell is GOOD** (adopt — leaves CR-Touch as probe, computes the difference, KE-fit better than protoloft z_calibration), but its **probing primitive is BAD/UNSAFE** (`probe_by_step`/`_check_trigger` = host-loop polling + custom `dirzctl`/`hx711s` MCU stack = the bed-damage architecture; felipejfc/zekromisblack reported bed damage). ninja- gist = concept-only.
  - **WHY mainline's primitive is safer (verified):** `src/load_cell_probe.c` triggers IN THE MCU via `trsync_do_trigger` on force≥`trigger_grams` (lines 105-167) + MCU-side over-force `is_safety_trigger`→`ERROR_SAFETY_RANGE` (150) + watchdog (185) — aborts regardless of host timing. Keeping load cell OUT of the `probe` singleton ALSO means bed_mesh/homing can never accidentally fire a nozzle tap (safety property, not just a workaround).
  - **FIRMWARE: already done, no reflash, no prtouch_v2.c.** mainline `sensor_hx71x.c` + `load_cell_probe.c` are in-tree; `CONFIG_WANT_HX71X=y` + `CONFIG_WANT_LOAD_CELL_PROBE=y` in mcu-firmware/klipper.config. **Salami punts** on PR-touch (TODO.md `(x)` = not done; ships orphaned read-only `[load_cell]`, homes Z off CR-Touch). Integrating Creality's `prtouch_v2.c` would REGRESS to the unsafe host-poll primitive — don't.
  - **RFC #6 step 1 = a SPIKE:** instantiate the load-cell trsync endstop standalone (reuse `LoadCellProbeConfigHelper`+`McuLoadCellProbe`+`LoadCellProbingMove`, no singleton grab) and do one bare tap, proving the seam before writing orchestration. If coupling is deeper than the one `add_object` line → fall back to a minimal `LoadCellEndstop` shim around `McuLoadCellProbe`. Config sketch + safety guards (tare/baseline, slow bounded Z move, nozzle-clean-at-fixed-temp, re-probe-and-agree, first-layer verify) in the chat + to be written into issue #1.
  - **WRITTEN INTO issue #1 (2026-06-07, coreyleavitt/v3ke-klipper#1):** full rewrite around the **composite `[probe]`** design — not a three-piece split but ONE composite probe that owns CR-Touch + load cell, hooking **`start_probe_session()`** (the universal probing chokepoint: bed_mesh ProbePointsHelper:499, run_single_probe:533; SEPARATE from G28 Z-home which is event-driven home_rails_begin:228-231) so PR-touch is the **mandatory kickoff** of probing, not an occasional command. **Frequency = (b) once-per-home-cycle, cached, FORCE=1 override** (Corey decided). Plus reentrancy guard (internal reference CR-Touch probe must bypass kickoff), nozzle-clean-at-temp constraint, spike-first, reference verdicts, naming glossary, safety rationale, acceptance criteria. **Refinement vs the earlier "non-probe endstop invoked only by explicit command" framing:** Corey corrected it — the load-cell reference MUST run automatically inside the probe flow (CR-Touch absolute Z is only reliable with a fresh true-Z ref), so the composite-probe-with-start_probe_session-hook is the design, NOT a separate occasional command.
- **CORRECTION (2026-06-06, via grill-me + stock-config pull — supersedes all earlier "unsolved/blocker" wording):**
  - **Printing is NOT blocked.** The KE has **BOTH** sensors: a **CR-Touch** (`[bltouch]` PC14/PC13, drives the bed mesh) **and** a **strain-gauge load cell** (HX711 PA4/PC6). Stock Creality combines them via proprietary `[prtouch_v2]`+`[z_compensate]`: CR-Touch maps the mesh, strain gauge does a **nozzle-tap to set true Z=0**. **CR-Touch alone is fully upstream and already homes Z + meshes in the reference `printer.cfg`** — Corey can print on mainline today, doing a one-time **manual Z-offset** instead of the auto nozzle-tap.
  - **The real gap = the COMBINED two-sensor workflow, not load-cell capability.** Mainline HAS load-cell primitives (`load_cell_probe.py`/`hx71x.py`/MCU code) BUT `[bltouch]` and `[load_cell_probe]` **both register as the singleton `probe` object — only ONE allowed**. Mainline has no built-in "CR-Touch-for-mesh + load-cell-for-Z=0" (no z_calibration-style two-sensor model). Salami doesn't add it (orphaned `load_cell.cfg`, `TODO.md` PR-touch not-done, README non-goal on proprietary features).
  - **HARDWARE (corrected — Corey + 3dprinting.SE):** the KE/SE strain gauge is mounted **UNDER THE FRONT-LEFT CORNER OF THE BED** (single sensor, pres_cnt:1), NOT toolhead-integrated (an earlier Explore *inferred* toolhead — wrong). Confirmed by the stock tap location (`clr_noz x=-3,y=20` = front-left). It is a **single-point Z=0 reference**, purpose-built — fully sufficient for auto Z-offset, but cannot full-bed-tap. So "drop CR-Touch, load_cell_probe-only" is DEFINITIVELY out.
  - **MECHANISM = z_calibration pattern (auto Z-offset cal, not a probe):** CR-Touch maps bed *shape*; nozzle driven down at one point until the corner gauge detects bed deflect = true contact Z=0; `z_offset = CRTouch_trigger_height − nozzle_contact_height` (the M851 value). Automates the paper/feeler step.
  - **Strain-gauge options (RFC #6):** (a) CR-Touch + one-time manual z_offset [print now, zero dev]; (c) stock-like hands-free auto-Z = **bounded custom work**: a klippy extra exposing the HX711 force-trigger as a **standalone homing endstop** (mainline locks it inside `[load_cell_probe]` which steals the singleton `probe` from the CR-Touch) + the existing **`z_calibration` plugin** (or a homing_override) to reconcile. NOT "reverse-engineer prtouch." MCU fw Kconfig must build in HX711 sensor support. Shortcut to check first: an existing community mainline load-cell/prtouch port.
  - **Status:** `/grill-me` converged — design space resolved; one decision pending = (a) print-now-manual-Z vs (c) build-auto-Z. My earlier "Z-homing won't work → can't print" was WRONG — CR-Touch handles homing+mesh today.
- **A5b WAIVED (intentional, Corey 2026-06-06):** keep the 5 legacy bash scripts as a manual fallback (solo user, no CI). The Python build path is proven locally (A5a real build + B4 repro). This supersedes the earlier "deferred until CI" framing — it is now a deliberate keep, not a pending task. Don't delete unless Corey reverses it.
- **B-side mode (user decision 2026-06-06):** "Author B-side locally, defer push." Write the workflow YAML + hermetic local gates (pytest schema/lint, `act --list`); real CI GREEN (ghcr push, live runs) is deferred until the user commits+pushes. Nothing committed.

## A-spike (O6) — RESOLVED 2026-06-06: uniform FP64, option (b), zero build change
Premise was a FALSE ALARM (stale artifact). Evidence: cross-gcc **defaults to -mfp64** (`-mfpxx`
disabled); a **fresh clean build** of klipper_mcu.elf = FP64 (fp_abi=6, == out/klipper.elf); device
userspace is FP64 (`/lib/libc.so.6`, `ld-2.29.so` both fp_abi=6); FP64 klipper.elf **ran on-device**
(/tmp, kernel FR=1, no SIGILL). The lone FPXX klipper_mcu.elf (753876 B) was stale old-toolchain; fresh
is 740432 B. **Decision:** `ACCEPTED_FP_ABI = {FP64=6}` both kinds (FPXX/DOUBLE rejected as
stale/wrong-toolchain — the regression the checker exists to catch). No Klipper patch, no -mfp64
injection (c_helper's explicit -mfp64 is now redundant). Applied to `abi_spec.py` + `elf.nim` (reverted
an interim {5,6}); RFC §5 O6 / A-spike slice / §6 risk / A4 note updated. Suites still green (good
fixture fp_abi=6 accepted, bad=1 and FPXX=5 rejected). **For A4/A5a:** assert fp_abi=6 on real output;
A5a must build CLEAN (not reuse stale artifacts). **On-device test left no trace** (scratch /tmp files
removed; stock Klipper untouched).
- **RFC:** `docs/rfc-build-release-pipeline.md`
- **Blocking on Corey:** none — O6 resolved (spike-then-decide; slice **A-spike** carries it inside the loop).
- **RFC "podman-free" wrong-spec: FIXED** (G3, §2, §4 preamble, A0, A3, A4, B2, §8, test-job all now say "lightweight `v3ke-dev` image, not the cross-toolchain image"; §8 `pip install` → `uv run`/`podman build tools/`).

## Stage-3 slice progress (18 slices)
Order: A0 · A1 · A1c · A2 · A-spike · A3 · A4 · A5a · B0 · B1 · B2 · B3 · A5b · B4 · B5 · C1 · C2 · C3
- [x] **A0** test scaffold — REDONE correctly after first attempt polluted the host (see below).
  Devtools now run in a **derivative podman image `v3ke-dev`** (`tools/Containerfile`: FROM
  `ghcr.io/coreyleavitt/nim:2.2.10` + `uv 0.11.19` via COPY-from astral; gives nim+gcc+uv+python in
  one lightweight container — NOT the 61 GB toolchain image). Files: `tools/pyproject.toml`
  (uv project, `package=false`, `[dependency-groups] dev=[pytest>=8]`, pytest `integration` marker,
  `pythonpath=["."]`), `tools/tests/test_harness.py` (1 smoke test), `tools/abi/README.md` +
  `tools/abi/fixtures/` (dir), `tools/v3ke/tests/tharness.nim` (Nim smoke test), additive `task test`
  in v3ke.nimble (C backend), `.gitignore` (.venv/pycache; uv.lock intentionally committed).
  **Acceptance GREEN in-container:** `podman run --rm -v $PWD:/w:Z -w /w/tools v3ke-dev uv run pytest`
  → 1 passed, exit 0; `… -w /w/tools/v3ke v3ke-dev nimble test` → [OK], exit 0. Host stays clean.
  **Not committed.**
- [x] **A1** `build/elf.py` ABI checker. `tools/build/elf.py` (pure `inspect_elf` parsing PHDR+section
  headers → reads `.MIPS.abiflags` fp_abi; `ElfInfo`/`AbiResult`/`AbiViolation`/`LoaderViolation`/
  `ArtifactKind{SHARED_LIBRARY,EXECUTABLE,RAW_FIRMWARE}`; `check_abi`), `tools/abi/abi_spec.py`
  (table-driven `DEVICE_ABI` rows for nan2008/o32/mips32r2 + `EM_MIPS`/`ELFDATA2LSB`/`EXPECTED_LOADER`
  + **PROVISIONAL** `ACCEPTED_FP_ABI` = {FP64=6} per kind, pending A-spike), `tools/abi/fixtures/`
  (9 synthetic ELF32-LE fixtures + `_gen.py` generator: 1 good_exec + good_dyn + 7 per-flag bad incl.
  `bad_fp_abi.elf` with fp_abi=1 in real abiflags, not e_flags), `tools/tests/test_elf.py` (26 tests).
  Verified `0x70001407` reconciles (arch 0x70000000 / abi 0x1000 / nan2008 0x400). **Acceptance GREEN:**
  `podman run --rm -v $PWD:/w:Z -w /w/tools v3ke-dev uv run pytest -m "not integration"` → 27 passed,
  exit 0. **Design note (accepted):** non-ELF firmware uses `ElfInfo.raw_sentinel()` + `check_abi(RAW_FIRMWARE)`
  short-circuit; `inspect_elf` still raises `MalformedElfError` on non-ELF bytes (parse errors stay explicit). **Not committed.**
- [x] **A1c** Nim ELF reader split + cross-language golden test. New `tools/v3ke/elf.nim` (pure
  ELF32-LE reader, **adds `.MIPS.abiflags` fp_abi parse at section byte 7**; `ArtifactKind`/`ElfInfo`/
  `Violation{IntViolation,LoaderViolation}`/`AbiResult`/`ok`/`readElf`/`checkAbi`; constants mirror
  `abi_spec.py` incl. `AcceptedFpAbi={6}` PROVISIONAL). `common.nim` slimmed to terminal/error/`shQuote`
  only; `verify.nim` repointed to `import elf` + prints typed violations incl. fp_abi. New
  `tools/v3ke/tests/tabi.nim` (20 tests: same accept/reject verdicts as A1 on every shared fixture).
  `v3ke.nimble task test` runs tharness+tabi via **direct `nim c -r`** (no nimble stock-nim download).
  **Acceptance GREEN:** `podman run --rm -v $PWD:/w:Z -w /w/tools/v3ke v3ke-dev nim c --hints:off
  --path:. -r tests/tabi.nim` → 20/20; A0 tharness 1/1; `v3ke.nim` compiles clean; Python A1 still
  27/27. **Note:** real abiflags `fp_abi` is at struct byte **7** (not 8) — A1 generator + both readers
  consistent; G2 cross-check confirms no Python/Nim disagreement on any fixture. **Not committed.**
- [x] **A2** `tools/build/host.py` `chelper_sources(init_py: str|Path) -> list[str]` — `ast`-based
  (not exec/regex) extraction of `SOURCE_FILES` from Klipper `chelper/__init__.py`; walks module-level
  children only (avoids false matches); raises typed `ChelperParseError` on missing/non-list/non-literal/
  syntax/not-found. Snapshot `tools/tests/fixtures/chelper_sources.txt` (21 files, from real pinned
  submodule e60fe3d); test diffs as frozenset with added/removed message (not a bare count). gcc/make
  builders **deferred to A4** (no speculative stubs). `tools/tests/test_host.py` 15 tests.
  **Acceptance GREEN:** `uv run pytest -m "not integration"` → 42 passed (A1 27 + A2 15). **Not committed.**
- [x] **A3** `tools/build/arm_mcu.py` (pure katapult+Klipper-MCU `make` builders) + `tools/build/artifacts.py`
  (I/O runner seam: `BuildStep`/`RunResult`/`StepResult`/`subprocess_runner`/`FakeRunner`/`run_steps`,
  fail-fast, guarded `check_abi` skipped for RAW_FIRMWARE, sha256 kept separate). Real paths reproduced
  from `build-bootloader-mcu-and-host-firmware.sh`: KCONFIG at `mcu-firmware/{katapult,klipper}.config`,
  make -C `external/{katapult,klipper}`, outputs `out/{katapult,klipper}.bin` (RAW_FIRMWARE). Determinism
  proactive: `SOURCE_DATE_EPOCH=git commit time` (resolver decoupled from builders → tests pass epoch
  directly) + `-ffile-prefix-map`/`-fdebug-prefix-map` as make vars. `tools/tests/test_artifacts.py` 26
  tests. **Acceptance GREEN:** `uv run pytest -m "not integration"` → 68 passed. **Not committed.**
- [x] **A-spike (O6)** — RESOLVED: uniform FP64 (see dedicated section at top). `ACCEPTED_FP_ABI={6}`
  applied to abi_spec.py + elf.nim; RFC updated; suites green; on-device test left no trace.
- [x] **A4** `tools/build/host.py` MIPS-host builders: `c_helper_steps`/`klipper_mcu_steps`/`host_steps`
  (`(repo_root, source_date_epoch, *, toolchain_root)`). c_helper.so = SHARED_LIBRARY gcc step with the
  required ABI flags `-mips32r2 -mabi=32 -mhard-float -mfp64 -mnan=2008` + `-shared -fPIC` + determinism
  prefix-maps + SOURCE_DATE_EPOCH via `["env", "SOURCE_DATE_EPOCH=…", gcc, …]` prefix; sources from A2
  `chelper_sources`; output `external/klipper/klippy/chelper/c_helper.so`. klipper_mcu.elf = EXECUTABLE
  clean/olddefconfig/build triplet, `make -C external/klipper`, KCONFIG `klipper/klipper_host_mcu/
  klipper-host-mcu.config`, `CROSS_PREFIX=<toolchain_root>/bin/mipsel-buildroot-linux-gnu-`, output
  `external/klipper/out/klipper.elf` (clean-first → cleanbuild precond). **REFACTOR:** extracted the
  shared clean/olddefconfig/build triplet + determinism_vars + nproc into pure `tools/build/_makesteps.py`
  (added `extra_vars` for CROSS_PREFIX); arm_mcu.py + host.py both import it; arm_mcu's private helpers
  removed; A3's 26 tests still green. New `tools/tests/test_host_a4.py` (40 tests).
  **Acceptance GREEN:** `… -w /w/tools v3ke-dev uv run pytest -m "not integration"` → **108 passed**
  (27 A1 + 15 A2 + 26 A3 + 40 A4). **Not committed.**
- [x] **A5a** Wired `build.py artifacts` end-to-end through the Python modules — **bash scripts no longer
  invoked**. New `tools/build/orchestrate.py`: `build_all_artifacts(repo_root, toolchain_root, *, runner=
  subprocess_runner, epoch=None, _resolve_epoch=resolve_source_date_epoch) -> list[StepResult]`, also a
  container entrypoint `python3 -m build.orchestrate` (reads `CROSS_TOOLCHAIN`, prints per-step results,
  non-zero exit on fail/ABI-violation). **11-step canonical order:** katapult(3) · klipper(3) ·
  **klipper-capture** (`cp external/klipper/out/klipper.bin mcu-firmware/`, RAW_FIRMWARE — inserted
  BEFORE klipper-mcu-clean so the host build's `make clean` in the shared `external/klipper` tree can't
  wipe klipper.bin) · c-helper-build · klipper-mcu(3). `cmd_artifacts` = ONE `podman run … v3ke-toolchain
  -w /work/tools python3 -m build.orchestrate`. **`all` alias REMOVED** (--help documents the 2-step
  image→artifacts sequence). New `tools/tests/test_orchestrate_a5a.py` (22 unit) + `test_integration_a5a.py`
  (1, `@pytest.mark.integration`, gated on `CROSS_TOOLCHAIN`).
  **Unit GREEN:** `… v3ke-dev uv run pytest -m "not integration"` → **130 passed, 1 skipped**.
  **INTEGRATION GREEN (real in-container build, v3ke-toolchain):** all 11 steps [OK]; 4 artifacts —
  katapult.bin 2310 B, klipper.bin 36064 B (captured), c_helper.so 72528 B **fp_abi=6**, klipper_mcu.elf
  740432 B **fp_abi=6** (== A-spike fresh build; no embedded timestamp → cleanbuild confirmed); ABI
  VERIFICATION PASSED. This empirically closes Stage A's integration assertions. **Not committed.**
- [x] **B0** CI workflow `.github/workflows/build-toolchain-image.yml` (build cross-toolchain image →
  push ghcr `ghcr.io/coreyleavitt/v3ke-toolchain`, emit sha256 digest). Triggers: `workflow_dispatch`
  + `push` path-filtered to `toolchain/**` + self. Permissions: top-level `{}` deny-by-default, job
  `packages: write` + `contents: read`. Steps: checkout → ghcr login → buildx → build+push (`:latest`
  moving + `:<sha>` immutable, no cache) → digest to `$GITHUB_OUTPUT`/`$GITHUB_STEP_SUMMARY`. All four
  `uses:` **SHA-pinned** (tag in comment). ghcr public-visibility is a one-time manual setting (noted
  in-file). **`act` GAP from A0 closed:** added `act v0.2.89` (static binary) to `tools/Containerfile`;
  rebuilt v3ke-dev. New dev deps (pinned, offline-safe): `check-jsonschema>=0.29` (0.37.2, **vendored**
  GH-workflows schema → `--builtin-schema vendor.github-workflows`, no net) + `pyyaml>=6`. RED gate =
  `tools/tests/test_workflows_b0.py` (16 tests: schema-valid + structural — triggers/least-priv-perms/
  Containerfile-ref/ghcr-ref/digest-surfaced/SHA-pins). PyYAML 1.1 `on:`→`True` quirk handled via
  `_get_triggers()`. **act:** `act --list` works (pure parse, lists build-push job); `act -n` daemon-gated
  (no nested daemon in v3ke-dev — expected; pytest gate is authoritative, full act dry-run is deferred-push
  validation). **Acceptance GREEN (offline `--network=none`):** **146 passed, 1 skipped.** **Not committed.**
- [x] **B1** Digest-pinned the cross-toolchain base + documented the non-reproducible layer.
  `toolchain/Containerfile` `FROM opensuse/tumbleweed:latest` → `FROM registry.opensuse.org/opensuse/
  tumbleweed@sha256:ffe0ae6f…740e` (the registry.opensuse.org **index** digest as of 2026-06-06).
  **Why opensuse registry not docker.io:** docker.io serves a *different* rolling snapshot (digest
  147ac2…) + has anon CI rate limits; opensuse registry is upstream/authoritative & unthrottled.
  Dropped the "base is not pinned" repro claim from the header; added a `zypper dup` comment recording
  the install layer as **acknowledged non-reproducible** (image=cache; B4 from-source ct-ng build is
  the anchor — don't "fix" by pinning packages/removing dup). RED gate `tools/tests/test_containerfile_b1.py`
  (5 tests: FROM is `@sha256:`-pinned, no `:latest`, 64-hex digest, dup-non-repro comment present).
  Re-resolve digest via `curl -sI …/v2/opensuse/tumbleweed/manifests/latest` → Docker-Content-Digest.
  **Acceptance GREEN (offline):** **151 passed, 1 skipped.** **Not committed.**
- [x] **B2** CI **test job** in new `.github/workflows/ci.yml` (single workflow; B3/B4 will add jobs —
  only `test` authored, no stubs). Triggers push(main)+pull_request+dispatch; top-level perms `{}`,
  job `contents: read`; checkout SHA-pinned `fetch-depth:0` `submodules:recursive`. **Submodule-integrity
  gate:** `git submodule status --recursive` greps `+`/`-`/`U` prefixes → fail on drift/uninit/conflict
  (guards klipper e60fe3d / katapult b0bf421 / mainsail-config ff3869a from silent `--remote` swaps).
  Runs BOTH unit suites in **v3ke-dev** (built from tools/Containerfile): Python `uv run pytest -m "not
  integration"` + Nim **direct `nim c -r tharness/tabi`** (not `nimble test` → no stock-nim download,
  `--network=none`-safe). **Docker in CI** (ubuntu runners ship docker; podman stays local — identical
  flags). All `uses:` SHA-pinned. Factored `tools/tests/_workflow_helpers.py` (repo_root/load_workflow/
  get_triggers/all_steps/assert_uses_sha_pinned/validate_workflow_schema); B0 test left untouched. RED
  gate `tools/tests/test_workflows_b2.py` (33 tests). `act --list` lists the `test` job cleanly.
  **Acceptance GREEN (offline):** **184 passed, 1 skipped.** **Not committed.**
- [x] **B3** CI **build job** added to `ci.yml` (now 2 jobs). **Gated** `if: workflow_dispatch || ref==
  main` (never on PRs — 61 GB image). Perms `packages: read` + `contents: read` (consumer, no write).
  Checkout SHA-pinned `fetch-depth:0` `submodules:recursive`. **Pull-by-digest:** new repo-tracked
  `toolchain/IMAGE_DIGEST` (currently placeholder `sha256:0…0` + TODO; populate from B0's "Report image
  digest" step after first dispatch); step greps `^sha256:[0-9a-f]{64}$`, builds `IMAGE_REF=ghcr.io/
  coreyleavitt/v3ke-toolchain@<digest>`, `docker pull` with `continue-on-error`. **Inline-build fallback**
  (`if pull.outcome==failure`) builds from `toolchain/Containerfile` — self-healing before B0 publishes.
  **Build+verify:** `python3 tools/build.py --runtime docker --image <ref> artifacts` → `build.orchestrate`
  inside the image (exits non-zero on ABI violation). `upload-artifact` (SHA-pinned) ships the 4 artifacts
  (90-day). **build.py change:** added `--runtime {podman,docker}` (default podman; CI uses docker), threaded
  through cmd_image/snapshot/artifacts + `require_image(image, runtime)` (docker has no `image exists` →
  uses `image inspect`); +4 unit tests. RED gate `tools/tests/test_workflows_b3.py` (33 tests). `act --list`
  lists both jobs. **Acceptance GREEN (offline):** **221 passed, 1 skipped.** **Not committed.**
- [x] **B4** Reproducibility. **`scripts/repro-check.sh`** (POSIX/bash, `--runtime`/`--image` params):
  double-builds via `tools/build.py artifacts`, captures sha256 of the 4 artifacts to `.repro-check/
  build{1,2}/` (gitignored), `diff -u` the manifests → match exit 0 / mismatch runs diffoscope + exit 1.
  Each build is `--rm` + `make clean`, no ccache/cache-volume → true cold rebuild. **REAL DOUBLE-BUILD
  RAN (v3ke-toolchain, local): BYTE-IDENTICAL — reproducibility PROVEN.** katapult.bin 5039…ba20,
  klipper.bin 51f0…ee60, c_helper.so ba39…99a8, klipper.elf 3662…4a33 (both builds, ABI PASSED). This
  empirically validates A3/A4's determinism flags (SOURCE_DATE_EPOCH=commit-time + prefix-maps + clean).
  **CI `repro` job** in ci.yml (now 3 jobs + compare): `strategy.matrix.run:[a,b]` (two independent
  runners) each pull the **same** `toolchain/IMAGE_DIGEST` digest, cache disabled, upload sha256 manifest;
  `repro-compare` (`needs: repro`) downloads both + `diff -u` (diffoscope on mismatch). Both pull same
  digest (NOT from-source rebuild — that would re-float Tumbleweed via dup). Gated off PRs; least-priv;
  SHA-pinned. RED gate `tools/tests/test_workflows_b4.py` (33 tests). `act --list` lists test/build/repro
  + repro-compare. **Acceptance GREEN (offline):** **254 passed, 1 skipped.** **Not committed.**
- [x] **C1** Version stamping. New **`tools/v3ke/version.nim`**: pure `resolveVersion(raw: string): string`
  (the test seam — git is "mocked" by passing the describe string) strips whitespace, raises
  `VersionError` (subtype of `common.V3keError` → caught by v3ke.nim's existing top-level handler →
  loud msg + exit 1) on empty/whitespace ("create+push bootstrap v0.1.0 tag" action, never "unknown")
  or non-`v*` ("malformed"). Stamp seam: `overrideVersion {.strdefine:"v3keVersion".} = ""` else
  `staticExec("git describe --match 'v*' --abbrev=12 2>/dev/null")`; `version()` lazy (no compile-time
  raise in the untagged repo). `v3ke.nim`: `import version` + dispatch `version`/`--version`/`-v` →
  `echo "v3ke " & version()` + usage line. `v3ke.nimble`: retired hardcoded `0.1.0` → `pkgVersion()`
  derives semver-core from git describe (`0.0.0` untagged fallback; nimble rejects the `-N-gSHA`
  suffix); added `tversion.nim` to `task test`. RED `tools/v3ke/tests/tversion.nim` (**8 tests**:
  exact-tag/tag+commits+sha/strip/empty-raises-actionable/whitespace-raises/non-v*-malformed +
  no-hardcoded-version source asserts on version.nim & v3ke.nimble). **Bootstrap tag + real
  `v3ke --version` integration check DEFERRED** (push-gated). **Acceptance GREEN (offline):**
  tversion 8/8, tharness+tabi green, v3ke compiles; **override-stamp proof** `-d:v3keVersion:v0.1.0-3-gtest`
  → `v3ke --version` prints `v3ke v0.1.0-3-gtest`; **untagged loud-fail proof** bare build `--version`
  → exit 1 + bootstrap-tag message on stderr (not "unknown"); Python suite unchanged **254 passed,
  1 skipped**. **Not committed.**
- [x] **C2** Release packaging. New **`tools/build/release.py`** (pure core + thin I/O, established split):
  `resolve_version` (**Python mirror of C1's Nim** `git describe --match 'v*' --abbrev=12`; loud
  `ReleaseError`/bootstrap-tag msg on empty/non-v*/nonzero — deliberate double, injectable git seam),
  `submodule_provenance` (`.gitmodules` url + `git rev-parse HEAD:<path>` commit), pure `build_manifest`
  (`_type`/`schema_version`/`build{id,commit,timestamp(from epoch, NOT wall clock → reproducible),
  reproducible,toolchain}`/`sources`/`artifacts`), `validate_manifest` (jsonschema lib vs new
  **`tools/build/manifest.schema.json`** Draft2020-12: const `_type`/`schema_version`, sha256
  `^[0-9a-f]{64}$`), `hash_artifact`, `release_members` (pure zip plan), `release_zip_name`
  (`v3ke-<ver>-linux-amd64.zip`, O2), thin `write_release_zip` (deterministic order + epoch mtime).
  **`toolchain/ct_build.py`** gained `emit-versions` subcommand (pure `emit_versions()→{mips:{glibc
  2.29,gcc 8.5.0,binutils 2.32,linux 4.14.329,glibc_min_kernel 4.4.0,arch,float}, arm:{gcc 14.3.0}}`,
  stdlib-only). Static assets `tools/build/release_assets/{INSTALL.md(points at RFC#2 DEPLOY.md, out of
  scope),SOURCES.md(GPL source offer)}`. `build.py release` subcommand (`--out-dir`/`--reproducible`/
  `--toolchain-versions`; C3 passes `--reproducible` post-gate). `.gitignore` += `/dist/`,`/tools/dist/`.
  **Zip layout:** `firmware/{katapult,klipper}.bin` · `host/{c_helper.so,klipper.elf,klipper.dict}` ·
  top-level `INSTALL.md`/`SOURCES.md`/`manifest.json`/`v3ke` · `LICENSES/{klipper(from COPYING),katapult,
  mainsail-config,v3ke}.LICENSE`. RED `tools/tests/test_release_c2.py` (**73 tests**). Toolchain/container
  real-run = integration (deferred); pure core+schema+zip+provenance tested hermetically vs temp fake repo.
  **Acceptance GREEN (offline):** **327 passed, 1 skipped**; `ct-build emit-versions` JSON correct; sample
  manifest validates. **Not committed.**
- [x] **C3** CI release job. New **`.github/workflows/release.yml`** (separate from ci.yml — distinct
  `push: tags:['v*']` + `workflow_dispatch` trigger). Top-level `permissions: {}` + `concurrency`. Single
  `release` job, perms **exactly** `contents:write` (Release) + `id-token:write` (cosign keyless OIDC) +
  `packages:read` (pull toolchain image). Steps (all `uses:` SHA-pinned — checkout@11bd7190 v4.2.2,
  cosign-installer@dc72c7d5 v3.7.0): checkout `fetch-depth:0`+`submodules:recursive` → submodule-integrity
  gate → resolve image from `toolchain/IMAGE_DIGEST` + docker-pull w/ inline-build fallback → **repro gate
  `scripts/repro-check.sh` (step 4, BEFORE packaging)** → `ct-build emit-versions`→json → `build.py
  --runtime docker release --reproducible --toolchain-versions …` (step 6) → `SHA256SUMS` (covers
  `manifest.json`) → cosign install → **`cosign sign-blob --yes --output-signature SHA256SUMS.sig`
  (NOT continue-on-error → signing failure aborts)** → prerelease detect (`v0.*`) → `gh release create
  $tag --generate-notes --fail-if-exists [--prerelease]` uploading zip+SHA256SUMS+SHA256SUMS.sig+manifest.json.
  RED `tools/tests/test_workflows_c3.py` (**40 tests**: schema-valid, triggers, perms-exact, checkout,
  submodule-gate, all-SHA-pinned, repro-before-package ordering, --reproducible, sums-cover-manifest,
  cosign-keyless+not-coe, gh flags, prerelease, asset uploads). **Milestone (real tagged run: cosign OIDC +
  GitHub Release + 61 GB image build) DEFERRED** to push — structural contract is the local proof; `act
  --list` lists the release job. **Acceptance GREEN (offline):** **367 passed, 1 skipped.** **Not committed.**
- [x] **B5 — N/A (no work needed).** B5 = "residual non-determinism investigation, *only if B4 still
  diverges* after A3/A4's proactive flags; RED: B4 goes green." B4's first real double-build was already
  byte-identical (zero divergence), so there is no residual to scope. B5's RED is satisfied. If a future
  real CI run on two GitHub runners ever diverges (e.g. a runner-arch or locale difference the local
  same-host proof can't surface), reopen B5 then.

### Test-suite bloat — workflow part FIXED 2026-06-06 (Corey: "just fix it now")
**DONE — workflow consolidation:** the ~155 slice-named workflow constraint tests (b0 16/b2 33/b3 33/
b4 33/c3 40, which triplicated schema+SHA-pin+perms over the same ci.yml) were collapsed to **59
subject-named tests, zero behavioral/security coverage lost**. New layout (the old `test_workflows_b*.py`
+ `test_workflows_c3.py` are **deleted**):
- `tools/tests/test_workflows_common.py` (12 = 4 invariants × 3 workflows, parametrized over
  `_workflow_helpers.all_workflows()`): schema-valid · all-`uses:` SHA-pinned · top-level perms `{}` ·
  no write-all.
- `tools/tests/test_workflow_toolchain_image.py` (9) — build-toolchain-image.yml specifics.
- `tools/tests/test_workflow_ci.py` (27) — ci.yml test/build/repro/repro-compare specifics (merged
  B2+B3+B4 behavior; pytest-dup 4→2, nim-dup 5→2).
- `tools/tests/test_workflow_release.py` (11) — release.yml specifics.
All 8 security-critical assertions retained + named (SHA-pins, perms-deny-default, release-perms-exact,
cosign-not-continue-on-error, repro-before-package, submodule-integrity, nim-not-nimble, pytest-in-
v3ke-dev). Dropped only pure restatements + 3 file-property checks on repro-check.sh (exists/exec/shebang
— not workflow structure; its behavioral contract is covered by the release-job step that invokes it).
**Suite now 271 passed, 1 skipped** (was 367). NB: the per-slice B0–C3 entries below still cite the
OLD file names/counts as the historical RED gate at implementation time — superseded by this note.
**NOT done (deliberately left — genuine logic, not shape):** C2's 73 (still the highest single file;
real manifest/version/schema/zip logic, only lightly trimmable) and A4's 40 (ABI build-flag asserts —
correctness-critical for the exotic MIPS ABI). If further trimming is wanted, C2 is the place; flag at
/code-review.

### Carry-forward debts / env notes
- **CONVENTION (now in memory [[devtools-in-podman]]):** all devtools run in podman, never the host;
  `uv` is the Python resolver; nim dev image is `ghcr.io/coreyleavitt/nim:2.2.10`. The dev/test image
  is `v3ke-dev` (built from `tools/Containerfile`).
- **RFC wrong-spec — DONE (applied before A1):** "podman-free" reframed everywhere (G3, §2, §4
  preamble, A0, A3, A4, B2, §8, test-job) to "lightweight `v3ke-dev` image, not the cross-toolchain
  image"; §8 step 2/3 now `podman build tools/` + `uv run pytest` (was `pip install -e`).
- **nimble-fetches-stock-Nim — RESOLVED (at A1c).** Diagnosis: `nim` on PATH in `v3ke-dev` IS the
  patched `/opt/nim/2.2.10-patched`; the `task test` body `exec`s that, so tests DO compile with the
  patched compiler. But invoking `nimble test` still side-effect-downloads stock nim (~16 MB, network)
  every `--rm` run. **Canonical Nim test command is therefore direct `nim c --hints:off -r
  tests/t*.nim`** (patched nim, zero download, offline). `nimble test` kept only as a dev convenience.
- Working tree holds the **14 prior code-review fix files** (uncommitted) + untracked `docs/` + the
  new A0 files (`tools/Containerfile`, `tools/pyproject.toml`, `tools/tests/`, `tools/abi/`,
  `tools/v3ke/tests/`, `.gitignore` change, v3ke.nimble `task test`). Nothing committed.

## Session close 2026-06-06 — priority recalibration (Corey)
This RFC's build/release pipeline is **code-complete + locally green (271 passed, 1 skipped)** but NOT
"launch-ready": nothing committed; real CI never run; bootstrap tag absent; A5b pending; Stage 4
(/code-review) not done. **Corey's stated priorities:** doesn't care about CI/release ceremony yet
(**solo user, no other consumers**) — so ghcr publish / IMAGE_DIGEST / real CI runs / bootstrap tag /
cosign / GitHub Release / A5b are all **deprioritized**. For solo local use the pipeline is effectively
ready: `build.py image`→`artifacts` produces correct, reproducible artifacts (proven A5a+B4). The
**real blocker to actually printing is hardware-side, in separate RFC #2, not this one:** (1) no full
SWD-flash + mainline bring-up has been done end-to-end on the device; (2) **load-cell PRTouch Z-homing
is unsolved** (reference config uses CR-Touch/BLTouch) → Z-homing won't work → can't print. **Pending
fork (next session):** run `/code-review` (Stage 4, worth doing before trusting firmware on hardware)
**vs** start the PRTouch Z-homing problem (the actual thing between Corey and a print). Also: a local
commit was suggested for hygiene (push optional) — not yet done.

## Scope (locked)
In: #1 shell→Python build collapse + ABI-checker unification, #5 test harness, #3 CI +
reproducibility proof + base-image pin, #4 release packaging + versioning.
Out (separate RFCs): #2 host-swap/rollback/DEPLOY.md, #6 load-cell PRTouch.

## Slices (revised round 2)
Merge order: A0 · A1 · **A1c (does common.nim→elf.nim split + adds abiflags parse)** · A2 · A3 · A4 ·
A5a · B0 · B1 · B2 · B3 · **A5b (delete-bash, gated→merges after B3)** · B4 · B5 · C1 · C2 · C3.
- Stage A modules **renamed**: `arm_mcu.py` (katapult+klipper-MCU make) / `host.py`
  (chelper_sources + MIPS cross-builds) — replaces round-1's klipper.py/mcu.py (wrong axis).
- A1 ABI check now: machine/endianness/nan2008/o32/mips32r2/**fp_abi (from .MIPS.abiflags)**/loader;
  ArtifactKind incl. **RAW_FIRMWARE**; AbiViolation(int)/LoaderViolation; table-driven abi_spec.
- artifacts.py: typed **BuildStep** handoff, runner returns thin **RunResult** (not StepResult),
  fail-fast, sha256/manifest hashing separated out.
- A3/A4 RED are **unit/podman-free only**; integration assertions moved to **A5a** acceptance.
- B-side: trigger matrix, concurrency cancel-in-progress, submodule-integrity gate, pull-by-digest +
  inline-build fallback, B4 = two-runner/same-digest (dropped fresh-image option).
- C-side: --abbrev=12, bootstrap-tag prereq, manifest.schema.json + ct-build --emit-versions,
  SHA256SUMS covers manifest.json, cosign-fail aborts, repro-gate blocks release, --generate-notes/
  pre-release-v0.x/--fail-if-exists. New **§8 first-clone dev path**.
(no /tdd until O6 resolved — last blocking fork)

## Open forks
- (none open) — **O6 resolved: investigate-then-decide.** New slice **A-spike** (before A4 finalizes)
  determines why klipper_mcu.elf=FPXX vs c_helper.so=FP64, tries forcing -mfp64, then picks
  (a) accept {FPXX,FP64} for EXECUTABLE / {FP64} for SHARED_LIBRARY, or (b) tighten build to uniform
  FP64. Output sets `abi_spec.ACCEPTED_FP_ABI`; A4/A5a consume it. No further Corey input needed.
- O2 → linux-only first (resolved). O5 → cosign keyless (resolved).

## Resolved this round (round 2)
- fp64 mechanism **corrected**: round-1's e_flags-bit check was vacuous (bit unset even with -mfp64);
  now reads `.MIPS.abiflags` fp_abi. Both elf.py + elf.nim parse section headers.
- SOURCE_DATE_EPOCH = git commit time (was unspecified → would diverge on wall clock).
- Module split axis fixed (arm_mcu/host); runner seam thinned (RunResult); RAW_FIRMWARE kind added.
- A1c/elf.nim ordering bug fixed (split pulled into A1c; A5b = bash-delete only, reordered after B3).
- ~30 clear-best fixes total across depth/breadth/design/feasibility (see RFC §3–§8).
- O1/O3/O4 (round 1) carried forward.

## Round-1 findings applied to RFC (clear-best fixes)
- **fp64 gap (D3, High):** ABI check now covers EF_MIPS_FP64 — an fp32 binary passed every old gate.
- **Repro proof (D1/D6/breadth, High):** B4 now two *independent environments* (not same-container); ghcr image is a cache, B4 from-source rebuild is the proof.
- **Missing A0 (High):** test scaffolding slice added before A1 (tests were written before the harness existed).
- **Missing B0 (High):** ghcr image-publish slice added (B3 had nothing to pull).
- **zypper dup vs digest pin (D2, High):** image-layer repro claim dropped; from-source ct-ng build is the sole anchor.
- **Release dead-end (breadth, High):** zip ships INSTALL.md stub (→RFC#2) + LICENSES/ + SOURCES.md (GPL source-offer).
- **check_abi interface (design):** `list[str]` → `AbiResult`/`AbiViolation` dataclass; `want_loader: bool` → `ArtifactKind` enum.
- **Module granularity (design):** sources.py absorbed into deep `klipper.py`; pure cmd-construction (klipper.py/mcu.py) split from I/O runner (artifacts.py); `common.nim` ELF reader → `elf.nim`.
- **Golden coverage (D4):** per-flag known-bad fixtures, not one good/bad pair.
- **Test seams (breadth/feasibility):** injectable `runner` in artifacts.py + FakeRunner; A3/A4 RED tests reframed as constraint tests, integration smoke as named acceptance.
- **A5 split:** A5a wire / A5b delete (gated) / A1c cross-lang test pulled up after A1.
- **git-describe (D5/breadth):** --match 'v*', fetch-depth 0, gitignore build outputs, bootstrap tag, version-semantics note.
- **manifest:** schema-versioned attestation (_type, build/sources/artifacts blocks, reproducible bool).
- **CI permissions:** least-priv `permissions:` per job; B5 narrowed to residual; proactive determinism in A3/A4.
- **§7 Alternatives** added (just/nix/bazel); submodule-bump policy + signing surfaced (O5).

## Key decisions (this session)
- One RFC = the build/release pipeline domain; device-path (#2) and hardware (#6) stay separate — coherence.
- Python/Nim ABI checker stays a deliberate double, pinned by shared `tools/abi/` golden fixtures.
- Sequence is load-bearing: A (collapse) → B (CI/repro) → C (release).
- Reproducibility = byte-identical across *independent environments*, not a same-container rebuild.

## Review ledger (stage 4 — /code-review 2026-06-07, 5 reviewers + adversarial verify)
Mandate: **fix non-CI thru Medium, defer CI surface, leave Low.**
| id | sev | finding | status | proof / reason |
|----|-----|---------|--------|----------------|
| C1 | Crit | release.py:361 `firmware/klipper.bin` sourced from `external/klipper/out/` (wiped by host make clean); canonical = `mcu-firmware/klipper.bin` | FIXED (P1) | release_members sources mcu-firmware/klipper.bin; regr test TestC1* (2) |
| C2 | Crit | release.py:365 `host/klipper.dict` from wiped out/, no capture step | FIXED (P1) | new `_klipper_dict_capture_step` (cp before clean) + sourced from mcu-firmware/; TestC2* (5) |
| C-elf | Crit | release.py:364 `host/klipper.elf` same wiped-path + likely ARM-klipper.elf vs MIPS-klipper_mcu.elf mixup | FIXED (P1) | new `_klipper_elf_capture_step` → mcu-firmware/klipper_mcu.elf (MIPS, name-disambiguated); TestCElf* (5) |
| C3 | Crit | release.yml all-zeros IMAGE_DIGEST + continue-on-error → unverified image shipped+signed | DEFERRED | verified; CI surface (Corey deprioritized; ghcr bootstrap TODO) |
| C4 | Crit | release.yml repro gate runs against fallback image → circular | DEFERRED | verified; CI surface |
| H1 | High | artifacts.py:163/167/176 run_steps `ok=True` on missing/malformed output (silent-green) | FIXED (P1) | run_steps now ok=False on missing output_path or MalformedElfError; TestH1* (5) |
| H2 | High | ci.yml submodule-gate absent from build/repro jobs | DEFERRED | verified; CI surface |
| H3 | High | no golden fixture for absent .MIPS.abiflags (the FP-ABI regression the double exists to catch) | FIXED (E1) | new bad_no_abiflags.elf fixture; both parsers ALREADY rejected (fp_abi None→violation) — gap was test coverage; TestAbsentAbiflags (Py 3) + tabi suite (Nim 3) |
| M1 | Med | verify.nim/rollout.nim ElfError not V3keError → traceback, rollout fail-fast bypassed | FIXED (N1) | checkMips wraps readElf → fail() (V3keError); rollout covered via verifyCmd; tfindings (2 behavioral) |
| M2 | Med | verify.nim/deploy.nim default paths ≠ release-zip layout (host/) → zero-arg broken for release consumer | FIXED (N1) | defaults → host/c_helper.so + host/klipper.elf; flash FW_DIR untouched; tfindings (2, source-grep — re-review check depth) |
| M3 | Med | flash.nim:66 predictable /tmp readback file → verify bypass TOCTOU | FIXED (N1) | createTempFile (O_EXCL) std/tempfiles; tfindings (2, source-grep — re-review check depth) |
| M4 | Med | elf.py raises vs elf.nim silently breaks on truncated phdr/shdr → verdict divergence | FIXED (E1) | elf.nim phdr+shdr silent break → elfCheck raise ElfError; parity locked by tests both sides (tabi "truncated header tables" 2 + Py TestMalformedInput 2) |
| M5 | Med | release.py:377 manifest sentinel-path contract leak | FIXED (P1) | removed sentinel from release_members; write_release_zip appends manifest directly; TestM5* (4) |
| M6 | Med | submodule_provenance bare subprocess, no runner seam | FIXED (P1) | injectable `runner` param (matches resolve_version pattern); TestM6* (3) |
| M7 | Med | release.yml workflow_dispatch can release from non-tag ref | DEFERRED | CI surface |
| M8 | Med | BuildStep.output_path=Path("") sentinel (primitive obsession) | FIXED (P1) | Optional[Path]=None across _makesteps/artifacts; run_steps gate `is not None`; TestM8* (6) |
| LOW* | Low | elf.nim:245 stale comment; Nim entsize-trust (zero-stride DoS + refuted P2 divergence) — one entsize>=min guard; parity Lows (NUL strip, absent-fp_abi sentinel, per-kind dict, cpr1_size); CI Lows; design Lows (epoch-IO-in-pure-module, FakeRunner index, in-loop import) | open (batch/skip) | cosmetic per mandate |
| P2 | — | parity phdr/shdr stride divergence | REFUTED (practical) | no reachable input (fixtures+prod lock 32/40); folded into LOW entsize guard |
| H2b | — | repro-check.sh unsound | REFUTED | fails closed (diff -u, set -euo pipefail) |

### Round 2 (re-review of the fixes — 3 reviewers: correctness/security/design+test-depth, 2026-06-07)
Mandate unchanged: fix non-CI thru Medium, leave Low. C-elf REOPENED — the "fix" was masked by shape-only tests.
| id | sev | finding | status | proof / reason |
|----|-----|---------|--------|----------------|
| R2-C1 | **Crit** | orchestrate.py:145 `_klipper_elf_capture_step` cmd=`cp src DIR/` → produces mcu-firmware/klipper.elf, but output_path + release_members expect klipper_mcu.elf → H1 existence-check fails at build / zip unbuildable. **Reopens C-elf.** | **FIXED (PF)** — C-elf truly closed | one parameterized `_klipper_capture_step(src_name,dst_name,step_name)` w/ `cmd=["cp",str(src),str(dst)]` full path (orchestrate.py:121). BEHAVIORAL test runs REAL cp via run_steps, asserts klipper_mcu.elf lands + klipper.elf does NOT (test_r2_findings.py TestR2C1*); I re-read fix+test — locks the contract against str(dst_dir) revert |
| R2-H1 | High | tfindings.nim M2 tests (zero-arg default paths) are tautological source-grep (`src.contains("host/c_helper.so")`) — pass as long as the literal exists anywhere, survive a real regression | **FIXED (NF)** | extracted pure `defaultHostArtifacts()`/`defaultDeployArtifacts()`; tests call them + assert exact tuple — a wrong-path change now fails, a source-string change doesn't false-detect |
| R2-H2 | High | tfindings.nim M3 first test source-greps for absent `.readback` literal — tautological (2nd test already behavioral) | **FIXED (NF)** | deleted the source-grep test; behavioral "two temp names distinct" already covers uniqueness contract fully |
| R2-H3 | High | test_artifacts.py:329 `_make_dummy_steps` uses output_path=Path("") not None → Path("") resolves to cwd (exists) so H1 existence-check passes for the WRONG reason; fail-fast suite not testing the None-bypass it purports to | **FIXED (PF)** | `_make_dummy_steps` → output_path=None; TestR2H3* proves None-bypass + rc=0-missing-output→ok=False contract directly |
| R2-M1 | Med | orchestrate.py:83-148 three near-identical capture-step builders → one parameterized `_klipper_capture_step(src_name,dst_name,step_name)`; cmd should pass full dst path (this fix IS the R2-C1 fix) | **FIXED (PF)** | collapsed to one helper; 3 call sites now one-liners; full-dst-path |
| R2-M2 | Med | release.py:181-231 submodule_provenance Optional[Callable]=None + internal _run closure = a THIRD runner-injection idiom (resolve_version uses concrete default; write_release_zip uses _underscore test-only) | **FIXED (PF)** | aligned to `runner=_subprocess_runner_text` concrete default; _run closure removed |
| R2-M3 | Med | elf.nim:162-199 no floor on ePhentsize(≥20)/eShentsize(≥24) → crafted small entsize → IndexDefect (not ElfError) escapes M1's catch; fails-closed under -d:release, heap OOB if -d:danger. (Elevates the pre-existing LOW* entsize item) | **FIXED (NF)** — also closes LOW* entsize | elfCheck ePhentsize≥20 / eShentsize≥24 floors (gated on >0 entry counts so empty tables don't regress); 6 tabi tests incl. off-by-one boundaries + good-fixture regression; RED(IndexDefect)→GREEN confirmed |
| R2-L1 | Low | release.py:502/532 manifest hash + zip-write are 2 separate read_bytes() → manifest could attest different bytes than packed (tiny single-user-container window) | **FIXED (PF)** | read-once into member_bytes; sha256 + writestr over same bytes |
| R2-L2 | Low | test_artifacts.py:135,144 + test_orchestrate_a5a.py:240 stale `!= Path("")` assertions (post-M8 should be `is not None`); TestM6 signature-inspection test redundant to behavioral test below it | **FIXED (PF)** | `is not None`; redundant inspect-test deleted |
| R2-ok | — | VERIFIED CORRECT: M3 flash temp (mkstemp O_EXCL 0600 + sticky /tmp + comparison gates), M2 no traversal, command-injection clean (startProcess argv + checkOcdPath + shQuote), rollout fail-fast-before-flash sequencing sound (no rollback needed by design) | — | security reviewer |

### Round 3 (convergence re-review of the round-2 fixes — 3 reviewers, 2026-06-07) → **FLOOR REACHED**
**0 Critical / 0 High / 0 Medium. Only Lows remain → fix loop TERMINATES per mandate (fix thru Medium, leave Low).**
- **Correctness:** clean. Re-verified capture call-sites + ordering, runner equivalence, single-read manifest, fail-fast None-bypass; cp-rename contract locked. Nothing above Low.
- **Security:** `elf.nim` bounds-hardening **COMPLETE** — every attacker-controlled offset read gated to raise ElfError (ehdr, phdr entsize≥20, shdr entsize≥24, PT_INTERP off+sz≤len, .MIPS.abiflags shSize≥24/shOffset+shSize≤len; no e_shstrndx lookups; u32→int64 can't overflow on the linux-amd64 CLI host). defaultHost/Deploy paths no traversal. read-once manifest closed. Nothing above Low.
- **Design + test-depth:** refactors clean (parameterized capture helper, concrete-default runner, separate-but-justified default-path procs). Core new tests confirmed genuinely behavioral (R2-C1 real-cp landing, M2 default-path procs, tabi entsize crafted-ELF RED→GREEN w/ boundary). Nothing above Low.

**Remaining Lows (deferred per mandate — batch or skip):**
- R3 test cosmetics in test_r2_findings.py: dead `_build_single_step` helper (delete); 3 structural tests (`test_single_capture_helper_exists`, `test_capture_step_cmd_uses_full_dst_path`, `test_capture_step_output_path_matches_cmd_dst`) redundant w/ the behavioral R2-C1 tests; `test_no_internal_run_closure` AST-scan guard — all acceptable regression locks, weakest tests in their classes.
- Pre-existing LOW* cluster (minus entsize, now FIXED via R2-M3): elf.nim:245 stale comment; parity Lows (NUL strip, absent-fp_abi sentinel, per-kind dict, cpr1_size); design Lows (epoch-IO-in-pure-module, FakeRunner index, in-loop import); CI Lows.

**DEFERRED (CI surface, out of mandate scope):** C3 (all-zeros IMAGE_DIGEST + continue-on-error), C4 (repro gate vs fallback image), H2 (submodule-gate absent from build/repro jobs), M7 (workflow_dispatch non-tag release). These are real and tracked — revisit when CI is reprioritized (ghcr bootstrap TODO).

**Final suite state:** Python 330 passed / 1 skipped (integration); Nim 45 OK / 0 failed. Nothing committed.
