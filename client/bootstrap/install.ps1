# Firekeep client bootstrap (Windows). Mirrors bootstrap/install.sh step for step:
# resolve the version, fetch this version's SHA256SUMS once, checksum-verify EVERY
# executable artifact (uv.exe AND the wheel) against it before either is used, provision a
# standalone CPython into ~/.firekeep/venv, install the wheel BY LOCAL FILE PATH, hand off to
# the wizard.
#
# No stdin trap here, unlike the POSIX side: `irm <url> | iex` keeps the console attached to
# the PowerShell process running this script (unlike `curl | sh`, where piping makes the
# SCRIPT ITSELF stdin and silently defeats the wizard's prompts). There is no /dev/tty
# equivalent to reattach and none is needed — do not "fix" this asymmetry.
$ErrorActionPreference = 'Stop'

$FirekeepHome = Join-Path $env:USERPROFILE '.firekeep'
$Venv      = Join-Path $FirekeepHome 'venv'
$Bin       = Join-Path $FirekeepHome 'bin'
$PythonVersion = '3.12'

function Die($msg) {
    # [Console]::Error.WriteLine, not Write-Error: with $ErrorActionPreference = 'Stop',
    # Write-Error becomes a terminating error and throws immediately, so `exit 1` below
    # would depend on however the host maps an unhandled terminating error to a process
    # exit code (host- and version-dependent, and not something verifiable off Windows).
    # Writing directly to stderr keeps the fail-loud contract (message on stderr, nonzero
    # exit) unconditional, mirroring the POSIX die()'s plain `echo >&2; exit 1`.
    [Console]::Error.WriteLine("firekeep: $msg")
    exit 1
}

# Baked at release time by make_release.py --dist-base (mirrors install.sh):
# the published copy carries its release URL so `irm .../install.ps1 | iex`
# needs no $env: line. Env var overrides; repo copy keeps the placeholder.
$DistBaseDefault = '__FIREKEEP_DIST_BASE_DEFAULT__'
$Placeholder = '__FIREKEEP_DIST_BASE_' + 'DEFAULT__'
if ((-not $env:FIREKEEP_DIST_BASE) -and ($DistBaseDefault -ne $Placeholder)) {
    $env:FIREKEEP_DIST_BASE = $DistBaseDefault
}
if (-not $env:FIREKEEP_DIST_BASE) {
    Die "FIREKEEP_DIST_BASE is not set - this script must be fetched from a release"
}
$Base = $env:FIREKEEP_DIST_BASE.TrimEnd('/')

# --- TLS trust for corporate networks (mirrors install.sh; see its comment) --
# Unlike the POSIX side (`curl | sh` runs in a child shell), the documented
# `irm <url> | iex` invocation runs this script IN the caller's PowerShell session, so
# these env changes would outlive the install. Capture the prior state here and restore
# it at the end of the script (after the firekeep install hand-off). Best-effort: Die's
# exit paths skip the restore — acceptable, an aborted install leaves the change only
# for the current session, and the warning below has already named it.
$HadUvNativeTls     = [bool]$env:UV_NATIVE_TLS
$OrigSslCertFile    = $env:SSL_CERT_FILE
$RemovedSslCertFile = $false
$env:UV_NATIVE_TLS = '1'
if ($env:SSL_CERT_FILE -and -not $env:FIREKEEP_KEEP_SSL_CERT_FILE) {
    [Console]::Error.WriteLine("firekeep: SSL_CERT_FILE is set; ignoring it for this install and using the OS trust store (set FIREKEEP_KEEP_SSL_CERT_FILE=1 to keep it)")
    Remove-Item Env:SSL_CERT_FILE
    $RemovedSslCertFile = $true
}

New-Item -ItemType Directory -Force -Path $Bin | Out-Null

