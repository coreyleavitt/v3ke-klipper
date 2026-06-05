## SSH helpers for the printer. The device's old dropbear has no sftp/scp, so files are pushed by
## streaming over `ssh <host> 'cat > path'`. Host defaults to the `v3ke` ~/.ssh/config alias.
import std/[os, osproc, streams, strformat]
import common

proc sshHost*(): string = getEnv("V3KE_HOST", "v3ke")

proc runRemote*(cmd: string): string =
  ## Run a command on the device; return merged stdout/stderr, raise on non-zero exit.
  let host = sshHost()
  let p = startProcess("ssh", args = @[host, cmd], options = {poUsePath, poStdErrToStdOut})
  result = p.outputStream.readAll()
  let code = p.waitForExit()
  p.close()
  if code != 0: fail(&"ssh {host}: `{cmd}` exited {code}:\n{result}")

proc pushFile*(localPath, remotePath: string) =
  ## Stream a local file to the device (no scp): `ssh host 'cat > remote' < local` via the local
  ## shell (deadlock-proof for large binaries; klipper_mcu.elf is ~740 KB).
  if not fileExists(localPath): fail(&"missing local file: {localPath}")
  let host = sshHost()
  let remoteCmd = "cat > '" & remotePath & "'"
  let cmd = &"ssh {quoteShell(host)} {quoteShell(remoteCmd)} < {quoteShell(localPath)}"
  if execShellCmd(cmd) != 0: fail(&"push {localPath} -> {host}:{remotePath} failed")
  ok(&"pushed {extractFilename(localPath)} ({getFileSize(localPath)} B) -> {remotePath}")
