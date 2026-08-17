# Firekeep client bootstrap (Windows). Mirrors bootstrap/install.sh step for step:
# resolve the version, fetch this version's SHA256SUMS once, checksum-verify EVERY
# executable artifact (uv.exe AND the wheel) against it before either is used, provision a
# standalone CPython into ~/.firekeep/venvs/<version>, install the wheel BY LOCAL FILE
# PATH, flip the ~/.firekeep/current junction to the new venv, hand off to the wizard.
#
# SIDE-BY-SIDE VENVS (client 0.1.35). Each version's venv is provisioned AT its final
# path venvs/<version> and never moved — a uv venv is not relocatable (pyvenv.cfg and
# every console-script interpreter line bake the absolute path; the 0.1.26 rename
# attempt died on exactly that, see install.sh's history note). `current` is an NTFS
# junction (works without admin, unlike a directory symlink) that every rendered
# surface routes through. Updating therefore NEVER touches the venv live sessions run
# from: their open handles pin the real files, not the link. This retires the old
# in-place `uv venv --clear` rebuild and with it the "close every agent session"
# requirement and the wall of holder PIDs it printed.
#
# No stdin trap here, unlike the POSIX side: `irm <url> | iex` keeps the console attached to
# the PowerShell process running this script (unlike `curl | sh`, where piping makes the
# SCRIPT ITSELF stdin and silently defeats the wizard's prompts). There is no /dev/tty
# equivalent to reattach and none is needed — do not "fix" this asymmetry.
$ErrorActionPreference = 'Stop'

$FirekeepHome = Join-Path $env:USERPROFILE '.firekeep'
$Venvs      = Join-Path $FirekeepHome 'venvs'
$Current    = Join-Path $FirekeepHome 'current'
$LegacyVenv = Join-Path $FirekeepHome 'venv'
$Bin        = Join-Path $FirekeepHome 'bin'
$PythonVersion = '3.12'

# --- module-resolution self-defense ------------------------------------------
# This script runs under Windows PowerShell 5.1 when spawned by `firekeep update`,
# and 5.1 must not inherit a PSModulePath built by PowerShell 7: pwsh's module dirs
# lead the list, 5.1 autoloads Microsoft.PowerShell.Utility 7.0.0.0 over its own
# 3.1.0.0, and that module binds `Select-String` under 5.1 but NOT `Get-FileHash` —
# so the failure lands precisely on the checksum gate and reads like a broken
# Windows install. cli.py strips the variable on its side; this is the script's own
# defense for direct runs from a pwsh prompt. Restored on the exit paths below:
# under `irm | iex` we run IN the caller's session (see the TLS block).
$OrigPSModulePath = $env:PSModulePath
$env:PSModulePath = Join-Path $PSHOME 'Modules'

