"""Structural + (on Windows) executable checks for the PowerShell bootstrap.

The parity assertions run everywhere: they are what stops the two scripts from drifting
into different installers with the same name."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

BOOTSTRAP = Path(__file__).resolve().parents[1] / "bootstrap" / "install.ps1"
BOOTSTRAP_SH = Path(__file__).resolve().parents[1] / "bootstrap" / "install.sh"


def test_both_bootstraps_provision_only_managed_python():
    """Real-machine failure (2026-07-12): uv 0.5.11's default interpreter discovery walks
    the PATH and hard-fails querying the zero-byte WindowsApps python3.exe app-execution
    alias ("Failed to inspect Python interpreter ... os error 3") — the alias dangles when
    the Store package layout changes (the new PythonManager packages), and uv's Store-shim
    skip-list predates those aliases. The scripts' stated contract is a STANDALONE CPython
    anyway, so uv must be told exactly that: `--python-preference only-managed` skips PATH
    discovery entirely (and stops the venv from silently binding to whatever random system
    Python discovery finds first). Parity: both scripts, same flag, on the venv line."""
    for script in (BOOTSTRAP, BOOTSTRAP_SH):
        text = script.read_text(encoding="utf-8")
        venv_lines = [
            line for line in text.splitlines()
            if " venv " in line and "--python " in line
        ]
        assert len(venv_lines) == 1, f"{script.name}: expected exactly one uv venv line"
        assert "--python-preference only-managed" in venv_lines[0], (
            f"{script.name}: uv venv must pin --python-preference only-managed, or PATH "
            "discovery can crash on broken Store aliases / bind to a non-standalone Python"
        )


def test_ps1_refuses_a_forced_reinstall_of_the_held_running_version():
    """Descendant of the 2026-07-12 in-use guard, rescoped by the side-by-side layout.

    The original failure: with agent sessions alive, the kit's stdio MCP servers ran
    FROM the single ~/.firekeep/venv, and Windows cannot delete a directory whose
    executables are running — `uv venv --clear` died mid-install with a bare
    "Access is denied. (os error 5)", and the guard's only possible advice was
    "close every session" plus a wall of ~93 raw PIDs.

    Side-by-side venvs dissolve that case for NORMAL updates (the target venvs/<V>
    is a fresh directory nobody holds). The ONE case where live processes can still
    hold venvs/<V> itself is FIREKEEP_FORCE_REINSTALL of the version `current`
    points at while sessions run it — clearing a held venv on Windows dies
    mid-delete and leaves it gutted, so the script must still refuse, BEFORE uv
    touches the venv. The message must stay humane: holders summarized as
    'Nx name' counts (Group-Object ProcessName), never the PID wall.

    Windows-only by nature: POSIX unlink()s running executables happily, so
    install.sh deliberately has no twin — same reasoning as the stdin-trap
    asymmetry."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "Get-Process" in text, "no process enumeration — the guard is missing"
    scope_idx = text.index("if (Test-Path $TargetVenv) {")
    guard_idx = text.index("Get-Process")
    venv_idx = text.index("venv $TargetVenv --python $PythonVersion")
    assert scope_idx < guard_idx < venv_idx, (
        "the guard must be scoped to an already-existing venvs/<V> (the only dir a "
        "forced reinstall can collide with) and must run before uv venv touches it"
    )
    assert "Group-Object ProcessName" in text, (
        "holders must be summarized by process name — never the raw-PID wall the "
        "old in-place guard printed"
    )
    assert "a normal update never does" in text, (
        "the Die message must say that only a FORCED reinstall of the running "
        "version needs sessions closed — a normal update never does"
    )
    assert "close" in text.lower(), "the Die message must say what action unblocks"


def test_ps1_exists_and_declares_the_same_contract():
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "FIREKEEP_DIST_BASE" in text
    assert "FIREKEEP_VERSION" in text
    assert "--dist-base" in text
    assert "uv pip install" in text or "pip" in text


def test_both_bootstraps_forward_join_on_fast_and_main_paths():
    ps = BOOTSTRAP.read_text(encoding="utf-8")
    sh = BOOTSTRAP_SH.read_text(encoding="utf-8")
    assert "$JoinArgs" in ps
    assert ps.count("@JoinArgs") == 2
    assert "FIREKEEP_JOIN" in sh
    # Two branches (fast/main), each with TTY and headless handoffs.
    assert sh.count('--join "${FIREKEEP_JOIN}"') == 4


