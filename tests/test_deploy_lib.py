"""deploy/lib.sh helpers, driven through bash.

install.sh printed "Vault: Enabled" unconditionally while key generation
could silently skip, making the installer assert a security control it had
not configured.
"""
import os
import platform
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "deploy" / "lib.sh"


def _find_bash() -> str | None:
    """Resolve a POSIX-correct bash to drive deploy/lib.sh through.

    On non-Windows platforms, plain `bash` on PATH is correct.

    On Windows, `CreateProcess` resolves a bare `bash` to the System32
    WSL shim regardless of PATH order -- and WSL cannot read `E:\\...`
    style paths (it needs `/mnt/e/...`), so every `bash -c` call would
    silently run against the wrong filesystem. `shutil.which("bash")`
    usually finds Git Bash first, but that isn't guaranteed, so its
    result is verified (not trusted) and well-known Git-for-Windows
    install locations are tried as a fallback.
    """
    if platform.system() != "Windows":
        return "bash"

    candidates = []
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    candidates += [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
    ]

    for candidate in candidates:
        path = Path(candidate)
        if not path.is_file():
            continue
        # The System32 WSL shim is a `bash.exe` too -- reject it by path
        # rather than trusting `shutil.which`'s PATH-order result.
        if path.name.lower() == "bash.exe" and "system32" in str(path).lower():
            continue
        try:
            probe = subprocess.run(
                [str(path), "-c", "uname -s"],
                capture_output=True, text=True, timeout=10,
            )
        except OSError:
            continue
        if probe.returncode == 0 and "MINGW" in probe.stdout.upper():
            return str(path)
    return None


BASH = _find_bash()
if BASH is None:
    pytest.skip(
        "no POSIX-correct bash found: checked PATH (shutil.which) and the "
        r"usual Git-for-Windows locations (C:\Program Files\Git\bin\bash.exe, "
        r"C:\Program Files\Git\usr\bin\bash.exe); the System32 WSL bash.exe "
        "shim was excluded because it cannot resolve E:\\... style paths",
        allow_module_level=True,
    )


def _p(path) -> str:
    """POSIX-ify a path for embedding into a bash -c string. On Windows,
    str(Path) yields backslashes, which MSYS/git-bash cannot resolve as
    path separators -- every path handed to `bash -c` in this file must go
    through this."""
    return Path(path).as_posix()


FAKE_ENV_EXAMPLE = (
    "VPS_IP=YOUR_VPS_IP_HERE\n"
    'CORS_ORIGINS=["http://YOUR_VPS_IP_HERE:8040"]\n'
    "NEO4J_PASSWORD=changeme\n"
    "VAULT_KEY=\n"
    "CONFLUENCE_PAT_VAULT_KEY=confluence_pat\n"
)


def _chmod_is_enforced() -> bool:
    """Some filesystems (NTFS via git-bash/MSYS on Windows) accept chmod()
    calls without actually restricting access -- permission-bit assertions
    would be meaningless there. Probe once at collection time rather than
    assuming POSIX semantics just because the test runs under bash."""
    fd, path = tempfile.mkstemp()
    try:
        os.close(fd)
        os.chmod(path, 0o600)
        return (os.stat(path).st_mode & 0o777) == 0o600
    finally:
        os.unlink(path)


CHMOD_ENFORCED = _chmod_is_enforced()
skip_unless_chmod_enforced = pytest.mark.skipif(
    not CHMOD_ENFORCED,
    reason="filesystem does not enforce POSIX permission bits (e.g. NTFS)",
)


def _configure_env(tmp_path, vps_ip: str, neo4j_password: str):
    """Drives configure_env through bash under set -euo pipefail (matching
    how install.sh actually sources and calls it) and returns
    (returncode, stdout, stderr, envfile_path)."""
    example = tmp_path / ".env.example"
    example.write_text(FAKE_ENV_EXAMPLE, encoding="utf-8")
    envfile = tmp_path / ".env"

    script = (
        f'set -euo pipefail; source "{_p(LIB)}"; cd "{_p(tmp_path)}"; '
        f'if configure_env .env .env.example {shlex.quote(vps_ip)} {shlex.quote(neo4j_password)}; then '
        f'echo CONFIGURE_ENV_OK; else echo CONFIGURE_ENV_FAILED; fi'
    )
    result = subprocess.run(
        [BASH, "-c", script],
        capture_output=True, text=True,
    )
    return result, envfile