# --- 1. resolve the version to install ---------------------------------------
# $Base is VERSION-AGNOSTIC: that is what makes latest.json a real moving pointer instead of
# a self-referential one. Every version keeps its own directory under $Base, so a pinned
# FIREKEEP_VERSION (the --to rollback path) still resolves to that version's own artifacts
# rather than 404ing.
if ($env:FIREKEEP_VERSION) {
    $V = $env:FIREKEEP_VERSION
} else {
    try {
        $Manifest = Invoke-RestMethod -UseBasicParsing -Uri "$Base/latest/latest.json"
    } catch {
        Die "download failed: $Base/latest/latest.json"
    }
    $V = $Manifest.version
    if (-not $V) { Die "latest.json has no version" }
}
$VBase = "$Base/$V"
$WheelName = "firekeep_client-$V-py3-none-any.whl"
$FirekeepExe  = Join-Path $Venv 'Scripts\firekeep.exe'
# FIREKEEP_RUNTIME targets ONE agent (claude|codex|kiro|opencode) and is forwarded as --runtime.
# When UNSET we pass nothing, so the client installs every shipped adapter.
# An explicit environment override still wins.
$RuntimeArgs = if ($env:FIREKEEP_RUNTIME) { @('--runtime', $env:FIREKEEP_RUNTIME) } else { @() }
$JoinArgs = if ($env:FIREKEEP_JOIN) { @('--join', $env:FIREKEEP_JOIN) } else { @() }

# Detect an existing install independently of the fast path. Version-changing updates and
# FIREKEEP_FORCE_REINSTALL both rebuild below, but their final hand-off must reuse the
# existing connection instead of prompting for it again. Join codes are zero-prompt too.
$Installed = ''
if (Test-Path $FirekeepExe) {
    try {
        $Installed = (& (Join-Path $Venv 'Scripts\python.exe') -c "import firekeep_client; print(firekeep_client.__version__)" 2>$null | Out-String).Trim()
    } catch { $Installed = '' }
}
[string[]]$NonInteractiveArgs = @()
if ($Installed -or $env:FIREKEEP_JOIN) {
    $NonInteractiveArgs = @('--non-interactive')
}

# --- idempotent fast path: already at $V -> re-render only, NO venv rebuild -------
# Re-running the bootstrap when already current (e.g. to re-target a runtime via
# FIREKEEP_RUNTIME) must NOT rebuild the venv: `uv venv --clear` below fails on Windows against
# the running MCP servers/shims of every live agent session (the in-use guard is what refuses
# it, correctly). If the installed version already equals $V, skip fetch+provision+install and
# hand straight to the wizard, which only RE-RENDERS adapters (never touches the venv — see the
# 0.1.3 fix). This makes a re-run while agents are live succeed. FIREKEEP_FORCE_REINSTALL=1 forces
# the full reinstall path without changing the non-interactive update hand-off.
if (($Installed -eq $V) -and -not $env:FIREKEEP_FORCE_REINSTALL) {
    Write-Host "firekeep: already at $V - re-rendering adapters, no venv rebuild. Set FIREKEEP_FORCE_REINSTALL=1 to force a full reinstall."
    & $FirekeepExe install --dist-base $Base @RuntimeArgs @JoinArgs @NonInteractiveArgs
    $FirekeepExit = $LASTEXITCODE
    if (-not $HadUvNativeTls) { Remove-Item Env:UV_NATIVE_TLS -ErrorAction SilentlyContinue }
    if ($RemovedSslCertFile) { $env:SSL_CERT_FILE = $OrigSslCertFile }
    exit $FirekeepExit
}

# --- 2. this version's SHA256SUMS, fetched once ------------------------------
$SumsPath = Join-Path $Bin 'SHA256SUMS'
try {
    Invoke-WebRequest -UseBasicParsing -Uri "$VBase/SHA256SUMS" -OutFile $SumsPath
} catch {
    Die "download failed: $VBase/SHA256SUMS"
}

