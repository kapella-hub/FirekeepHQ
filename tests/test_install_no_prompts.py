"""The server installer must complete with NOTHING on stdin.

This file exists because of a real cold install on a fresh VPS (2026-08-15). The
published one-liner installed the client kit fine; `firekeep init` then downloaded
and ran the server installer, which opened with:

    VPS IP address: Neo4j password:
    ERROR: VPS IP address must not be empty

The operator -- the product's own author -- could not answer either question, and
the install ended there.

Both values are derivable or generatable, and the proof that we always knew it is
`.github/workflows/install-smoke.yml`, the job named "the stranger test", which
passed only because it piped the answer key in:

    printf '%s\\n%s\\n' "127.0.0.1" "smoke-test-password" | bash install.sh

A test that supplies the answers cannot detect a question that should not exist.
So the assertions here are about the SHAPE of the install, not its output: no
prompt in the config block, and every derivation helper behaving under the same
`set -euo pipefail` that install.sh runs them under.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest

from test_deploy_lib import BASH, LIB, _p

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "install.sh"


def _sh(snippet: str) -> subprocess.CompletedProcess:
    """Run a snippet with deploy/lib.sh sourced, under install.sh's own options."""
    return subprocess.run(
        [BASH, "-c", f'set -euo pipefail; source "{_p(LIB)}"; {snippet}'],
        capture_output=True,
        text=True,
    )


# --- the prompts must stay gone ---------------------------------------------

def test_install_sh_never_reads_from_stdin():
    """No `read` at all in install.sh.

    Deliberately broader than "no VPS IP prompt": ANY read makes the installer
    depend on a human being present, which is the class of defect here rather
    than the two specific questions. If a future change genuinely needs input,
    it should take a flag -- and updating this test is the moment to argue for
    it, which is the whole point.
    """
    offenders = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(INSTALL_SH.read_text(encoding="utf-8").splitlines(), 1)
        # `read -r ...` as a command, not the word "read" inside prose/comments.
        if re.match(r"\s*read\s+-", line) and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        "install.sh reads from stdin, so it cannot complete unattended:\n  "
        + "\n  ".join(offenders)
    )


def test_install_sh_derives_both_values_it_used_to_ask_for():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "detect_host_ip" in text, "install.sh no longer derives the host address"
    assert "generate_secret" in text, "install.sh no longer generates the Neo4j password"