def test_configure_env_writes_valid_env(tmp_path):
    result, envfile = _configure_env(tmp_path, "10.0.0.5", "hunter2pass")
    assert result.returncode == 0, result.stderr
    assert "CONFIGURE_ENV_OK" in result.stdout
    assert envfile.exists()
    content = envfile.read_text(encoding="utf-8")
    assert "VPS_IP=10.0.0.5" in content
    assert "NEO4J_PASSWORD=hunter2pass" in content
    assert "YOUR_VPS_IP_HERE" not in content


@skip_unless_chmod_enforced
def test_configure_env_writes_env_mode_600(tmp_path):
    _, envfile = _configure_env(tmp_path, "10.0.0.5", "hunter2pass")
    assert (envfile.stat().st_mode & 0o777) == 0o600


def test_configure_env_leaves_confluence_pat_vault_key_untouched(tmp_path):
    """Regression guard for the unanchored VAULT_KEY= sed: it also matched
    the substring VAULT_KEY= inside CONFLUENCE_PAT_VAULT_KEY=confluence_pat
    and corrupted that unrelated line."""
    _, envfile = _configure_env(tmp_path, "10.0.0.5", "hunter2pass")
    content = envfile.read_text(encoding="utf-8")
    assert "CONFLUENCE_PAT_VAULT_KEY=confluence_pat" in content
    assert "VAULT_KEY=" in content.splitlines()[3]  # the real VAULT_KEY= line, untouched


def test_configure_env_rejects_empty_vps_ip_and_does_not_abort_the_caller(tmp_path):
    result, envfile = _configure_env(tmp_path, "", "hunter2pass")
    assert result.returncode == 0, result.stderr  # the calling script must survive
    assert "CONFIGURE_ENV_FAILED" in result.stdout
    # Was `"must not be empty"`. install.sh no longer prompts for this -- it
    # derives it -- so an empty value now means someone passed `--ip ""`, and
    # the message names the fix instead of restating the rejected field. The
    # bare "ERROR: VPS IP address must not be empty" was the last line the
    # author saw on the cold install that prompted this whole change.
    assert "host address is empty" in result.stderr
    assert "--ip" in result.stderr, "the error must name the flag that sets it"
    assert not envfile.exists(), "a rejected value must not leave a half-written .env"


def test_configure_env_rejects_empty_neo4j_password(tmp_path):
    result, envfile = _configure_env(tmp_path, "10.0.0.5", "")
    assert result.returncode == 0, result.stderr
    assert "CONFIGURE_ENV_FAILED" in result.stdout
    assert not envfile.exists()


def test_configure_env_rejects_pipe_character_that_would_break_the_sed_delimiter(tmp_path):
    result, envfile = _configure_env(tmp_path, "10.0.0.5", "bad|pass")
    assert result.returncode == 0, result.stderr
    assert "CONFIGURE_ENV_FAILED" in result.stdout
    assert not envfile.exists()


def test_configure_env_rejects_ampersand_that_would_corrupt_the_sed_replacement(tmp_path):
    result, envfile = _configure_env(tmp_path, "10.0.0.5", "bad&pass")
    assert result.returncode == 0, result.stderr
    assert "CONFIGURE_ENV_FAILED" in result.stdout
    assert not envfile.exists()


def test_configure_env_rejects_backslash_that_would_corrupt_the_sed_replacement(tmp_path):
    result, envfile = _configure_env(tmp_path, "10.0.0.5", "bad\\pass")
    assert result.returncode == 0, result.stderr
    assert "CONFIGURE_ENV_FAILED" in result.stdout
    assert not envfile.exists()


def test_configure_env_failure_leaves_no_temp_files_behind(tmp_path):
    _configure_env(tmp_path, "", "hunter2pass")
    leftovers = [p for p in tmp_path.iterdir() if p.name not in (".env.example",)]
    assert leftovers == [], f"stray files left behind: {leftovers}"