# Verify a local file against this SHA256SUMS. Shared by uv.exe AND the wheel — a SECOND,
# subtly different verifier is how the wheel got skipped once already.
#
# Distinguish a MISSING SHA256SUMS entry from a MISMATCHED one. `Select-String` returning
# nothing must be handled explicitly here — left unchecked, $Want would silently be $null,
# and the comparison below would report a bogus "checksum mismatch: expected , got <hash>"
# for a target that was simply never listed. This is the PowerShell mirror of the POSIX
# `grep | cut` bug (a masked exit status hid a missing entry behind that exact message).
function Verify-AgainstSums($File, $Name) {
    $Match = Select-String -Path $SumsPath -Pattern " $([regex]::Escape($Name))$"
    if (-not $Match) {
        Remove-Item $File -Force -ErrorAction SilentlyContinue
        Die "no SHA256SUMS entry for $Name"
    }
    # Select-Object -First 1 normalizes to a single MatchInfo even if Select-String somehow
    # returned more than one line, so `.Line` below is never an array whose `.Split` would
    # apply element-wise instead of to the matched line itself.
    $Want = ($Match | Select-Object -First 1).Line.Split(' ')[0]
    $Got = (Get-FileHash -Algorithm SHA256 -Path $File).Hash.ToLower()
    if ($Want.ToLower() -ne $Got) {
        Remove-Item $File -Force -ErrorAction SilentlyContinue
        Die "checksum mismatch for $Name`: expected $Want, got $Got"
    }
}

# --- 3. uv, checksum-verified BEFORE we execute it ---------------------------
# This binary is fetched over unauthenticated HTTP and then RUN. The checksum is the only
# thing standing between this machine and someone else's code. Windows is not the soft
# target: verify before it is moved into place or invoked, same as the POSIX side.
$Target = 'x86_64-pc-windows-msvc'
Write-Host "firekeep: fetching uv ($Target)"
$UvTmp = Join-Path $Bin 'uv.tmp.exe'
try {
    Invoke-WebRequest -UseBasicParsing -Uri "$VBase/uv-$Target.exe" -OutFile $UvTmp
} catch {
    Die "download failed: $VBase/uv-$Target.exe"
}
Verify-AgainstSums $UvTmp "uv-$Target.exe"
$Uv = Join-Path $Bin 'uv.exe'
Move-Item -Force $UvTmp $Uv

# --- 4. the wheel, fetched then checksum-verified BEFORE it is installed -----
# Never by name: `firekeep-client` on PyPI belongs to a third party. `uv pip install <url>` does
# NO hash checking at all — that was the hole C2 lived in. Fetch to a local file and verify
# it with the SAME helper as uv.exe, BEFORE the venv even exists, so a tampered wheel never
# reaches `uv pip install`.
Write-Host "firekeep: fetching $WheelName"
$WheelPath = Join-Path $Bin $WheelName
try {
    Invoke-WebRequest -UseBasicParsing -Uri "$VBase/$WheelName" -OutFile $WheelPath
} catch {
    Die "download failed: $VBase/$WheelName"
}
Verify-AgainstSums $WheelPath $WheelName

# --- 5. standalone CPython + venv, only now that uv is verified --------------
# -Clear is required, not cosmetic: `firekeep update` re-runs this exact script against the
# SAME ~/.firekeep/venv that is already running it. Without it, `uv venv` refuses to recreate
# an existing venv ("A virtual environment already exists ... Use --clear") and every real
# (non--check) update would die right here — a fresh install has nothing to clear, so this
# is a no-op there.
# Refuse to replace a venv that is still RUNNING. Windows cannot delete a directory whose
# executables are executing, and every live agent session runs the kit's stdio MCP servers
# (firekeep-decision, firekeep-symdex, shims) from this venv — without this guard, uv dies
# mid-install with a bare "failed to remove directory ... Access is denied. (os error 5)".
# Name the holders so the operator knows WHAT to close. The `firekeep update` self-lock is
# already handled upstream (cli.py spawns this script detached and exits), so any process
# found here is a genuinely live session, not the updater. Best-effort by nature (a process
# can appear after the check); uv's own failure below stays as the backstop. POSIX has no
# twin: unlink() on a running executable succeeds there, so install.sh deliberately omits
# this — same kind of asymmetry as the stdin trap (see the file header).
# The try/catch inside the filter is required: $_.Path can THROW on protected processes
# (access denied on MainModule), and under $ErrorActionPreference='Stop' that would kill the
# whole install while merely scanning the process list.
if (Test-Path $Venv) {
    $Holders = @(Get-Process | Where-Object {
        try { $p = $_.Path } catch { $p = $null }
        $p -and $p.StartsWith($Venv, [System.StringComparison]::OrdinalIgnoreCase)
    })
    if ($Holders.Count -gt 0) {
        $List = ($Holders | ForEach-Object { "$($_.ProcessName) (pid $($_.Id))" }) -join ', '
        Die "$Venv is in use by: $List - close the agent sessions running the firekeep kit (Claude Code, kiro, sidecar), then re-run this installer"
    }
}

