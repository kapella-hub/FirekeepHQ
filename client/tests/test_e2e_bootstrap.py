# client/tests/test_e2e_bootstrap.py
"""Real bootstrap, real uv, real CPython, real wheel. Network required.

Run: pytest tests/test_e2e_bootstrap.py -m e2e
"""
import functools
import http.server
import json
import os
import platform
import shutil
import subprocess
import threading
import tomllib
from pathlib import Path

import pytest

from tests.conftest import _uv_target

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.name == "nt", reason="POSIX bootstrap; ps1 covered on the Windows runner"),
]

CLIENT = Path(__file__).resolve().parents[1]
# The symdex package lives beside client/ in the monorepo. install.sh step 7b / install.ps1
# step 6b hard-require a firekeep_symdex-*.whl entry in the release SHA256SUMS (they die
# "release is incomplete" otherwise), so the release fixture below MUST build and serve it
# alongside the client wheel — a symdex-less release is exactly the broken shape those steps
# refuse to install.
SYMDEX = CLIENT.parent / "symdex"


def _run_bootstrap(args, env, **kwargs):
    """Run install.sh (or an argv starting with it) with NO controlling terminal.

    install.sh decides interactive-vs-headless with `{ : < /dev/tty; } 2>/dev/null` — that
    tests whether a CONTROLLING TERMINAL can be opened, not whether stdin is a pipe. Under
    CI/the Bash tool there is no controlling tty, so the headless `--non-interactive` branch
    is taken and the run finishes in seconds. But a developer running `pytest -m e2e` from
    their own terminal DOES have a controlling tty inherited by the child: install.sh would
    take the interactive branch and `firekeep install`'s wizard would block reading its first
    prompt from /dev/tty, hanging the test until the 600s timeout. `start_new_session=True`
    detaches the child into a new session with no controlling terminal, so the headless
    branch is deterministic regardless of how pytest itself was invoked.
    """
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("timeout", 600)
    return subprocess.run(args, env=env, start_new_session=True, **kwargs)