function Die($msg) {
    # [Console]::Error.WriteLine, not Write-Error: with $ErrorActionPreference = 'Stop',
    # Write-Error becomes a terminating error and throws immediately, so `exit 1` below
    # would depend on however the host maps an unhandled terminating error to a process
    # exit code (host- and version-dependent, and not something verifiable off Windows).
    # Writing directly to stderr keeps the fail-loud contract (message on stderr, nonzero
    # exit) unconditional, mirroring the POSIX die()'s plain `echo >&2; exit 1`.
    [Console]::Error.WriteLine("firekeep: $msg")
    # Under `irm | iex` this script runs IN the caller's session: put back the
    # module path the self-defense block trimmed, even on the failure exits.
    # (Assigning $null removes the variable, which is also the correct restore
    # when it was never set — 5.1 then rebuilds its default from the registry.)
    $env:PSModulePath = $OrigPSModulePath
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

# --- side-by-side layout helpers ---------------------------------------------

# Point the `current` junction at a venv. The removal is `cmd /c rmdir` ON
# PURPOSE: it deletes the LINK NODE only. Never a recursive delete here — a
# traversal that follows the reparse point deletes the target venv's files
# (probed live: Remove-Item -Recurse on current PS builds happens to stop at
# the link, but ancient 5.1 builds recursed into the target, and rmdir is
# unambiguous on every build). The two-step flip leaves a millisecond window
# with no `current`; a spawn in that window fails file-not-found and its retry
# succeeds — hooks fail open by design. (Compare: the old in-place rebuild left
# NO venv at all for the 30-120s of reprovisioning.)
function Set-CurrentJunction($Target) {
    if (Test-Path $Current) {
        # Already pointing at the target -> no flip, no window at all.
        $Existing = ((Get-Item $Current -Force).Target | Select-Object -First 1)
        if ("$Existing" -eq "$Target") { return }
        cmd /c rmdir "$Current"
        if ($LASTEXITCODE -ne 0) { Die "could not remove the old 'current' link at $Current" }
    }
    New-Item -ItemType Junction -Path $Current -Target $Target | Out-Null
    Write-Host "firekeep: current -> venvs/$(Split-Path -Leaf $Target)"
}

# Garbage-collect venvs nothing can use anymore: every versioned venv except the
# one `current` points at and the next-newest other version (kept as an instant
# rollback target), plus the legacy pre-0.1.35 ~/.firekeep/venv, plus leftovers
# of interrupted GCs. The liveness test is a RENAME-PROBE, not process
# enumeration: renaming a directory with open files anywhere beneath it fails
# atomically with no partial state, whereas a recursive delete guts the venv
# BEFORE hitting the first locked exe (probed live — "delete failed" would mean
# "venv now corrupt"). Rename succeeding proves nothing holds it; then the
# delete is safe. Rename failing means live sessions still run from it — skip
# with one line, a future update re-sweeps. Never a wall of PIDs.
function Remove-StaleVenvs {
    $KeepNames = @((Split-Path -Leaf $TargetVenv))
    $Others = @(Get-ChildItem -Path $Venvs -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne (Split-Path -Leaf $TargetVenv) -and $_.Name -notlike '*.gc' })
    if ($Others.Count -gt 0) {
        $Previous = ($Others | Sort-Object { try { [version]$_.Name } catch { [version]'0.0.0' } } | Select-Object -Last 1).Name
        $KeepNames += $Previous
    }
    $Candidates = @()
    $Candidates += @(Get-ChildItem -Path $Venvs -Directory -ErrorAction SilentlyContinue |
        Where-Object { $KeepNames -notcontains $_.Name })
    # The legacy venv is only GC-able after a FULL adapter re-render: with
    # FIREKEEP_RUNTIME set, the wizard re-rendered ONE runtime, and the other
    # three runtimes' configs still embed absolute ~/.firekeep/venv paths —
    # deleting it would break every runtime the targeted render didn't touch.
    if (-not $env:FIREKEEP_RUNTIME) {
        if (Test-Path $LegacyVenv) { $Candidates += Get-Item $LegacyVenv }
    }
    if (Test-Path "$LegacyVenv.gc") { $Candidates += Get-Item "$LegacyVenv.gc" }
    foreach ($Dir in $Candidates) {
        $Probe = "$($Dir.FullName).gc"
        if ($Dir.Name -notlike '*.gc') {
            try {
                Rename-Item -Path $Dir.FullName -NewName (Split-Path -Leaf $Probe) -ErrorAction Stop
            } catch {
                Write-Host "firekeep: kept $($Dir.Name) - still in use by open agent sessions; a future update will remove it"
                continue
            }
        } else {
            $Probe = $Dir.FullName  # a crashed earlier GC already renamed it
        }
        try {
            Remove-Item -Recurse -Force $Probe -ErrorAction Stop
            Write-Host "firekeep: removed old venv $($Dir.Name)"
        } catch {
            Write-Host "firekeep: could not finish removing $($Dir.Name) (will retry on a future update)"
        }
    }
}

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
$TargetVenv = Join-Path $Venvs $V
# The wizard hand-off runs through `current` — the same alias every rendered
# surface uses — so what we hand to is provably what sessions will launch.
$FirekeepExe = Join-Path $Current 'Scripts\firekeep.exe'
# FIREKEEP_RUNTIME targets ONE agent (claude|codex|kiro|opencode) and is forwarded as --runtime.
# When UNSET we pass nothing, so the client installs every shipped adapter.
# An explicit environment override still wins.
$RuntimeArgs = if ($env:FIREKEEP_RUNTIME) { @('--runtime', $env:FIREKEEP_RUNTIME) } else { @() }
$JoinArgs = if ($env:FIREKEEP_JOIN) { @('--join', $env:FIREKEEP_JOIN) } else { @() }