# only-managed is load-bearing, not an optimization: default discovery walks the PATH and
# hard-fails querying the zero-byte WindowsApps python3.exe app-execution alias (a dangling
# APPEXECLINK stub — "Failed to inspect Python interpreter ... os error 3") on machines with
# the Store PythonManager installed. The contract here is a STANDALONE CPython regardless, so
# never let discovery bind the venv to whatever system Python it finds first.
Write-Host "firekeep: provisioning Python $PythonVersion"
& $Uv venv $Venv --python $PythonVersion --python-preference only-managed --clear
if ($LASTEXITCODE -ne 0) { Die "could not provision Python $PythonVersion" }

# --- 6. install the wheel BY LOCAL FILE PATH, never a URL --------------------
Write-Host "firekeep: installing $WheelName"
& $Uv pip install --python $Venv --reinstall $WheelPath
if ($LASTEXITCODE -ne 0) { Die "wheel install failed" }

# --- 6b. symdex wheel: ALWAYS installed, same fetch -> verify -> local-path dance ---
$SymdexMatch = Select-String -Path $SumsPath -Pattern 'firekeep_symdex-[0-9][^ ]*\.whl' | Select-Object -First 1
if (-not $SymdexMatch) { Die "SHA256SUMS lists no firekeep_symdex wheel - release is incomplete" }
$SymdexWheel = $SymdexMatch.Matches[0].Value
Write-Host "firekeep: fetching $SymdexWheel"
$SymdexPath = Join-Path $Bin $SymdexWheel
try {
    Invoke-WebRequest -UseBasicParsing -Uri "$VBase/$SymdexWheel" -OutFile $SymdexPath
} catch {
    Die "download failed: $VBase/$SymdexWheel"
}
Verify-AgainstSums $SymdexPath $SymdexWheel
Write-Host "firekeep: installing $SymdexWheel"
& $Uv pip install --python $Venv --reinstall $SymdexPath
if ($LASTEXITCODE -ne 0) { Die "symdex wheel install failed" }

# --- 7. hand off to the wizard -------------------------------------------------
# See the file-header note: no stdin trap and no /dev/tty equivalent needed on this path.
# @RuntimeArgs = --runtime <FIREKEEP_RUNTIME> when set, else empty for all adapters.
& $FirekeepExe install --dist-base $Base @RuntimeArgs @JoinArgs @NonInteractiveArgs
$FirekeepExit = $LASTEXITCODE

# --- restore the caller's TLS env (see the TLS block above) -------------------
# Under `irm | iex` this ran in the caller's session; put back what the TLS block
# changed so UV_NATIVE_TLS / a cleared SSL_CERT_FILE do not outlive the install.
# Env-only operations never touch $LASTEXITCODE, but the hand-off's exit code is
# captured above anyway so nothing here can launder it.
if (-not $HadUvNativeTls) { Remove-Item Env:UV_NATIVE_TLS -ErrorAction SilentlyContinue }
if ($RemovedSslCertFile) { $env:SSL_CERT_FILE = $OrigSslCertFile }

exit $FirekeepExit
