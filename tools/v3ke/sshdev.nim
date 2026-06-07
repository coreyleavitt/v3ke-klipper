## SSH helpers for the printer. The device's old dropbear has no sftp/scp, so files are pushed by
## streaming over `ssh <host> 'cat > path'`. Host defaults to the `v3ke` ~/.ssh/config alias.
import std/[os, osproc, streams, strformat]
import common

proc sshHost*(): string = getEnv("V3KE_HOST", "v3ke")

# Pin the host key on first use and refuse a changed key (TOFU). Guards the device-controlled
# command output we interpolate (see deploy.nim) against a LAN MITM. After a firmware reset clears
# the device key this will (correctly) refuse until the user clears the stale known_hosts entry.
const SshOpts = ["-o", "StrictHostKeyChecking=accept-new"]

proc runRemote*(cmd: string): string =
  ## Run a command on the device; return merged stdout/stderr, raise on non-zero exit.
  let host = sshHost()
  let p = startProcess("ssh", args = @[SshOpts[0], SshOpts[1], host, cmd],
                       options = {poUsePath, poStdErrToStdOut})
  result = p.outputStream.readAll()
  let code = p.waitForExit()
  p.close()
  if code != 0: fail(&"ssh {host}: `{cmd}` exited {code}:\n{result}")

proc pushFile*(localPath, remotePath: string) =
  ## Stream a local file to the device (no scp): pipe it into `ssh host 'cat > remote'`. Uses
  ## startProcess (NO local shell) so local paths can't inject; remotePath is shell-quoted for the
  ## device's ash. Reads the whole file into memory first (host artifacts are <1 MB).
  if not fileExists(localPath): fail(&"missing local file: {localPath}")
  let host = sshHost()
  let p = startProcess("ssh",
    args = @[SshOpts[0], SshOpts[1], host, "cat > " & shQuote(remotePath)],
    options = {poUsePath, poStdErrToStdOut})   # fold ssh diagnostics in so a failure isn't silent
  p.inputStream.write(readFile(localPath))
  p.inputStream.close()          # EOF so the remote `cat` finishes
  let diag = p.outputStream.readAll()          # drain (cat emits nothing; this is ssh stderr on error)
  let code = p.waitForExit()
  p.close()
  if code != 0: fail(&"push {localPath} -> {host}:{remotePath} failed (ssh exit {code}):\n{diag}")
  ok(&"pushed {extractFilename(localPath)} ({getFileSize(localPath)} B) -> {remotePath}")