# Probe the version a venv actually holds.
# -I (isolated) is load-bearing, not tidiness. `python -c` puts the CURRENT
# WORKING DIRECTORY on sys.path[0], so running this from a checkout's client/
# directory -- the likeliest place a developer runs an update from -- imports
# the SOURCE TREE and reports its version instead of the venv's. When that
# misread happens to equal $V, the fast path below skips the whole install and
# prints "already at $V": the update silently does nothing while reporting
# success. MEASURED: a venv holding 0.1.33 reported 0.1.34 from client/, and
# `firekeep update` no-opped. -I also drops PYTHONPATH and user site-packages,
# the other two ways the caller's environment can shadow what is INSTALLED.
function Get-VenvVersion($VenvPath) {
    $Py = Join-Path $VenvPath 'Scripts\python.exe'
    if (-not (Test-Path $Py)) { return '' }
    try {
        return (& $Py -I -c "import firekeep_client; print(firekeep_client.__version__)" 2>$null | Out-String).Trim()
    } catch { return '' }
}

# The fast path's health probe must prove the venv is COMPLETE, not merely that
# the client package imports. An install killed between the client wheel and
# either dex wheel leaves a venv whose python happily reports $V — accepting that
# would flip `current` to a half-installed venv and, worse, keep taking the fast
# path on every later run, so the breakage would never route back through the
# full provision that repairs it. firekeep.exe + every bundled package's import
# is the same bar the full path's runnable check enforces before it ever flips.
# Every wheel this script always installs belongs here; a wheel added to the
# install steps but not to this probe is a half-install the fast path accepts.
function Test-VenvComplete($VenvPath) {
    if (-not (Test-Path (Join-Path $VenvPath 'Scripts\firekeep.exe'))) { return $false }
    $Py = Join-Path $VenvPath 'Scripts\python.exe'
    if (-not (Test-Path $Py)) { return $false }
    & $Py -I -c "import firekeep_client, firekeep_symdex, firekeep_docdex" 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

# Detect an existing install independently of the fast path. Version-changing updates and
# FIREKEEP_FORCE_REINSTALL both provision below, but their final hand-off must reuse the
# existing connection instead of prompting for it again. Join codes are zero-prompt too.
# `current` is the layout's truth; the legacy single venv marks a pre-0.1.35 install
# mid-migration.
$Installed = Get-VenvVersion $Current
if (-not $Installed) { $Installed = Get-VenvVersion $LegacyVenv }
[string[]]$NonInteractiveArgs = @()
if ($Installed -or $env:FIREKEEP_JOIN) {
    $NonInteractiveArgs = @('--non-interactive')
}

# --- idempotent fast path: a healthy venvs/<V> already exists -> flip + re-render ---
# One rule covers three cases: a re-run while already current (e.g. re-targeting a
# runtime via FIREKEEP_RUNTIME), a crash between flip and wizard (re-run heals), and
# an instant ROLLBACK (`firekeep update --to <prev>` while venvs/<prev> survives GC —
# zero downloads, just a flip). "Healthy" means the venv's OWN python reports $V;
# a partial venv from an interrupted install fails the probe and takes the full
# provision path below, which rebuilds it with --clear. FIREKEEP_FORCE_REINSTALL=1
# forces the full path without changing the non-interactive hand-off.
if (((Get-VenvVersion $TargetVenv) -eq $V) -and (Test-VenvComplete $TargetVenv) -and -not $env:FIREKEEP_FORCE_REINSTALL) {
    Write-Host "firekeep: venvs/$V is already provisioned - selecting it and re-rendering adapters. Set FIREKEEP_FORCE_REINSTALL=1 to force a full reinstall."
    Set-CurrentJunction $TargetVenv
    & $FirekeepExe install --dist-base $Base @RuntimeArgs @JoinArgs @NonInteractiveArgs
    $FirekeepExit = $LASTEXITCODE
    Remove-StaleVenvs
    if (-not $HadUvNativeTls) { Remove-Item Env:UV_NATIVE_TLS -ErrorAction SilentlyContinue }
    if ($RemovedSslCertFile) { $env:SSL_CERT_FILE = $OrigSslCertFile }
    $env:PSModulePath = $OrigPSModulePath
    exit $FirekeepExit
}

# --- 2. this version's SHA256SUMS ---------------------------------------------
# Mirrors install.sh step 3: on the `firekeep update` path the CLIENT already fetched
# and signature-verified this version's SHA256SUMS against its pinned key, and hands
# the VERIFIED bytes through FIREKEEP_SUMS_FILE. Using that file — and NOT fetching
# again — closes the two-fetch split (a host can serve different bytes to the
# client's urllib fetch and this script's own web request). Honoured only alongside
# FIREKEEP_VERSION, the shape only the client's hand-off produces; a manual
# `irm | iex` run sets neither and fetches as before. Set-but-unusable is fatal —
# a silent fallback to the network would BE the vulnerability.
$SumsPath = Join-Path $Bin 'SHA256SUMS'
$SumsHanded = $false
if ($env:FIREKEEP_SUMS_FILE -and $env:FIREKEEP_VERSION) {
    if (-not (Test-Path -PathType Leaf $env:FIREKEEP_SUMS_FILE)) {
        Die "FIREKEEP_SUMS_FILE is set but not readable: $($env:FIREKEEP_SUMS_FILE)"
    }
    try {
        Copy-Item $env:FIREKEEP_SUMS_FILE $SumsPath -Force
    } catch {
        Die "cannot copy FIREKEEP_SUMS_FILE into place: $($env:FIREKEEP_SUMS_FILE)"
    }
    $SumsHanded = $true
    Write-Host "firekeep: using signature-verified SHA256SUMS handed by firekeep update (no re-fetch)"
} else {
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$VBase/SHA256SUMS" -OutFile $SumsPath
    } catch {
        Die "download failed: $VBase/SHA256SUMS"
    }
}