def test_install_sh_accepts_explicit_overrides():
    """Deriving must not mean 'un-overridable'. A NAT'd host, a floating IP, or a
    restored backup all need to supply a real value."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    for token in ("flag_value ip", "flag_value neo4j-password",
                  "FIREKEEP_VPS_IP", "FIREKEEP_NEO4J_PASSWORD"):
        assert token in text, f"install.sh lost the {token!r} override path"


def test_vault_key_has_a_non_python_fallback():
    """A stock Ubuntu VPS has no python3 'cryptography'. Before this fallback the
    installer printed "[SKIP] cryptography not installed" and shipped a stack whose
    /vault/* answered 503 -- observed on a clean ubuntu:24.04 in the install lab."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "openssl rand -base64 32" in text, "VAULT_KEY has no openssl fallback"
    assert "[SKIP] cryptography not installed" not in text, (
        "the old skip message is back: it reports a dead vault as a tidy-up"
    )


# --- detect_host_ip ----------------------------------------------------------

def test_detect_host_ip_always_answers():
    result = _sh("detect_host_ip")
    assert result.returncode == 0, result.stderr
    assert re.fullmatch(r"[0-9]+(\.[0-9]+){3}", result.stdout.strip()), (
        f"detect_host_ip returned {result.stdout!r}, which is not an IPv4 address"
    )


def test_detect_host_ip_falls_back_to_loopback_with_no_tools():
    """PATH stripped to nothing resolvable: the helper must still answer rather
    than returning empty and writing VPS_IP= into .env."""
    result = _sh('PATH=/nonexistent-for-this-test; detect_host_ip')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "127.0.0.1"


# --- generate_secret ---------------------------------------------------------

def test_generate_secret_is_hex_of_the_requested_length():
    result = _sh("generate_secret 24")
    assert result.returncode == 0, result.stderr
    value = result.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{48}", value), f"not 24 bytes of hex: {value!r}"


def test_generate_secret_avoids_every_character_configure_env_rejects():
    """configure_env rejects | & \\ because they corrupt its sed substitutions.
    A generated secret that could contain one would turn a silent success into a
    corrupted .env, so the alphabet is the contract -- not an implementation detail."""
    value = _sh("generate_secret 32").stdout.strip()
    assert value and not (set(value) & set("|&\\/+="))


def test_generate_secret_is_not_a_constant():
    first = _sh("generate_secret 16").stdout.strip()
    second = _sh("generate_secret 16").stdout.strip()
    assert first and second and first != second


# --- flag_value --------------------------------------------------------------

@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--ip", "10.0.0.5"], "10.0.0.5"),
        (["--ip=10.0.0.6"], "10.0.0.6"),
        (["--pull", "--ip", "10.0.0.7", "--office"], "10.0.0.7"),
        (["--pull"], ""),
        ([], ""),
        # Last occurrence wins, as a shell would treat a repeated option.
        (["--ip", "10.0.0.1", "--ip", "10.0.0.2"], "10.0.0.2"),
        # A value that merely looks like a flag is still a value.
        (["--ip", "--office"], "--office"),
    ],
)
def test_flag_value(args, expected):
    quoted = " ".join(shlex.quote(a) for a in args)
    result = _sh(f"flag_value ip {quoted}")
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


def test_flag_value_does_not_confuse_similar_flag_names():
    """`--ip` must not match `--insecure-no-auth`, and a prefix must not match a
    longer flag -- the substring bugs this style of parser usually ships with."""
    assert _sh("flag_value ip --insecure-no-auth --ip-address 1.2.3.4").stdout == ""
    assert _sh("flag_value ip --ip 9.9.9.9").stdout == "9.9.9.9"


# --- the errors that remain must name the fix --------------------------------

def test_empty_overrides_are_rejected_with_the_command_that_fixes_them(tmp_path):
    """Unreachable from install.sh now, but reachable via `--ip ""`. The old text
    was "ERROR: VPS IP address must not be empty" -- the last line the author saw
    before giving up. An error that names only the rejected field is the defect."""
    example = tmp_path / ".env.example"
    example.write_text("VPS_IP=YOUR_VPS_IP_HERE\nNEO4J_PASSWORD=changeme\n", encoding="utf-8")
    result = _sh(
        f'cd "{_p(tmp_path)}"; configure_env .env .env.example "" "pw" || true'
    )
    assert "bash install.sh --ip" in result.stderr, result.stderr

    result = _sh(
        f'cd "{_p(tmp_path)}"; configure_env .env .env.example "10.0.0.1" "" || true'
    )
    assert "--neo4j-password" in result.stderr, result.stderr
    assert "generated" in result.stderr, (
        "the message should say a value will be generated if the flag is omitted"
    )


# --- settle_model_pull: both branches, actually executed ---------------------
#
# The default install no longer blocks on the ~3.3GB pull; it waits briefly and
# then hands the wait to a DETACHED watcher. That watcher had no coverage at all
# until these tests, and an installer that spawns a background process nothing
# ever executes is precisely the thing that breaks in somebody else's shell.
#
# Driven with a stub `docker` on PATH so both outcomes are reachable in
# milliseconds instead of minutes — the same extract-and-execute discipline
# deploy/tests/test_auth_posture.sh uses for the summary block.


def _stub_docker(ready: bool) -> str:
    """A shell FUNCTION shadowing `docker`, not a script on PATH.

    PATH was the obvious approach and it does not work here: this suite runs
    under Git Bash on Windows, where a native `C:/Users/...` entry is not a
    valid MSYS PATH element, so the stub is silently never found and the test
    exercises the real (absent) docker instead. A function shadows the command
    in every POSIX shell with no path translation at all.

    `export -f` matters for the watcher: settle_model_pull launches it with
    `bash -c`, a fresh shell that inherits exported functions but not plain
    ones -- without it the watcher would call the real docker.
    """
    body = "Models ready" if ready else "pulling manifest"
    return f'docker() {{ echo "{body}"; }}; export -f docker; '


def _run_lib(tmp_path, snippet: str, ready: bool | None = None) -> subprocess.CompletedProcess:
    stub = _stub_docker(ready) if ready is not None else ""
    return subprocess.run(
        [
            BASH, "-c",
            f'set -euo pipefail; source "{_p(LIB)}"; {stub}'
            f'cd "{_p(tmp_path)}"; {snippet}',
        ],
        capture_output=True, text=True, timeout=180,
    )


def _with_stub_docker(tmp_path, ready: bool, snippet: str) -> subprocess.CompletedProcess:
    return _run_lib(tmp_path, snippet, ready=ready)


def test_models_ready_reports_ok_immediately(tmp_path):
    """A re-install with a warm model volume must still say [OK], not sit out
    the grace period."""
    result = _with_stub_docker(tmp_path, True, "settle_model_pull 30 0 1")
    assert result.returncode == 0, result.stderr
    assert "[OK] models ready" in result.stdout
    assert "still downloading" not in result.stdout


def test_an_unfinished_pull_backgrounds_and_says_what_is_true(tmp_path):
    """The branch that replaced a 15-minute silent wait. It must state the
    CONSEQUENCE -- writes stored but not searchable -- not merely that it is
    still going."""
    result = _with_stub_docker(tmp_path, False, "settle_model_pull 2 0 1")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "[..] models are still downloading" in out
    assert "stack is UP and usable now" in out
    assert 'status="partial"' in out
    assert "not yet recallable" in out
    # Every escape hatch named, so nobody has to go looking.
    assert "docker compose logs -f ollama-pull" in out
    assert "firekeep doctor" in out
    assert "--wait-for-models" in out


def test_backgrounding_actually_spawns_a_watcher(tmp_path):
    """Proves the detached spawn RUNS, rather than that the message was printed.
    grace=0 skips the foreground wait entirely and goes straight to the handoff,
    even though the stub would have reported ready."""
    result = _run_lib(
        tmp_path,
        "settle_model_pull 0 0 1; "
        "for _ in 1 2 3 4 5 6 7 8 9 10; do "
        "  [ -s model-pull.log ] && break; sleep 1; done",
        ready=True,
    )
    assert result.returncode == 0, result.stderr
    log = tmp_path / "model-pull.log"
    assert log.is_file(), "the watcher never created its log"
    assert "models ready" in log.read_text(encoding="utf-8")


def test_blocking_mode_reports_a_timeout_instead_of_backgrounding(tmp_path):
    """`--wait-for-models` is what CI uses: it must never silently background,
    because the job asserts a memory round-trips right afterwards."""
    result = _with_stub_docker(tmp_path, False, "settle_model_pull 2 1 1")
    assert result.returncode == 0, result.stderr
    assert "WARNING: timed out after 2s" in result.stdout
    assert "still downloading" not in result.stdout
    assert not (tmp_path / "model-pull.log").exists(), "blocking mode spawned a watcher"


def test_settle_model_pull_never_fails_the_install(tmp_path):
    """A slow link is not a broken install. Under `set -e` a nonzero return here
    would abort the script AFTER the stack is already up and healthy."""
    for ready, blocking in ((True, 0), (False, 0), (False, 1)):
        result = _with_stub_docker(
            tmp_path, ready, f"settle_model_pull 2 {blocking} 1; echo RC=$?"
        )
        assert "RC=0" in result.stdout, (ready, blocking, result.stdout, result.stderr)


def test_install_sh_delegates_rather_than_inlining_it(tmp_path):
    """Guard the refactor: if the logic moves back inline it silently loses all
    the coverage above."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "settle_model_pull " in text
    assert "--wait-for-models" in text
