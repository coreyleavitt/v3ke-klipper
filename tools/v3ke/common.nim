## Shared helpers for the v3ke CLI: errors, styled output, and a minimal ELF32-LE reader
## (so `verify` needs no external readelf — works on a bare user machine, incl. Windows).
import std/[strformat, strutils, terminal]
export terminal   # so importers can use fgGreen/styledWriteLine without re-importing

type V3keError* = object of CatchableError

proc fail*(msg: string) = raise newException(V3keError, msg)

proc styled(f: File, color: ForegroundColor, prefix, msg: string) =
  ## Color only on a real terminal; plain text when piped/redirected (clean logs).
  if isatty(f): f.styledWriteLine(color, prefix, resetStyle, msg)
  else:         f.writeLine(prefix & msg)
  f.flushFile()   # keep stdout/stderr ordering sane when interleaved

proc ok*(msg: string)       = styled(stdout, fgGreen,  "  ok:   ", msg)
proc warn*(msg: string)     = styled(stdout, fgYellow, "  warn: ", msg)
proc note*(msg: string)     = stdout.writeLine("  " & msg); stdout.flushFile()
proc errln*(msg: string)    = styled(stderr, fgRed,    "ERROR: ", msg)
proc okBanner*(msg: string) = styled(stdout, fgGreen,  "", msg)

# --- minimal ELF32 little-endian reader (no readelf dependency) -------------------------------
type ElfInfo* = object
  etype*:   uint16   ## 2 = EXEC, 3 = DYN (shared lib / PIE)
  machine*: uint16   ## 8 = EM_MIPS
  flags*:   uint32   ## e_flags (MIPS ABI/arch/nan bits)
  interp*:  string   ## PT_INTERP string; "" for shared libs (no interpreter)

proc u16(b: string, o: int): uint16 =
  uint16(ord(b[o])) or (uint16(ord(b[o+1])) shl 8)
proc u32(b: string, o: int): uint32 =
  uint32(ord(b[o])) or (uint32(ord(b[o+1])) shl 8) or
  (uint32(ord(b[o+2])) shl 16) or (uint32(ord(b[o+3])) shl 24)

proc readElf*(path: string): ElfInfo =
  let b = readFile(path)
  if b.len < 52 or b[0..3] != "\x7fELF": fail(&"{path}: not an ELF file")
  if ord(b[4]) != 1 or ord(b[5]) != 1:   fail(&"{path}: expected 32-bit little-endian ELF")
  result.etype   = u16(b, 16)
  result.machine = u16(b, 18)
  result.flags   = u32(b, 36)
  # Walk the program headers for PT_INTERP (p_type == 3) to recover the dynamic loader.
  let phoff = u32(b, 28).int
  let phentsize = u16(b, 42).int
  let phnum = u16(b, 44).int
  for i in 0 ..< phnum:
    let p = phoff + i * phentsize
    if p + 20 > b.len: break
    if u32(b, p) == 3'u32:                 # PT_INTERP
      let off = u32(b, p + 4).int
      let sz  = u32(b, p + 16).int
      if off + sz <= b.len:
        result.interp = b[off ..< off + sz].strip(chars = {'\0'})
      break