# --- 2b. BEST-EFFORT minisign verification of SHA256SUMS ---------------------
# Mirrors install.sh step 3b — see its comment and docs/RELEASE-SIGNING.md for the
# honest scope: TOFU on a first install, real protection on the `firekeep update` re-exec
# path (the client verified this script against a signed SHA256SUMS before running it).
# Skips silently without a minisign binary or a baked/provided key; warns when the
# release publishes no .minisig; a PRESENT signature that fails to verify is fatal.
# Skipped entirely under a handed FIREKEEP_SUMS_FILE (mirrors install.sh 3b): the
# client already did the authoritative verification against ITS pinned key, and a
# handed file means NO network round trips on this path — not even for the .minisig.
$SigningPubDefault = '__FIREKEEP_SIGNING_PUB_DEFAULT__'
$SigPlaceholder = '__FIREKEEP_SIGNING_PUB_' + 'DEFAULT__'
if ($SigningPubDefault -eq $SigPlaceholder) { $SigningPubDefault = '' }
$SigningPub = if ($env:FIREKEEP_SIGNING_PUB) { $env:FIREKEEP_SIGNING_PUB } else { $SigningPubDefault }
$Minisign = Get-Command minisign -ErrorAction SilentlyContinue
if ((-not $SumsHanded) -and $SigningPub -and $Minisign) {
    $SigPath = Join-Path $Bin 'SHA256SUMS.minisig'
    $SigFetched = $true
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$VBase/SHA256SUMS.minisig" -OutFile $SigPath
    } catch {
        $SigFetched = $false
        [Console]::Error.WriteLine("firekeep: WARNING: release $V is not signed (no SHA256SUMS.minisig); relying on TLS + checksums")
    }
    if ($SigFetched) {
        & $Minisign.Source -Vq -m $SumsPath -x $SigPath -P $SigningPub
        if ($LASTEXITCODE -ne 0) { Die "SHA256SUMS signature verification FAILED - refusing to install (possible release-host compromise)" }
        Write-Host "firekeep: SHA256SUMS signature verified (minisign)"
    }
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

# --- 4b. the symdex wheel, fetched and verified BEFORE anything destructive ---
# Deliberately hoisted above provisioning: when venvs/<V> is the venv `current`
# points at (forced reinstall / repair of the running version), the --clear
# below destroys it — so every network fetch and every checksum must already
# have succeeded by then. A download or verification failure must never strand
# `current` on a gutted venv.
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