def test_ps1_existing_install_handoff_is_non_interactive_even_when_forced():
    """Detect an installed client independently of the healthy-venv fast path.

    A version-changing update and ``FIREKEEP_FORCE_REINSTALL=1`` both take the full
    provisioning path, but neither may turn the final adapter re-render back into a
    credential prompt. A fresh install remains interactive because the argument array
    stays empty when there is no installed version or join code.

    Detection probes `current` (the layout's truth) with the legacy single venv as
    the pre-0.1.35 mid-migration fallback; the fast path is a separate probe of
    venvs/<V> itself, so a healthy already-provisioned target flips + re-renders
    with zero downloads while detection still decides interactivity.
    """
    text = BOOTSTRAP.read_text(encoding="utf-8")
    detection = "$Installed = Get-VenvVersion $Current"
    legacy_fallback = "if (-not $Installed) { $Installed = Get-VenvVersion $LegacyVenv }"
    # Test-VenvComplete in the condition is load-bearing: an install killed
    # between the client wheel and the symdex wheel leaves a venv whose python
    # happily reports $V — without the completeness probe the fast path would
    # flip `current` to that half-installed venv forever, and nothing would
    # ever route back through the full provision that repairs it.
    fast_path = ("if (((Get-VenvVersion $TargetVenv) -eq $V) "
                 "-and (Test-VenvComplete $TargetVenv) "
                 "-and -not $env:FIREKEEP_FORCE_REINSTALL) {")

    assert detection in text
    assert legacy_fallback in text, (
        "a pre-0.1.35 install (legacy ~/.firekeep/venv, no `current` yet) must "
        "still be detected as installed, or its update re-prompts for credentials"
    )
    assert fast_path in text
    assert text.index(detection) < text.index(fast_path)
    assert "$Installed -or $env:FIREKEEP_JOIN" in text
    assert text.count("@NonInteractiveArgs") == 2


@pytest.mark.skipif(os.name != "nt", reason="PowerShell argument splatting is Windows-only")
@pytest.mark.parametrize(
    ("installed", "join_code", "expected"),
    [
        ("0.1.31", "", "--non-interactive"),
        ("", "join-code", "--non-interactive"),
        ("", "", ""),
    ],
)
def test_ps1_non_interactive_handoff_splats_one_argument(
    installed, join_code, expected
):
    """Execute the shipped assignment, not a Python model of PowerShell semantics.

    An ``if`` expression unwraps a one-item array into a scalar; splatting that scalar
    passes one character at a time (``- - n o ...``).  Keeping the variable typed as
    ``string[]`` is therefore a runtime requirement, not style.
    """
    text = BOOTSTRAP.read_text(encoding="utf-8")
    start = text.index("[string[]]$NonInteractiveArgs = @()")
    end = text.index("# --- idempotent fast path", start)
    assignment = text[start:end]
    script = "\n".join([
        f"$Installed = '{installed}'",
        f"$env:FIREKEEP_JOIN = '{join_code}'",
        assignment,
        "function Receive-Args { param([Parameter(ValueFromRemainingArguments=$true)]"
        "[string[]]$Rest) $Rest }",
        "$received = @(Receive-Args @NonInteractiveArgs)",
        "Write-Output ($received -join '|')",
    ])
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected


def test_ps1_verifies_the_uv_checksum_before_executing_it():
    """Same reasoning as the POSIX side: uv is downloaded over unauthenticated HTTP and then
    run. Windows must not be the soft target."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "Get-FileHash" in text
    assert "SHA256SUMS" in text


def test_ps1_never_resolves_the_wheel_by_name():
    """`firekeep-client` on PyPI belongs to a third party. The wheel is fetched to a local file
    first (never installed straight from a URL — `uv pip install <url>` does no hash
    checking), so the eventual `uv pip install` call must reference the local path, not a
    URL variable."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert ".whl" in text
    install_line = next(
        line for line in text.splitlines() if line.strip().startswith("& $Uv pip install")
    )
    assert "$WheelPath" in install_line
    assert "http" not in install_line.lower()


