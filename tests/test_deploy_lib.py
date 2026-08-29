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


def _run_sed_i(tmp_path, script: str, file_content: str):
    """Drives the portable sed_i helper through bash under set -euo pipefail,
    the way install.sh sources and calls it. Returns (result, target_path)."""
    target = tmp_path / ".env"
    target.write_text(file_content, encoding="utf-8")
    bash = (
        f'set -euo pipefail; source "{_p(LIB)}"; '
        f'sed_i {shlex.quote(script)} "{_p(target)}"'
    )
    result = subprocess.run([BASH, "-c", bash], capture_output=True, text=True)
    return result, target


def test_sed_i_edits_in_place_without_corrupting_other_lines(tmp_path):
    """sed_i replaces the targeted line and leaves the rest byte-for-byte.
    Why it exists: `sed -i "s|..|..|" f` consumes the script as a backup-suffix
    argument on BSD/macOS sed and corrupts the file, so the same installer that
    works on Linux would mangle .env on a Mac. sed_i routes through a temp file
    so the call behaves identically on GNU, BSD/macOS and busybox."""
    result, target = _run_sed_i(
        tmp_path,
        "s|^VAULT_KEY=.*|VAULT_KEY=generated|",
        "NEO4J_PASSWORD=keep\nVAULT_KEY=\nBIND_ADDR=127.0.0.1\n",
    )
    assert result.returncode == 0, result.stderr
    content = target.read_text(encoding="utf-8")
    assert content == "NEO4J_PASSWORD=keep\nVAULT_KEY=generated\nBIND_ADDR=127.0.0.1\n"


@skip_unless_chmod_enforced
def test_sed_i_result_is_not_world_readable(tmp_path):
    """The only file sed_i edits in anger is .env (Neo4j password, Fernet
    VAULT_KEY). mktemp yields mode 0600, so the rewritten file must never widen
    to world-readable, whatever the original file's mode was."""
    _, target = _run_sed_i(
        tmp_path,
        "s|^VAULT_KEY=.*|VAULT_KEY=generated|",
        "VAULT_KEY=\n",
    )
    assert (target.stat().st_mode & 0o777) == 0o600


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


REPO = LIB.parents[1]
UNINSTALL_SH = REPO / "uninstall.sh"


# --- provenance_app_version: the --pull path reports the tag, not the sentinel
#
# install.sh's build-provenance line used to run `git describe ... || echo
# <sentinel>` unconditionally. On a `--pull` install from the source-free bundle
# there is no git repo, so it fell through to the sentinel and printed it as
# APP_VERSION for a deployment that is actually the pulled release tag (e.g.
# v0.4.5). The rule now lives in this helper so it can be asserted directly.
#
# The sentinel itself is imported rather than spelled out: it changed from the
# release-shaped `0.6.0` to `0.0.0-unprovenanced` (see tests/test_provenance.py),
# and a literal here would have kept passing while guarding nothing.

def _provenance(pull_mode: int, image_tag: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, "-c",
         f'set -euo pipefail; source "{_p(LIB)}"; '
         f'provenance_app_version {pull_mode} {shlex.quote(image_tag)}'],
        capture_output=True, text=True, encoding="utf-8", cwd=str(REPO),
    )


def test_provenance_app_version_pull_reports_the_release_tag():
    result = _provenance(1, "v0.4.5")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "v0.4.5"


def test_provenance_app_version_pull_is_not_the_absent_repo_fallback():
    """The exact regression: --pull stamped git-describe's no-repo fallback as
    APP_VERSION instead of the tag it deployed."""
    import provenance
    assert _provenance(1, "v0.4.5").stdout.strip() != provenance._FALLBACK_VERSION


def test_provenance_app_version_source_ignores_a_passed_tag():
    """From source the version is git-describe, never the IMAGE_TAG argument: a
    checkout's stale IMAGE_TAG must not masquerade as the built version. (This is
    why install.sh only overrides APP_VERSION on the pull path.)"""
    out = _provenance(0, "v9.9.9").stdout.strip()
    assert out != "v9.9.9"
    assert out, "the source path must always answer something (describe/SHA/sentinel)"


def test_install_sh_stamps_the_image_tag_into_app_version_on_pull():
    """Guard the wiring: the helper can be correct while install.sh calls it
    wrong. The pull branch must feed IMAGE_TAG_VALUE through provenance_app_version."""
    text = (REPO / "install.sh").read_text(encoding="utf-8")
    assert 'provenance_app_version "$PULL_MODE" "${IMAGE_TAG_VALUE:-}"' in text
    # The inline `git describe ... || echo <sentinel>` must be gone from install.sh —
    # it moved into the helper, and leaving a copy behind reopens the bug.
    assert "git describe" not in text, "install.sh still computes APP_VERSION inline"


# --- box-drawing helpers: borders stay aligned ------------------------------
#
# The closing summary frames the one-time admin key. The frame is only useful if
# it does not tear: every rendered line must be the same display width, which
# means the ASCII interior field must be exactly <width> columns wide and the
# box characters must sit outside it.