# --- 4c. the docdex wheel, same hoist and the same reasoning as 4b -----------
# Docdex (the documents dex) is bundled exactly like symdex: name and hash read
# from SHA256SUMS, fetched to a local file, verified — all of it above the
# --clear, because ONE bundled wheel fetched below that line is enough to strand
# `current` on a gutted venv. It versions independently of the client AND of
# symdex, so the release names the exact wheel and this script never guesses it.
$DocdexMatch = Select-String -Path $SumsPath -Pattern 'firekeep_docdex-[0-9][^ ]*\.whl' | Select-Object -First 1
if (-not $DocdexMatch) { Die "SHA256SUMS lists no firekeep_docdex wheel - release is incomplete" }
$DocdexWheel = $DocdexMatch.Matches[0].Value
Write-Host "firekeep: fetching $DocdexWheel"
$DocdexPath = Join-Path $Bin $DocdexWheel
try {
    Invoke-WebRequest -UseBasicParsing -Uri "$VBase/$DocdexWheel" -OutFile $DocdexPath
} catch {
    Die "download failed: $VBase/$DocdexWheel"
}
Verify-AgainstSums $DocdexPath $DocdexWheel

# --- 5. standalone CPython + venv at its FINAL versioned path ----------------
# venvs/<V> is fresh on a normal update (the running install lives in a DIFFERENT
# versioned dir, or in the legacy ~/.firekeep/venv), so live sessions are never in
# the way — this is the step that used to refuse with a wall of holder PIDs, and
# no longer has anything to refuse over. --clear covers re-provisioning a partial
# venvs/<V> left by an interrupted install (the fast path's health probe fails it
# into this full path on purpose).
#
# HONEST RESIDUAL: when venvs/<V> is the venv `current` points at (forced
# reinstall, or repairing an unhealthy current version), --clear IS destructive
# to the selected venv. That is why every download and checksum happens above
# this line — the only failures left after the clear are local (uv provision,
# pip). If one of those still dies, `current` points at a gutted venv until a
# re-run heals it: the fast path's health probe fails the gutted venv straight
# back into this full path. Confined to the explicit repair flow; a normal
# version-changing update never clears anything `current` selects.
#
# The ONE case where live processes can still hold venvs/<V> itself is a
# FIREKEEP_FORCE_REINSTALL of the version `current` points at while sessions run
# it. Summarize the holders BY NAME — never a wall of PIDs — and refuse, because
# clearing a held venv on Windows dies mid-delete and leaves it gutted.
# The try/catch inside the filter is required: $_.Path can THROW on protected
# processes (access denied on MainModule), and under $ErrorActionPreference='Stop'
# that would kill the whole install while merely scanning the process list.
if (Test-Path $TargetVenv) {
    # Trailing separator on every prefix: a bare StartsWith('...\venvs\0.1.3')
    # would also match processes under venvs\0.1.35.
    $Prefixes = @("$TargetVenv\")
    if (Test-Path $Current) {
        $CurTarget = ((Get-Item $Current -Force).Target | Select-Object -First 1)
        # Processes launched through the junction report the JUNCTION path, so when
        # `current` resolves to the venv being cleared, match that prefix too.
        if ("$CurTarget" -eq "$TargetVenv") { $Prefixes += "$Current\" }
    }
    # Exclude our own ancestry. `firekeep update` now WAITS on this script
    # (foreground child), so the parent firekeep.exe — running from the very
    # venv being force-reinstalled, via `current` — would otherwise always be
    # found here, and the guard would refuse blaming sessions that do not
    # exist. The old detached spawn excluded the updater by exiting before
    # this scan ran; the waiting design must exclude it by PID ancestry.
    $SelfChain = @(); $AncestorPid = $PID
    for ($i = 0; $i -lt 6 -and $AncestorPid; $i++) {
        $SelfChain += $AncestorPid
        $AncestorProc = Get-CimInstance Win32_Process -Filter "ProcessId=$AncestorPid" -ErrorAction SilentlyContinue
        if (-not $AncestorProc) { break }
        $AncestorPid = $AncestorProc.ParentProcessId
    }
    $Holders = @(Get-Process | Where-Object {
        $proc = $_
        if ($SelfChain -contains $proc.Id) { return $false }
        try { $p = $proc.Path } catch { $p = $null }
        $p -and (@($Prefixes | Where-Object { $p.StartsWith($_, [System.StringComparison]::OrdinalIgnoreCase) }).Count -gt 0)
    })
    if ($Holders.Count -gt 0) {
        $Summary = ($Holders | Group-Object ProcessName | Sort-Object Count -Descending |
            ForEach-Object { "$($_.Count)x $($_.Name)" }) -join ', '
        Die "venvs/$V is the running version and still in use ($Summary) - a forced reinstall of the RUNNING version needs its sessions closed; a normal update never does"
    }
}

# only-managed is load-bearing, not an optimization: default discovery walks the PATH and
# hard-fails querying the zero-byte WindowsApps python3.exe app-execution alias (a dangling
# APPEXECLINK stub — "Failed to inspect Python interpreter ... os error 3") on machines with
# the Store PythonManager installed. The contract here is a STANDALONE CPython regardless, so
# never let discovery bind the venv to whatever system Python it finds first.
Write-Host "firekeep: provisioning Python $PythonVersion into venvs/$V"
New-Item -ItemType Directory -Force -Path $Venvs | Out-Null
& $Uv venv $TargetVenv --python $PythonVersion --python-preference only-managed --clear
if ($LASTEXITCODE -ne 0) { Die "could not provision Python $PythonVersion" }

# --- 6. install the wheel BY LOCAL FILE PATH, never a URL --------------------
Write-Host "firekeep: installing $WheelName"
& $Uv pip install --python $TargetVenv --reinstall $WheelPath
if ($LASTEXITCODE -ne 0) { Die "wheel install failed" }

# --- 6b. symdex wheel: ALWAYS installed (fetched + verified in 4b) -----------
Write-Host "firekeep: installing $SymdexWheel"
& $Uv pip install --python $TargetVenv --reinstall $SymdexPath
if ($LASTEXITCODE -ne 0) { Die "symdex wheel install failed" }

# --- 6c. docdex wheel: ALWAYS installed (fetched + verified in 4c) -----------
# Every dex wheel ships with every install; REGISTRATION (~/.firekeep/dexes.json)
# is what decides whether a dex does anything. Gating the INSTALL instead would
# put a second, unverified download path in front of a user who later opts in —
# the signed supply chain is the thing that must not become optional.
Write-Host "firekeep: installing $DocdexWheel"
& $Uv pip install --python $TargetVenv --reinstall $DocdexPath
if ($LASTEXITCODE -ne 0) { Die "docdex wheel install failed" }

# --- 6d. the install must have produced a runnable firekeep -------------------
# Mirrors install.sh section 7d. Provisioning can succeed while producing
# something unusable (a wheel that installs but provides no exe, a truncated
# interpreter). Checked at the venv's REAL path and BEFORE the flip: a broken
# build must never become `current`, and a session started mid-install must
# never resolve to it.
if (-not (Test-Path (Join-Path $TargetVenv 'Scripts\firekeep.exe'))) {
    Die "install completed but venvs/$V\Scripts\firekeep.exe does not exist - the wheel did not provide it"
}

# --- 7. flip `current`, hand off to the wizard, GC ----------------------------
# The flip happens ONLY after every bundled wheel verified and installed — an
# install that dies earlier leaves `current` (and every live session) as it was.
# The wizard then re-renders shims and adapters through `current`, which is also
# what migrates a legacy install's rendered paths off ~/.firekeep/venv.
# See the file-header note: no stdin trap and no /dev/tty equivalent needed on this path.
# @RuntimeArgs = --runtime <FIREKEEP_RUNTIME> when set, else empty for all adapters.
Set-CurrentJunction $TargetVenv
& $FirekeepExe install --dist-base $Base @RuntimeArgs @JoinArgs @NonInteractiveArgs
$FirekeepExit = $LASTEXITCODE

# GC after the wizard so a failed hand-off still leaves the machine on the new,
# fully-installed version with everything else untouched.
Remove-StaleVenvs

# --- restore the caller's TLS env (see the TLS block above) -------------------
# Under `irm | iex` this ran in the caller's session; put back what the TLS block
# changed so UV_NATIVE_TLS / a cleared SSL_CERT_FILE do not outlive the install.
# Env-only operations never touch $LASTEXITCODE, but the hand-off's exit code is
# captured above anyway so nothing here can launder it.
if (-not $HadUvNativeTls) { Remove-Item Env:UV_NATIVE_TLS -ErrorAction SilentlyContinue }
if ($RemovedSslCertFile) { $env:SSL_CERT_FILE = $OrigSslCertFile }
$env:PSModulePath = $OrigPSModulePath

exit $FirekeepExit
