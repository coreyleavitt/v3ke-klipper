# `tools/abi/` — shared ABI ground truth

Language-neutral home for the device-ABI spec the build pipeline checks artifacts against.
Both the Python build-side checker (`build/elf.py`, added in A1) and the Nim operator-side
checker (`tools/v3ke/elf.nim`, added in A1c) are tested against the **same** fixtures here, so
the two implementations can't silently drift (RFC §3 G2).

- `abi_spec.py` — the table-driven `DEVICE_ABI` constants + accepted `fp_abi` set (added in A1).
- `fixtures/` — golden ELFs: one known-good, plus a per-flag set of known-bad files (one wrong
  in exactly machine / endianness / nan2008 / o32 / mips32r2 / fp_abi / loader).