def test_ps1_verifies_the_wheel_against_sha256sums_before_installing_it():
    """THE test that would have caught C2: the wheel must go through the SAME verifier as
    uv.exe, using a local file, before `uv pip install` ever sees it."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "Verify-AgainstSums $WheelPath $WheelName" in text
    # The wheel fetch + verify must happen strictly before the venv is created — that is
    # what makes "tampered wheel -> no venv at all" true rather than "tampered wheel -> venv
    # exists but the wheel never got installed."
    verify_idx = text.index("Verify-AgainstSums $WheelPath $WheelName")
    venv_idx = text.index("venv $TargetVenv --python $PythonVersion")
    assert verify_idx < venv_idx


def test_ps1_distinguishes_a_missing_sums_entry_from_a_mismatch():
    """Mirrors the POSIX regression test: a missing SHA256SUMS entry must be reported as
    exactly that, not laundered through an empty $Want into a bogus 'mismatch' message."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "no SHA256SUMS entry" in text
    # Select-String returning nothing must be checked explicitly (on the match, or on
    # whatever is assigned from it) before the expected hash is used in a comparison -
    # otherwise a missing entry silently becomes an empty $Want that then "mismatches".
    assert "-not $Match" in text or "-not $Want" in text


def test_ps1_shares_one_verifier_between_uv_and_the_wheel():
    """A SECOND, subtly different verifier is how the wheel got skipped once already (this
    is the whole point of C2). There must be exactly one Verify-AgainstSums function,
    called once for uv.exe and once for the wheel — not two near-duplicate inline checks."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert text.count("function Verify-AgainstSums") == 1
    assert 'Verify-AgainstSums $UvTmp "uv-$Target.exe"' in text
    assert "Verify-AgainstSums $WheelPath $WheelName" in text


def test_ps1_checks_lastexitcode_after_native_calls():
    """PowerShell's $ErrorActionPreference does NOT trap a nonzero exit code from a native
    executable — only $LASTEXITCODE reflects it. Every `&$Uv ...` invocation must be followed
    by an explicit check."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "$ErrorActionPreference" in text
    assert "$LASTEXITCODE" in text