def _bare_machine_env(home: Path, dist_base: str) -> dict:
    """Env for the installer subprocess, simulating a genuinely bare machine.

    This suite's own tests/conftest.py has an autouse fixture that monkeypatches
    FIREKEEP_CONFIG/FIREKEEP_CACHE_DIR/FIREKEEP_LOG_DIR onto THIS pytest process (so unit tests never
    touch a developer's real ~/.firekeep). Spreading os.environ verbatim would leak those
    isolation overrides straight into the installed `firekeep` binary's subprocess, so it
    resolves its own home from the leaked FIREKEEP_CONFIG instead of from HOME — producing a
    second, parallel ~/.firekeep-shaped tree under the *test's* isolation sandbox and silently
    failing to write anything under the `home` this test actually inspects. Any FIREKEEP_* the
    developer's own shell exports (e.g. FIREKEEP_AGENT_ID) would equally contaminate a "does this
    install cleanly on a bare machine" assertion, so strip the whole prefix rather than
    naming the three known offenders.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("FIREKEEP_")}
    env["HOME"] = str(home)
    env["FIREKEEP_DIST_BASE"] = dist_base
    return env


# The release fixture builds the REAL wheel from this checkout, and make_release.py
# refuses a version that doesn't match the wheel filename — so this must be the
# checkout's actual version. A literal here rots on every release bump (0.1.3 sat
# frozen while the client moved on, and the whole e2e suite failed at fixture setup).
VERSION = tomllib.loads((CLIENT / "pyproject.toml").read_text())["project"]["version"]


@pytest.fixture
def release(tmp_path):
    """Build a REAL release: real wheel, real mirrored uv, real make_release.py — then fan it
    out into the REAL version-addressed layout CI now publishes, exactly as the upload() loop
    in .gitlab-ci.yml does: BASE/latest/{install.sh,install.ps1,latest.json} (the stable
    entry point) + BASE/<version>/{SHA256SUMS,uv-<target>,wheel} (every version keeps its own
    directory). Serving flat (the old shape) would silently resurrect C1/C3 — a tag-pinned
    base whose latest.json points at the release it shipped inside, forever."""
    dist = tmp_path / "dist"
    dist.mkdir()
    subprocess.run(["python3", "-m", "build", "--wheel", "--outdir", str(dist), str(CLIENT)],
                   check=True, capture_output=True)
    # The symdex wheel is not optional: install.sh step 7b / install.ps1 step 6b read its name
    # straight out of SHA256SUMS and die "release is incomplete" if it is missing, and A4's
    # coming make_release.py presence guard will refuse to build a release without it. Build it
    # into the SAME dist dir BEFORE make_release runs — make_release checksums every *.whl it
    # finds there, so this both feeds SHA256SUMS the required firekeep_symdex- entry AND satisfies
    # A4's guard. (make_release's own count check globs firekeep_client-*.whl specifically, so a
    # second wheel here does not trip it.)
    subprocess.run(["python3", "-m", "build", "--wheel", "--outdir", str(dist), str(SYMDEX)],
                   check=True, capture_output=True)
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not installed on the runner; CI installs it in the e2e job")
    try:
        target = _uv_target()
    except KeyError:
        pytest.skip(
            f"unsupported platform for this suite: {platform.system()}/{platform.machine()}")
    shutil.copy(uv, dist / f"uv-{target}")
    # make_release.py hashes the bootstrap scripts into latest.json, so they must be in dist
    # before it runs — mirroring exactly what the CI release job does.
    for name in ("install.sh", "install.ps1"):
        shutil.copy(CLIENT / "bootstrap" / name, dist / name)
    subprocess.run(
        ["python3", str(CLIENT / "scripts" / "make_release.py"), VERSION, str(dist)],
        check=True, capture_output=True,
    )

    served = tmp_path / "served"
    (served / "latest").mkdir(parents=True)
    (served / VERSION).mkdir()
    for name in ("install.sh", "install.ps1", "latest.json"):
        shutil.copy(dist / name, served / "latest" / name)
    for pattern in ("firekeep_client-*.whl", "firekeep_symdex-*.whl", "uv-*"):
        for p in dist.glob(pattern):
            shutil.copy(p, served / VERSION / p.name)
    shutil.copy(dist / "SHA256SUMS", served / VERSION / "SHA256SUMS")

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(served))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"base": base, "dist": dist, "served": served, "version": VERSION}
    srv.shutdown()


def test_bootstrap_installs_and_renders_one_hook_group_per_event(release, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = _bare_machine_env(home, release["base"])

    proc = _run_bootstrap(["sh", str(CLIENT / "bootstrap" / "install.sh")], env)
    assert proc.returncode == 0, proc.stderr

    firekeep = home / ".firekeep" / "venv" / "bin" / "firekeep"
    assert firekeep.is_file(), "the venv must expose the firekeep console script"

    # The config records where it came from, so `firekeep update` can find its way home.
    cfg = (home / ".firekeep" / "config").read_text()
    assert "[dist]" in cfg and release["base"] in cfg

    # THE assertion that would have caught the 2026-07-11 bug: exactly one firekeep hook group
    # per Claude lifecycle event, no duplicates, no dangling scripts.
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    for event in ("SessionStart", "Stop", "UserPromptSubmit", "PreToolUse", "PostToolUse"):
        groups = settings["hooks"][event]
        assert len(groups) == 1, f"{event} has {len(groups)} groups, expected 1"
        assert "firekeep_client.hooks" in groups[0]["hooks"][0]["command"]


def test_bootstrap_migrates_a_preexisting_duplicate_hook_setup(release, tmp_path):
    """Reproduces the actual 2026-07-11 precondition: a machine whose ~/.claude/settings.json
    already carries BOTH a retired bash hook group and a stale firekeep python-hook group for the
    same event (exactly what upsert_hook_group's docstring says it must collapse). Without
    this, the fresh-home happy path above would pass even if the collapsing logic regressed,
    since a brand-new install never has a second group to begin with.
    """
    home = tmp_path / "home"
    home.mkdir()
    claude_dir = home / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "bash ~/scripts/briefing.sh",
                            "timeout": 15}]},
                {"hooks": [{"type": "command",
                            "command": "/some/stale/venv/bin/python -m firekeep_client.hooks "
                                       "session_start", "timeout": 15}]},
            ],
            # A foreign (non-firekeep) hook must survive the merge untouched.
            "Notification": [
                {"hooks": [{"type": "command", "command": "notify-send hi"}]},
            ],
        }
    }))

    env = _bare_machine_env(home, release["base"])
    proc = _run_bootstrap(["sh", str(CLIENT / "bootstrap" / "install.sh")], env)
    assert proc.returncode == 0, proc.stderr

    settings = json.loads((claude_dir / "settings.json").read_text())
    session_start = settings["hooks"]["SessionStart"]
    assert len(session_start) == 1, (
        f"pre-existing legacy + stale firekeep groups did not collapse: {session_start}")
    command = session_start[0]["hooks"][0]["command"]
    assert "firekeep_client.hooks" in command
    assert "briefing.sh" not in command
    assert str(home / ".firekeep" / "venv") in command, "must point at THIS install's fresh venv"
    # Foreign hook untouched.
    assert settings["hooks"]["Notification"][0]["hooks"][0]["command"] == "notify-send hi"


def test_update_replaces_the_wheel_in_place(release, tmp_path):
    """Install, then publish a 'newer' release and run `firekeep update`. Proves the handoff
    actually completes rather than deadlocking on its own running process.

    This is also the trace that proves C3 is closed: BASE is version-agnostic, so bumping
    the SERVED latest/latest.json's version is enough for `firekeep update --check` to see it —
    unlike the old tag-pinned BASE, where latest.json could only ever describe the release
    it shipped inside."""
    home = tmp_path / "home"
    home.mkdir()
    env = _bare_machine_env(home, release["base"])
    _run_bootstrap(["sh", str(CLIENT / "bootstrap" / "install.sh")], env, check=True)

    manifest_path = release["served"] / "latest" / "latest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = "99.0.0"
    manifest_path.write_text(json.dumps(manifest))

    firekeep = home / ".firekeep" / "venv" / "bin" / "firekeep"
    proc = subprocess.run([str(firekeep), "update", "--check"],
                          capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0
    assert "99.0.0" in proc.stdout and "firekeep update" in proc.stdout


def test_update_to_actually_completes_a_real_reinstall_in_place(release, tmp_path):
    """`--check` (above) only proves the manifest-advance half of C3. This drives the FULL
    `firekeep update --to <version>` path — the one a teammate actually runs — through its real
    re-exec: `cmd_update` downloads+verifies the bootstrap script, execve(2)s over the running
    `firekeep` process, and install.sh re-provisions ~/.firekeep/venv IN PLACE, at the exact path the
    process invoking it is running from.

    That re-provisioning step is where a real gap was found during this task's own manual
    verification: `uv venv <path>` refuses to recreate a venv that already exists ("Use
    --clear"), and a post-install machine's ~/.firekeep/venv always already exists by
    definition — so every real (non---check) `firekeep update` died at 'provisioning Python'
    before this fix added --clear to both bootstraps' `uv venv` invocations. --check alone
    would never catch this: it never re-execs the bootstrap at all. Pinning `--to` the SAME
    version (rather than a fabricated 'newer' one) exercises the exact re-provision-in-place
    code path without needing a second real wheel build at a different version."""
    home = tmp_path / "home"
    home.mkdir()
    env = _bare_machine_env(home, release["base"])
    _run_bootstrap(["sh", str(CLIENT / "bootstrap" / "install.sh")], env, check=True)

    firekeep = home / ".firekeep" / "venv" / "bin" / "firekeep"
    proc = _run_bootstrap(
        [str(firekeep), "update", "--to", release["version"]], env,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "could not provision Python" not in proc.stderr

    # The re-exec replaced the process image (os.execve), so `firekeep` must still work
    # afterwards — a botched in-place reinstall would otherwise leave a half-provisioned or
    # missing venv that only a SUBSEQUENT command would ever reveal as broken.
    check = subprocess.run([str(firekeep), "update", "--check"],
                            capture_output=True, text=True, env=env, timeout=60)
    assert check.returncode == 0
    assert "already up to date" in check.stdout


def test_bootstrap_dies_on_a_tampered_wheel_and_never_creates_the_venv(release, tmp_path):
    """THE test that would have caught C2. Both bootstraps verify uv meticulously and then,
    before this fix, handed an UNVERIFIED URL straight to `uv pip install` — the wheel is not
    incidental, it becomes the PreToolUse hook that runs before every Edit on the machine.

    make_release.py has already hashed the wheel's real bytes into the served SHA256SUMS by
    the time this test runs (the `release` fixture ran it). Corrupting the SERVED wheel here
    reproduces exactly what a MITM or a broken mirror would produce: bytes on the wire that
    no longer match the hash the release actually published. install.sh must die with a
    checksum error and must not create the venv at all — verification happens strictly
    before `uv venv` is invoked, not just before `uv pip install`."""
    vdir = release["served"] / release["version"]
    wheel = next(vdir.glob("firekeep_client-*.whl"))
    original = wheel.read_bytes()
    wheel.write_bytes(original[:-1] + bytes([original[-1] ^ 0xFF]))

    home = tmp_path / "home"
    home.mkdir()
    env = _bare_machine_env(home, release["base"])
    proc = _run_bootstrap(["sh", str(CLIENT / "bootstrap" / "install.sh")], env)

    assert proc.returncode != 0, proc.stdout
    assert "checksum" in proc.stderr.lower(), proc.stderr
    assert not (home / ".firekeep" / "venv").exists(), (
        "a wheel that fails checksum verification must never reach `uv pip install`, and "
        "since verification now runs before `uv venv` too, the venv must not exist at all"
    )
