# Building & publishing the toolchain image

The cross-toolchain image (`ghcr.io/coreyleavitt/v3ke-toolchain`) is built **locally** by the
operator and pushed to the GitHub Container Registry.  CI (`ci.yaml`, `release.yaml`) pulls it
strictly by the SHA-256 digest recorded in `toolchain/IMAGE_DIGEST` and hard-fails if that
file still contains the all-zeros placeholder — there is no inline-build fallback.

---

## One-time setup

Log in to the GitHub Container Registry with an account that has write access to
`ghcr.io/coreyleavitt/v3ke-toolchain`:

```bash
docker login ghcr.io          # or: podman login ghcr.io
# enter your GitHub username and a personal access token with write:packages scope
```

---

## Build, push, and pin (the full local operator flow)

Pick an image tag (a short date or a descriptive label works; the digest is the stable pin):

```bash
TAG=ghcr.io/coreyleavitt/v3ke-toolchain:2026-06-07

# Build (~20–40 min the first time; crosstool-ng bakes the full MIPS + ARM toolchain).
# After building, push to ghcr and write the registry digest to toolchain/IMAGE_DIGEST.
python3 tools/build.py --runtime docker --image "$TAG" image --push
```

`--push` does three things after a successful build:

1. Pushes the image to `ghcr.io/coreyleavitt/v3ke-toolchain` under the given tag.
2. Inspects the registry to capture the immutable `sha256:<64-hex>` digest.
3. Writes that digest (and a short comment header) into `toolchain/IMAGE_DIGEST`,
   overwriting any previous content.

Use `--runtime podman` if you prefer podman locally (the default); use `--runtime docker` to
match the GitHub Actions runner environment.

---

## Commit the updated digest file

```bash
git add toolchain/IMAGE_DIGEST
git commit -m "toolchain: pin image to <tag>"
git push
```

Once the commit lands on `main`, every subsequent CI run will pull the image by the new digest.
If `IMAGE_DIGEST` still contains the all-zeros placeholder, the `build` and `repro` jobs in
`ci.yaml` and the `release` job in `release.yaml` will fail immediately with an operator-facing
error — this is intentional (fail-closed).

---

## Verify the image ABI (optional but recommended)

After pushing, confirm the image produces artifacts with the correct device ABI before CI
picks it up:

```bash
python3 tools/build.py --runtime docker --image "$TAG" artifacts
./tools/v3ke/v3ke verify
```

Both commands must exit 0.  `v3ke verify` checks the MIPS ELF headers against the Nebula Pad's
expected ABI (mipsel · mips32r2 · o32 · fp64 · nan2008 · `ld-linux-mipsn8.so.1`).

---

## When to rebuild

Rebuild and re-publish the image when `toolchain/Containerfile` or any file under `toolchain/`
changes (e.g. a new crosstool-ng fragment, a glibc patch, or a base-image digest bump).
Routine changes to `tools/`, `docs/`, printer configs, and workflow YAML do **not** require a
new image — the existing digest continues to work.

---

## Package visibility

The first time you push, set the package to **public** so CI can pull without credentials:

> github.com → your profile → Packages → `v3ke-toolchain` → Package settings →
> Change visibility → Public

This is a one-time manual step.  The published image is a **build cache**; the reproducibility
proof (`repro` job in `ci.yaml`) validates that two independent cold builds of the same commit
produce byte-identical artifacts, not that the image layers are reproducible.