def test_ps1_guards_every_network_fetch_with_try_catch():
    """Structural check only - this is NOT executed, since no Windows/PowerShell host is
    available to either the author or the reviewer of this fix. It asserts on the script's
    text shape, not on runtime behavior.

    What this proves: for EACH of the three network fetches (Invoke-WebRequest x2,
    Invoke-RestMethod x1), the fetch line is bound to its OWN try/catch - i.e. no other
    fetch call appears between the nearest preceding `try {` and the fetch, nor between the
    fetch and the nearest following `catch {` - and that catch block Die()s with a
    'download failed: ...' message. Mirrors the POSIX fetch() contract (`|| die "download
    failed: $1"`): without a per-call guard, a failed download surfaces as PowerShell's raw
    exception trace instead of the script's own firekeep-prefixed message.

    ONE deliberate exception: the SHA256SUMS.minisig fetch (release signing) is
    best-effort BY CONTRACT - releases predating signing publish no .minisig, so its
    catch must WARN and continue, never Die. Its guard is still per-fetch; only the
    catch body's obligation differs, and this test asserts that difference explicitly
    rather than exempting the line.

    What this does NOT prove: it is not a parser and does not execute the script. It is a
    deliberately stricter replacement for a earlier version of this test that only compared
    aggregate counts (3 fetches, >=3 try/catch blocks, 3 "download failed:" strings). Counts
    alone pass against a single SHARED guard plus decoy try/catch blocks with no per-fetch
    isolation at all, e.g.:

        try { fetch1; fetch2; fetch3 } catch { Die "download failed: x" }
        try { } catch { Die "download failed: y" }
        try { } catch { Die "download failed: z" }

    That has 3 fetch calls, 3 try blocks, 3 catch blocks, and 3 "download failed:" strings -
    every aggregate the old test checked - yet zero of the fetches are individually guarded,
    which was the entire point of the fix this test exists to protect. Binding each fetch to
    its nearest enclosing try/catch (and rejecting any OTHER fetch call sitting in between)
    is what catches that failure mode.
    """
    lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
    fetch_markers = ("Invoke-WebRequest", "Invoke-RestMethod")

    def is_fetch_line(line):
        return any(marker in line for marker in fetch_markers)

    fetch_indices = [i for i, line in enumerate(lines) if is_fetch_line(line)]
    # 1 Invoke-RestMethod (latest/latest.json, to resolve the version) + 5 Invoke-WebRequest
    # (this version's SHA256SUMS, its best-effort .minisig, uv.exe, the wheel, and the
    # always-on symdex wheel) = 6. Was 5 before release signing added the .minisig fetch,
    # 4 before symdex gained its own fetch-to-a-local-file step, and 3 before the main
    # wheel did (C2's fix) rather than being installed straight from a URL.
    assert len(fetch_indices) == 6, "expected exactly 6 network fetches; update this test if that changes"

    for idx in fetch_indices:
        fetch_line = lines[idx].strip()

        # Walk backwards for the nearest preceding `try {`. If another fetch call is hit
        # first, this fetch's "try" is actually shared with that other fetch - not its own.
        try_idx = None
        for j in range(idx - 1, -1, -1):
            if is_fetch_line(lines[j]):
                pytest.fail(
                    f"line {idx + 1} ({fetch_line!r}): another fetch call at line {j + 1} "
                    "sits between it and the nearest preceding 'try {' - the guard is "
                    "shared across fetches, not bound to this one"
                )
            if "try {" in lines[j]:
                try_idx = j
                break
        assert try_idx is not None, f"line {idx + 1} ({fetch_line!r}) has no preceding 'try {{'"

        # Walk forwards for the nearest following `catch {`. Same reasoning in reverse: if
        # another fetch call is hit first, this fetch's try block never closes before the
        # next fetch starts, so the eventual catch is shared, not this fetch's own.
        catch_idx = None
        for j in range(idx + 1, len(lines)):
            if is_fetch_line(lines[j]):
                pytest.fail(
                    f"line {idx + 1} ({fetch_line!r}): another fetch call at line {j + 1} "
                    "sits between it and the nearest following 'catch {' - the guard is "
                    "shared across fetches, not bound to this one"
                )
            if "catch {" in lines[j]:
                catch_idx = j
                break
        assert catch_idx is not None, f"line {idx + 1} ({fetch_line!r}) has no following 'catch {{'"

        # That catch block must actually Die with a download-failed message, not just
        # exist — EXCEPT the best-effort .minisig fetch, whose catch must WARN and
        # continue (a missing signature is history, not an error, until
        # [dist] require_signed flips) and must NOT Die.
        catch_body = []
        for j in range(catch_idx + 1, len(lines)):
            catch_body.append(lines[j])
            if "}" in lines[j]:
                break
        if "SHA256SUMS.minisig" in fetch_line:
            assert not any("Die " in line for line in catch_body), (
                f"the .minisig fetch at line {idx + 1} must stay best-effort: its catch "
                "must warn, never Die — a signature-less legacy release has to keep installing"
            )
            assert any("not signed" in line for line in catch_body), (
                f"the .minisig fetch at line {idx + 1} must WARN on absence, not stay silent"
            )
        else:
            assert any('Die "download failed:' in line for line in catch_body), (
                f"catch block for the fetch at line {idx + 1} ({fetch_line!r}) does not Die "
                "with a 'download failed: ...' message"
            )


def test_ps1_suppresses_cleanup_errors_consistently_before_die():
    """Minor-finding regression test: both the missing-entry branch and the mismatch branch
    (inside the shared Verify-AgainstSums helper) remove the failing file before calling Die.
    If Remove-Item is unguarded on either path, a locked file (e.g. AV scanning) would raise
    its own exception and preempt the intended diagnostic message. Both cleanup calls must
    suppress errors so the security-relevant message always survives — for BOTH callers
    (uv.exe and the wheel), since they now share this one code path."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    remove_calls = [line for line in text.splitlines() if "Remove-Item $File -Force" in line]
    assert len(remove_calls) == 2
    assert all("-ErrorAction SilentlyContinue" in line for line in remove_calls)


def test_ps1_documents_why_there_is_no_stdin_trap():
    """The next reader must not 'fix' the asymmetry with the POSIX script by adding a
    /dev/tty-style dance that doesn't apply here."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "irm" in text.lower() or "iex" in text.lower() or "stdin" in text.lower()


