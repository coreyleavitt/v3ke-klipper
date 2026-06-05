## `v3ke flash` — flash the motion MCU (GD32F303 / STM32F103) over SWD via openocd + ST-Link.
## Adds what flash-mcu.sh couldn't safely do: backup-before-erase with RDP/blank detection,
## and read-back-and-compare after every write. Any failure raises and stops (no half-flashed MCU).
import std/[os, osproc, streams, strformat, strutils, sequtils]
import common

const
  DefaultInterface = "interface/stlink-dap.cfg"   # STLINK-V3SET (override: V3KE_INTERFACE)
  Target           = "target/stm32f3x.cfg"        # GD32F303 speaks the stm32f3x target
  FlashBase        = 0x0800_0000
  KatapultAddr     = 0x0800_0000                   # bootloader
  KlipperAddr      = 0x0800_2000                   # after the 8 KiB bootloader
  FlashSize        = 0x0008_0000                   # 512 KiB

proc fwDir(): string = getEnv("FW_DIR", "mcu-firmware")

proc ocd(script: string): string =
  ## Run openocd with our interface+target + a -c command script. No shell => no quoting bugs;
  ## non-zero exit raises instead of silently continuing.
  let iface = getEnv("V3KE_INTERFACE", DefaultInterface)
  let p = startProcess("openocd",
    args = @["-f", iface, "-f", Target, "-c", script],
    options = {poUsePath, poStdErrToStdOut})
  result = p.outputStream.readAll()
  let code = p.waitForExit()
  p.close()
  if code != 0: fail(&"openocd exited {code}:\n{result}")

proc confirm(msg: string) =
  stdout.styledWrite(fgYellow, msg & " [y/N] ")
  if stdin.readLine().strip().toLowerAscii() notin ["y", "yes"]: fail("aborted by user")

proc backupStock(outPath: string) =
  note(&"Backing up stock firmware -> {outPath}")
  discard ocd(&"init; halt; dump_image {outPath} 0x{FlashBase:x} 0x{FlashSize:x}; exit")
  if not fileExists(outPath) or getFileSize(outPath) == 0:
    fail("empty dump — readout protection (RDP) likely enabled")
  let data = readFile(outPath)
  if data.allIt(it == '\xff'): warn("dump is all 0xFF — flash blank or unreadable (RDP?)")
  else: ok(&"backed up {data.len} bytes (not blank)")

proc flashOne(name, path: string; address: int) =
  if not fileExists(path): fail(&"missing {name} image: {path}")
  let want = readFile(path)
  note(&"Flashing {name}: {path} ({want.len} B) -> 0x{address:x}")
  discard ocd(&"init; reset halt; flash write_image erase {path} 0x{address:x}; " &
              &"verify_image {path} 0x{address:x}; reset run; exit")
  # Belt-and-suspenders beyond verify_image: read the region back and byte-compare.
  let tmp = getTempDir() / &"v3ke-{name}.readback"
  discard ocd(&"init; halt; dump_image {tmp} 0x{address:x} 0x{want.len:x}; exit")
  let got = readFile(tmp)
  removeFile(tmp)
  if got != want: fail(&"{name} read-back MISMATCH ({got.len} vs {want.len} B)")
  ok(&"{name} flashed + read-back verified ({want.len} B)")

proc eraseAll() =
  note("Erasing chip...")
  discard ocd(&"adapter speed 100; init; halt; reset halt; " &
              &"flash erase_address 0x{FlashBase:x} 0x60000; " &
              &"flash erase_address 0x{FlashBase + 0x60000:x} 0x20000; exit")

proc flashCmd*(args: seq[string]): int =
  let sub = if args.len >= 1: args[0] else: "help"
  let fw = fwDir()
  case sub
  of "backup":
    backupStock(if args.len >= 2: args[1] else: "stock-mcu-backup.bin")
  of "katapult": flashOne("katapult", fw / "katapult.bin", KatapultAddr)
  of "klipper":  flashOne("klipper",  fw / "klipper.bin",  KlipperAddr)
  of "all":
    backupStock("stock-mcu-backup.bin")
    confirm("About to ERASE and flash the motion MCU (stock backed up). Continue?")
    eraseAll()
    flashOne("katapult", fw / "katapult.bin", KatapultAddr)
    flashOne("klipper",  fw / "klipper.bin",  KlipperAddr)
    ok("Done — power-cycle the printer.")
  else:
    echo "usage: v3ke flash {backup [file] | katapult | klipper | all}"
    return (if sub == "help": 0 else: 1)
  return 0
