## Shared helpers for the v3ke CLI: errors, styled terminal output, and POSIX quoting.
## The ELF reader lives in elf.nim (pure, no output side-effects) so it can be tested
## independently without pulling in terminal/I/O machinery.
import std/[strutils, terminal]
export terminal   # so importers can use fgGreen/styledWriteLine without re-importing

type
  V3keError* = object of CatchableError
  UserAbort* = object of CatchableError   ## user declined a confirmation — a clean stop, not an error

proc fail*(msg: string) = raise newException(V3keError, msg)
proc abort*(msg: string) = raise newException(UserAbort, msg)

proc shQuote*(s: string): string =
  ## POSIX single-quote escaping, for safely interpolating a value into a *remote* shell command
  ## (the device runs the string through BusyBox ash). Wraps in '...', escaping embedded quotes.
  "'" & s.replace("'", "'\\''") & "'"

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