# --- the `current` alias (side-by-side venvs, 0.1.35) ---------------------------
#
# Windows-only (they run on the client-windows CI job): these guard the choice of
# NTFS link primitive, which only matters — and can only be meaningfully probed —
# on the platform whose privilege model created the problem.


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction semantics are Windows-only")
def test_ps1_current_link_is_a_junction_not_a_directory_symlink():
    """`current` must be an NTFS JUNCTION, never a directory symlink.

    Creating a directory symlink (`New-Item -ItemType SymbolicLink`, `mklink /D`)
    requires SeCreateSymbolicLinkPrivilege — admin, or Developer Mode. CI runners
    and the owner's box often have that privilege, so a symlink-based flip passes
    every automated check and then fails ONLY on real teammates' locked-down
    machines, at install time, with an 'A required privilege is not held' error.
    Junctions need no privilege at all (probe-verified non-elevated on Windows 11),
    which is the entire reason the design chose them."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "New-Item -ItemType Junction" in text, (
        "the current alias is no longer created as an NTFS junction"
    )
    assert "-ItemType SymbolicLink" not in text, (
        "a directory symlink needs admin/Developer Mode — it would pass on "
        "privileged CI while failing for real teammates at install time"
    )
    assert "mklink" not in text.lower(), (
        "mklink /D creates a directory SYMLINK (privilege-gated); /J would work "
        "but the script standardizes on New-Item -ItemType Junction"
    )


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction semantics are Windows-only")
def test_ps1_junction_removal_is_rmdir_only():
    """`cmd /c rmdir` must be the ONLY way the `current` junction is removed.

    rmdir on a junction deletes exactly the LINK NODE on every PowerShell build.
    Remove-Item is not that primitive: current builds happen to stop at the
    reparse point, but ancient 5.1 builds recursed THROUGH it and deleted the
    TARGET venv's files (probed live) — turning a routine flip into a gutted
    install. Every Remove-Item in the script must therefore stay away from
    $Current; recursive deletes are reserved for the GC's renamed .gc corpses."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert 'cmd /c rmdir "$Current"' in text, (
        "the junction is no longer removed via `cmd /c rmdir` — the only "
        "link-node-only removal primitive across PS builds"
    )
    offenders = [
        line.strip() for line in text.splitlines()
        if "Remove-Item" in line and "$Current" in line
        and not line.strip().startswith("#")
    ]
    assert offenders == [], (
        "Remove-Item must never target the `current` junction (old 5.1 builds "
        f"recurse through the reparse point into the venv): {offenders}"
    )


@pytest.mark.skipif(os.name != "nt" or shutil.which("powershell") is None,
                    reason="Windows + PowerShell required")
def test_ps1_refuses_an_unset_dist_base(tmp_path):
    # Deliberately leave FIREKEEP_DIST_BASE unset (the whole point), but keep the minimal
    # Windows env powershell.exe needs to even start: without SystemRoot, PS 5.1 fails to
    # initialize its crypto provider (error 8009001d) and dies BEFORE running the script,
    # emitting its own error text instead of the script's FIREKEEP_DIST_BASE message.
    env = {"USERPROFILE": str(tmp_path), "PATH": os.environ["PATH"]}
    env.update({k: os.environ[k] for k in ("SystemRoot", "SystemDrive") if k in os.environ})
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BOOTSTRAP)],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode != 0
    assert "FIREKEEP_DIST_BASE" in proc.stderr