def test_configure_env_helper_is_defined():
    result = subprocess.run(
        [BASH, "-c", f'source "{_p(LIB)}"; declare -F configure_env >/dev/null && echo DEFINED || echo MISSING'],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "DEFINED"


def _vault_status(tmp_path, env_contents: str) -> str:
    envfile = tmp_path / ".env"
    envfile.write_text(env_contents, encoding="utf-8")
    result = subprocess.run(
        [BASH, "-c", f'source "{_p(LIB)}"; vault_status_line "{_p(envfile)}"'],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def test_reports_enabled_when_key_present(tmp_path):
    # Presence-only check (vault_status_line), so an obviously-fake low-entropy
    # value keeps the test honest AND does not trip the gitleaks secret scan the
    # way a base64-shaped fake did (the line-pinned allowlist entry it needed
    # went stale the moment this file grew — the fragility .gitleaksignore warns of).
    out = _vault_status(tmp_path, "VAULT_KEY=DUMMY_VAULT_KEY_FOR_TESTS\n")
    assert "Enabled" in out
    assert "DISABLED" not in out


def test_reports_disabled_when_key_empty(tmp_path):
    out = _vault_status(tmp_path, "VAULT_KEY=\n")
    assert "DISABLED" in out
    assert "VAULT_KEY" in out, "must name the variable the operator has to set"


def test_reports_disabled_when_key_absent_entirely(tmp_path):
    out = _vault_status(tmp_path, "NEO4J_PASSWORD=hunter2\n")
    assert "DISABLED" in out


def test_reports_disabled_when_envfile_missing(tmp_path):
    # No .env is created at all — tmp_path is empty, so this exercises the
    # genuinely-missing case. The previous version wrote the file and then
    # unlinked it, which tested the same thing more obscurely and left an
    # unused variable behind.
    result = subprocess.run(
        [BASH, "-c", f'source "{_p(LIB)}"; vault_status_line "{_p(tmp_path)}/.env"'],
        capture_output=True, text=True, check=True,
    )
    assert "DISABLED" in result.stdout


def test_commented_out_key_is_not_counted_as_set(tmp_path):
    out = _vault_status(tmp_path, "#VAULT_KEY=abc123\n")
    assert "DISABLED" in out


def test_survives_pipefail_when_key_absent(tmp_path):
    """install.sh itself runs under `set -euo pipefail`. A no-match grep in
    the value= pipeline exits 1, and pipefail propagates that through the
    command substitution — without `|| true` this aborts the calling
    function silently before any DISABLED/Enabled line prints. Regression
    guard for that exact failure mode."""
    envfile = tmp_path / ".env"
    envfile.write_text("NEO4J_PASSWORD=hunter2\n", encoding="utf-8")
    result = subprocess.run(
        [BASH, "-c",
         f'set -euo pipefail; source "{_p(LIB)}"; vault_status_line "{_p(envfile)}"; echo AFTER_CALL_OK'],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "DISABLED" in result.stdout
    assert "AFTER_CALL_OK" in result.stdout


def _office_requested(*args: str) -> bool:
    """Drives office_mode_requested through bash.

    A bare `if office_mode_requested ...` would print NO on the else branch
    even when office_mode_requested is missing entirely (name lookup fails
    with "command not found", exit 127, which `if` swallows) — the negative
    test cases (`test_office_mode_off_by_default`,
    `test_office_mode_ignores_other_flags`) would then pass against a broken
    or deleted implementation, giving no real signal. The explicit
    `declare -F` presence check below turns that failure mode into a loud
    subprocess error (`check=True` raises) instead of a silent false-NO.
    """
    quoted = " ".join(f'"{a}"' for a in args)
    script = (
        f'set -e; source "{_p(LIB)}"; '
        f'declare -F office_mode_requested >/dev/null || {{ echo "office_mode_requested is not defined" >&2; exit 3; }}; '
        f'if office_mode_requested {quoted}; then echo YES; else echo NO; fi'
    )
    result = subprocess.run(
        [BASH, "-c", script],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip() == "YES"


def test_office_mode_helper_is_defined():
    """Canary: fails loudly (not a false NO) if office_mode_requested is
    missing/renamed/deleted from deploy/lib.sh. See _office_requested's
    docstring for the failure mode this guards against."""
    result = subprocess.run(
        [BASH, "-c", f'source "{_p(LIB)}"; declare -F office_mode_requested >/dev/null && echo DEFINED || echo MISSING'],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "DEFINED"


def test_office_mode_off_by_default():
    assert _office_requested() is False


def test_office_mode_on_with_flag():
    assert _office_requested("--office") is True


def test_office_mode_ignores_other_flags():
    assert _office_requested("--verbose", "--dry-run") is False


def test_office_mode_found_among_other_flags():
    assert _office_requested("--verbose", "--office") is True