def _run_box(width: int, interior: list[str]) -> subprocess.CompletedProcess:
    parts = [f"box_top {width}"]
    parts += [f"box_line {width} {shlex.quote(text)}" for text in interior]
    parts.append(f"box_bot {width}")
    return subprocess.run(
        [BASH, "-c", f'set -euo pipefail; source "{_p(LIB)}"; ' + "; ".join(parts)],
        capture_output=True, text=True, encoding="utf-8",
    )


def test_box_borders_all_render_the_same_width():
    width = 62
    key = "nxs_" + "a" * 48  # the real admin-key shape: 52 chars
    result = _run_box(width, ["  heading text", "", f"    {key}"])
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    # 2-space indent + 1 border + <width> interior + 1 border, counted in code
    # points (locale-independent), so a 3-byte box char still counts as one.
    assert lines, "box produced no output"
    for line in lines:
        assert len(line) == width + 4, f"border misaligned ({len(line)}): {line!r}"
    assert lines[0].startswith("  ╔") and lines[0].endswith("╗")
    assert lines[-1].startswith("  ╚") and lines[-1].endswith("╝")


def test_box_line_keeps_a_52_char_key_inside_the_frame():
    key = "nxs_" + "b" * 48
    result = _run_box(62, [f"    {key}"])
    assert result.returncode == 0, result.stderr
    key_line = next(line for line in result.stdout.splitlines() if key in line)
    assert key_line.endswith("║"), "the key overran the right border"
    assert len(key_line) == 62 + 4


# --- uninstall.sh: server teardown, guarded by an explicit confirmation ------
#
# The script removes the data volumes (docker compose down -v) -- all team
# memory. It must refuse unless the operator confirms, exactly one of three ways:
# typing "yes", --yes, or FIREKEEP_UNINSTALL_YES=1. `docker` is shadowed by an
# exported shell function (a PATH stub is silently unusable under Git Bash on
# Windows -- see test_install_no_prompts._stub_docker) so a real teardown never
# runs and each invocation is recorded to a log the test inspects.

def _run_uninstall(tmp_path, args: str = "", stdin_text: str = "",
                   env_yes: bool = False) -> tuple[subprocess.CompletedProcess, Path]:
    docker_log = tmp_path / "docker-invocations.log"
    stub = (
        f'export DOCKER_LOG="{_p(docker_log)}"; '
        'docker() { echo "$*" >> "$DOCKER_LOG"; }; export -f docker; '
    )
    prefix = 'export FIREKEEP_UNINSTALL_YES=1; ' if env_yes else ""
    result = subprocess.run(
        [BASH, "-c", f'{prefix}{stub}bash "{_p(UNINSTALL_SH)}" {args}'],
        input=stdin_text, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    return result, docker_log


def test_uninstall_exists_and_is_a_bash_script():
    assert UNINSTALL_SH.is_file()
    assert UNINSTALL_SH.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")


def test_uninstall_refuses_without_confirmation(tmp_path):
    """Closed stdin, no flag, no env var: it must abort and touch nothing."""
    result, docker_log = _run_uninstall(tmp_path)  # stdin_text="" -> immediate EOF
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Aborted" in result.stdout
    assert not docker_log.exists(), "docker was invoked despite the refusal"


def test_uninstall_refuses_on_any_answer_other_than_yes(tmp_path):
    # No trailing "\n": Python text-mode stdin would translate it to "\r\n" on
    # Windows and `read` would then see "no\r". `read` captures the partial line
    # at EOF, so a bare "no" is delivered verbatim on every platform.
    result, docker_log = _run_uninstall(tmp_path, stdin_text="no")
    assert result.returncode == 1
    assert "Aborted" in result.stdout
    assert not docker_log.exists()


def test_uninstall_warns_about_data_loss_before_prompting(tmp_path):
    result, _ = _run_uninstall(tmp_path)
    out = result.stdout
    assert "DATA LOSS WARNING" in out
    assert "ALL TEAM MEMORY IS DELETED" in out
    assert "deploy/backup.sh" in out, "the warning must point at the backup command"


def test_uninstall_removes_volumes_when_confirmed_by_typing_yes(tmp_path):
    # "yes" without a newline -- see test_uninstall_refuses_on_any_answer for why.
    result, docker_log = _run_uninstall(tmp_path, stdin_text="yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert docker_log.is_file(), "docker was never invoked"
    assert "compose down -v --remove-orphans" in docker_log.read_text(encoding="utf-8")


def test_uninstall_yes_flag_skips_the_prompt(tmp_path):
    result, docker_log = _run_uninstall(tmp_path, args="--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "compose down -v --remove-orphans" in docker_log.read_text(encoding="utf-8")


def test_uninstall_env_var_skips_the_prompt(tmp_path):
    result, docker_log = _run_uninstall(tmp_path, env_yes=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "compose down -v --remove-orphans" in docker_log.read_text(encoding="utf-8")


def test_uninstall_rejects_unknown_flags(tmp_path):
    result, docker_log = _run_uninstall(tmp_path, args="--force")
    assert result.returncode == 1
    assert "Usage:" in result.stdout or "Usage:" in result.stderr
    assert not docker_log.exists()