def test_both_bootstraps_use_native_tls_and_neutralize_ssl_cert_file():
    """Real-machine failure (2026-07-13): a corporate-root-only SSL_CERT_FILE export
    (~/.zshrc, corporate proxy workaround) is treated by uv/rustls as the EXCLUSIVE trust
    store — even with --native-tls — so genuine PyPI chains fail as UnknownIssuer while
    the intercepted release host needs the OS store. Fix: UV_NATIVE_TLS=1 (covers every
    uv call site by construction, no per-line flags) + unset SSL_CERT_FILE with a loud
    warning and a FIREKEEP_KEEP_SSL_CERT_FILE=1 escape hatch. Parity: both scripts, and the
    TLS block must precede the uv venv line."""
    for script in (BOOTSTRAP, BOOTSTRAP_SH):
        text = script.read_text(encoding="utf-8")
        assert "UV_NATIVE_TLS" in text, f"{script.name}: missing UV_NATIVE_TLS export"
        assert "SSL_CERT_FILE" in text, f"{script.name}: missing SSL_CERT_FILE handling"
        assert "FIREKEEP_KEEP_SSL_CERT_FILE" in text, (
            f"{script.name}: missing the keep-override escape hatch"
        )
        tls_idx = text.index("UV_NATIVE_TLS")
        venv_idx = text.index(
            "venv $TargetVenv" if script is BOOTSTRAP else 'venv "${TARGET_VENV}"'
        )
        assert tls_idx < venv_idx, f"{script.name}: TLS block must precede uv venv"


def test_both_bootstraps_carry_the_signing_verification_block():
    """Release signing (docs/RELEASE-SIGNING.md): both scripts must keep the baked-key
    placeholder (make_release substitutes the real public key at release time), the
    split-string placeholder probe (so the substitution cannot rewrite the comparison
    itself), the FIREKEEP_SIGNING_PUB out-of-band override, and the minisign-if-present
    gate — best-effort by contract, so a bare machine without minisign stays installable."""
    for script in (BOOTSTRAP, BOOTSTRAP_SH):
        text = script.read_text(encoding="utf-8")
        assert "__FIREKEEP_SIGNING_PUB" in text, f"{script.name}: signing-pub placeholder lost"
        assert "FIREKEEP_SIGNING_PUB" in text, f"{script.name}: no out-of-band key override"
        assert "minisign" in text, f"{script.name}: no minisign verification path"
        assert "SHA256SUMS.minisig" in text, f"{script.name}: never fetches the signature"
    # The probe strings are split so make_release's token substitution can't rewrite them.
    assert '"__FIREKEEP_SIGNING_PUB_""DEFAULT__"' in BOOTSTRAP_SH.read_text(encoding="utf-8")
    assert "'__FIREKEEP_SIGNING_PUB_' + 'DEFAULT__'" in BOOTSTRAP.read_text(encoding="utf-8")


def test_ps1_minisign_rejection_is_fatal_and_checked_via_lastexitcode():
    """minisign is a native executable: $ErrorActionPreference does not see its exit
    code, so the script must check $LASTEXITCODE and Die. An invalid signature is
    tampering evidence — unlike an ABSENT one, it must never degrade to a warning."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    idx = text.index("$Minisign.Source")
    tail = text[idx:idx + 400]
    assert "$LASTEXITCODE -ne 0" in tail
    assert "signature verification FAILED" in tail
    assert "Die" in tail


# --- the verified-sums hand-off (two-fetch split, security review HIGH) ---------
#
# The executable proof lives in test_bootstrap_sh.py (request-logged server); these
# keep the PowerShell mirror from drifting out of the contract, plus one Windows-only
# executable check of the handed branch itself.


def test_both_bootstraps_accept_the_handed_sums_file():
    """Both scripts must honour FIREKEEP_SUMS_FILE — and only TOGETHER with
    FIREKEEP_VERSION, the shape only the client's `firekeep update` hand-off produces,
    so a stray env var cannot redirect a manual install's verification."""
    sh = BOOTSTRAP_SH.read_text(encoding="utf-8")
    ps = BOOTSTRAP.read_text(encoding="utf-8")
    assert '[ -n "${FIREKEEP_SUMS_FILE:-}" ] && [ -n "${FIREKEEP_VERSION:-}" ]' in sh
    assert "$env:FIREKEEP_SUMS_FILE -and $env:FIREKEEP_VERSION" in ps
    # Set-but-unusable is fatal in both — a silent fallback to the network fetch
    # would reopen the exact hole the hand-off closes.
    for name, text in (("install.sh", sh), ("install.ps1", ps)):
        assert "FIREKEEP_SUMS_FILE is set but not readable" in text, (
            f"{name}: an unusable handed file must die, not fall back to fetching"
        )


