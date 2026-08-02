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


def test_ps1_refuses_to_replace_a_venv_that_is_in_use():
    """Second real-machine failure (2026-07-12): with agent sessions alive, the kit's own
    stdio MCP servers (firekeep-decision, firekeep-symdex, shims) run FROM ~/.firekeep/venv, and
    Windows cannot delete a directory whose executables are running — `uv venv` died
    mid-install with a bare "failed to remove directory ... Access is denied. (os error 5)".
    The script must enumerate the holder processes and Die with their names BEFORE uv
    touches the venv, so the operator learns WHAT to close instead of googling os error 5.
    Windows-only by nature: POSIX unlink()s running executables happily, so install.sh
    deliberately has no twin — same reasoning as the documented stdin-trap asymmetry."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "Get-Process" in text, "no process enumeration — the guard is missing"
    guard_idx = text.index("Get-Process")
    venv_idx = text.index("venv $Venv --python $PythonVersion")
    assert guard_idx < venv_idx, "the in-use guard must run before uv venv"
    assert "in use by" in text, "the Die message must name the holder processes"
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
    """Detect an installed client independently of the no-rebuild fast path.

    A version-changing update and ``FIREKEEP_FORCE_REINSTALL=1`` both take the full
    provisioning path, but neither may turn the final adapter re-render back into a
    credential prompt. A fresh install remains interactive because the argument array
    stays empty when there is no installed version or join code.
    """
    text = BOOTSTRAP.read_text(encoding="utf-8")
    detection = "if (Test-Path $FirekeepExe) {"
    fast_path = "if (($Installed -eq $V) -and -not $env:FIREKEEP_FORCE_REINSTALL) {"

    assert detection in text
    assert fast_path in text
    assert text.index(detection) < text.index(fast_path)
    assert "$Installed -or $env:FIREKEEP_JOIN" in text
    assert text.count("@NonInteractiveArgs") == 2


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
    venv_idx = text.index("venv $Venv --python $PythonVersion")
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
    # 1 Invoke-RestMethod (latest/latest.json, to resolve the version) + 4 Invoke-WebRequest
    # (this version's SHA256SUMS, uv.exe, the wheel, and the opt-in symdex wheel) = 5. Was 4
    # before symdex gained its own fetch-to-a-local-file step, and 3 before the main wheel did
    # (C2's fix) rather than being installed straight from a URL.
    assert len(fetch_indices) == 5, "expected exactly 5 network fetches; update this test if that changes"

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

        # That catch block must actually Die with a download-failed message, not just exist.
        catch_body = []
        for j in range(catch_idx + 1, len(lines)):
            catch_body.append(lines[j])
            if "}" in lines[j]:
                break
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
        venv_idx = text.index("venv $Venv" if script is BOOTSTRAP else 'venv "${VENV}"')
        assert tls_idx < venv_idx, f"{script.name}: TLS block must precede uv venv"