def test_both_bootstraps_skip_all_sums_network_fetches_under_a_handed_file():
    """Under a handed file there must be NO sums fetch and NO .minisig fetch: the
    client already verified against its pinned key, and 'no fetch #2' is the whole
    fix. The minisign block must be gated on the handed flag in both scripts."""
    sh = BOOTSTRAP_SH.read_text(encoding="utf-8")
    ps = BOOTSTRAP.read_text(encoding="utf-8")
    # sh: the SHA256SUMS fetch sits in the else of the handed branch...
    assert 'fetch "${VBASE}/SHA256SUMS" "${BIN}/SHA256SUMS"' in sh
    assert sh.index("FIREKEEP_SUMS_FILE") < sh.index('fetch "${VBASE}/SHA256SUMS"')
    # ...and the minisign gate checks the handed flag first.
    sh_gate = next(line for line in sh.splitlines()
                   if "command -v minisign" in line and "if " in line)
    assert '-z "${SUMS_HANDED}"' in sh_gate
    ps_gate = next(line for line in ps.splitlines()
                   if "$Minisign" in line and line.strip().startswith("if "))
    assert "-not $SumsHanded" in ps_gate


@pytest.mark.skipif(os.name != "nt" or shutil.which("powershell") is None,
                    reason="Windows + PowerShell required")
def test_ps1_handed_sums_makes_no_sums_fetch_and_refuses_mismatched_artifacts(tmp_path):
    """Executable Windows check of the handed branch: serve a self-consistent
    'attacker' release, hand a DIFFERENT (client-verified) sums file, and prove from
    the server's request log that the script fetched neither SHA256SUMS nor its
    .minisig — then refused the artifacts on checksum mismatch. The full POSIX twin
    (control run included) is test_bootstrap_sh.py's two-fetch-split test."""
    import functools
    import hashlib
    import http.server
    import threading

    root = tmp_path / "release"
    vdir = root / "1.0.0"
    vdir.mkdir(parents=True)
    uv = vdir / "uv-x86_64-pc-windows-msvc.exe"
    uv.write_bytes(b"not really an exe, never reached")
    wheel = vdir / "firekeep_client-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"attacker wheel bytes\n")
    (vdir / "SHA256SUMS").write_text(  # self-consistent: fetch #2 would accept it
        f"{hashlib.sha256(uv.read_bytes()).hexdigest()}  uv-x86_64-pc-windows-msvc.exe\n"
        f"{hashlib.sha256(wheel.read_bytes()).hexdigest()}  firekeep_client-1.0.0-py3-none-any.whl\n"
    )

    requests: list[str] = []

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def do_GET(self):
            requests.append(self.path)
            super().do_GET()

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Handler))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # What the CLIENT verified: same uv, different (genuine) wheel hash.
        verified = tmp_path / "SHA256SUMS.verified"
        verified.write_text(
            f"{hashlib.sha256(uv.read_bytes()).hexdigest()}  uv-x86_64-pc-windows-msvc.exe\n"
            f"{hashlib.sha256(b'genuine wheel').hexdigest()}  firekeep_client-1.0.0-py3-none-any.whl\n"
        )
        home = tmp_path / "home"
        home.mkdir()
        env = {"USERPROFILE": str(home), "PATH": os.environ["PATH"],
               "FIREKEEP_DIST_BASE": f"http://127.0.0.1:{srv.server_address[1]}",
               "FIREKEEP_VERSION": "1.0.0",
               "FIREKEEP_SUMS_FILE": str(verified)}
        env.update({k: os.environ[k] for k in ("SystemRoot", "SystemDrive")
                    if k in os.environ})
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(BOOTSTRAP)],
            capture_output=True, text=True, env=env, timeout=120,
        )
    finally:
        srv.shutdown()

    assert proc.returncode != 0, f"mismatched artifacts must be refused:\n{proc.stdout}"
    assert "handed by firekeep update" in proc.stdout
    assert "checksum mismatch" in proc.stderr.lower()
    sums_fetches = [p for p in requests if "SHA256SUMS" in p]
    assert sums_fetches == [], (
        f"under a handed FIREKEEP_SUMS_FILE the script must fetch no sums: {sums_fetches}"
    )
    # Side-by-side layout: a refused install must leave no venvs/<V> and no
    # `current` junction — the flip is the LAST act, after both wheels verify.
    assert not (home / ".firekeep" / "venvs" / "1.0.0").exists()
    assert not (home / ".firekeep" / "current").exists()
