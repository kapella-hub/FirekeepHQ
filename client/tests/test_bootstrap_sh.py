"""Drives the REAL install.sh against a local artifact server. No mocking of the script —
the entire class of bug this project exists to prevent is 'the installer produced a config
nobody verified', which a mocked bootstrap cannot catch."""
import contextlib
import functools
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from tests.conftest import _uv_target

BOOTSTRAP = Path(__file__).resolve().parents[1] / "bootstrap" / "install.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("sh") is None, reason="POSIX sh required"
)


VERSION = "1.0.0"


@pytest.fixture
def artifact_server(tmp_path):
    """Serve a fake release in the REAL version-addressed layout CI now publishes:
    latest/{install.sh,install.ps1,latest.json} + <version>/{SHA256SUMS,uv-<target>,wheel}.

    The stub uv fakes the two subcommands install.sh calls: `venv` (create the bin dir) and
    `pip install` (drop a `firekeep` shim that records its argv). That lets us drive the whole
    bootstrap without downloading a real CPython. The wheel itself is a stub too — the stub
    uv's `pip install` branch is a no-op regardless of its argument, so only the BYTES need to
    match SHA256SUMS, not real wheel contents."""
    root = tmp_path / "release"
    (root / "latest").mkdir(parents=True)
    vdir = root / VERSION
    vdir.mkdir()
    target = _uv_target()

    uv = vdir / f"uv-{target}"
    # The stub `firekeep` REPORTS ON ITS OWN STDIN (`[ -t 0 ]`). That is the whole point: asserting
    # only on argv proves which BRANCH the script took, not that `< /dev/tty` is actually
    # attached — delete the redirect and an argv-only assertion still passes. Testing stdin is
    # what makes the curl|sh test real.
    uv.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "venv" ]; then\n'
        '  mkdir -p "$2/bin"\n'
        '  printf "%s\\n" "#!/bin/sh" \\\n'
        '    "if [ -t 0 ]; then echo FIREKEEP_STDIN_IS_TTY; else echo FIREKEEP_STDIN_NOT_TTY; fi" \\\n'
        '    "echo FIREKEEP_INSTALL_CALLED \\$@" > "$2/bin/firekeep"\n'
        '  chmod +x "$2/bin/firekeep"\n'
        "fi\n"
        "exit 0\n"
    )
    wheel_name = f"firekeep_client-{VERSION}-py3-none-any.whl"
    wheel = vdir / wheel_name
    wheel.write_bytes(b"stub wheel bytes\n")
    # install.sh step 7b hard-requires a firekeep_symdex entry in SHA256SUMS (dies "release is
    # incomplete" without one) and fetch+verifies it like the client wheel. Its version is
    # independent of the client's; the stub uv's `pip install` ignores the contents.
    symdex_name = "firekeep_symdex-0.1.0-py3-none-any.whl"
    symdex = vdir / symdex_name
    symdex.write_bytes(b"stub symdex wheel bytes\n")

    digest_uv = hashlib.sha256(uv.read_bytes()).hexdigest()
    digest_wheel = hashlib.sha256(wheel.read_bytes()).hexdigest()
    digest_symdex = hashlib.sha256(symdex.read_bytes()).hexdigest()
    (vdir / "SHA256SUMS").write_text(
        f"{digest_uv}  uv-{target}\n{digest_wheel}  {wheel_name}\n"
        f"{digest_symdex}  {symdex_name}\n"
    )

    (root / "latest" / "latest.json").write_text(json.dumps({
        "version": VERSION,
        "bootstrap_sha256": "00" * 32,
        "bootstrap_ps1_sha256": "00" * 32,
    }))

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"

    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"root": root, "base": base, "target": target, "version": VERSION,
           "wheel_name": wheel_name}
    srv.shutdown()


def test_install_sh_refuses_an_unset_dist_base(tmp_path):
    """Fail loud: an installer with nowhere to fetch from must say so, not 404 six steps in."""
    proc = subprocess.run(["sh", str(BOOTSTRAP)], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert proc.returncode != 0
    assert "FIREKEEP_DIST_BASE" in proc.stderr


def _sums_path(artifact_server):
    return artifact_server["root"] / artifact_server["version"] / "SHA256SUMS"


def test_install_sh_aborts_on_a_uv_checksum_mismatch(tmp_path, artifact_server):
    """The uv binary is fetched over unauthenticated HTTP and then EXECUTED. A bad checksum
    must stop the install dead — this is the single most security-relevant line in the kit."""
    target = artifact_server["target"]
    wheel_digest = hashlib.sha256(
        (artifact_server["root"] / artifact_server["version"] /
         artifact_server["wheel_name"]).read_bytes()
    ).hexdigest()
    _sums_path(artifact_server).write_text(
        f"{'00' * 32}  uv-{target}\n{wheel_digest}  {artifact_server['wheel_name']}\n"
    )
    proc = subprocess.run(
        ["sh", str(BOOTSTRAP)], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "FIREKEEP_DIST_BASE": artifact_server["base"]},
    )
    assert proc.returncode != 0
    assert "checksum" in proc.stderr.lower()
    assert not (tmp_path / ".firekeep" / "venv").exists(), "must not proceed past a bad checksum"


def test_install_sh_aborts_on_a_wheel_checksum_mismatch(tmp_path, artifact_server):
    """THE test that would have caught C2: both bootstraps verified uv meticulously and then
    handed an UNVERIFIED URL straight to `uv pip install`. The wheel becomes the PreToolUse
    hook that runs before every Edit on the machine — it must be checksum-verified against
    SHA256SUMS exactly like uv is, using the SAME verify_against_sums code path, and verified
    BEFORE the venv is even created."""
    uv_digest = hashlib.sha256(
        (artifact_server["root"] / artifact_server["version"] /
         f"uv-{artifact_server['target']}").read_bytes()
    ).hexdigest()
    _sums_path(artifact_server).write_text(
        f"{uv_digest}  uv-{artifact_server['target']}\n"
        f"{'00' * 32}  {artifact_server['wheel_name']}\n"
    )
    proc = subprocess.run(
        ["sh", str(BOOTSTRAP)], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "FIREKEEP_DIST_BASE": artifact_server["base"]},
    )
    assert proc.returncode != 0
    assert "checksum" in proc.stderr.lower()
    assert not (tmp_path / ".firekeep" / "venv").exists(), (
        "a wheel that fails verification must never reach `uv pip install`, and — since "
        "verification now happens before the venv is provisioned at all — the venv must not "
        "exist as if nothing happened"
    )


def test_install_sh_distinguishes_a_missing_sums_entry_from_a_mismatch(tmp_path, artifact_server):
    """A SHA256SUMS with no line for our target at all must SAY SO — not report a bogus
    "checksum mismatch: expected , got <hash>".

    This test exists because its absence let a real regression through undetected: `grep |
    cut` returns CUT's exit status (0 even on empty input), so `|| die` never fired, `want`
    was empty, and a missing entry masqueraded as a tampered binary. The mismatch test below
    does NOT cover this — it rewrites the hash on an existing line, so grep still succeeds."""
    _sums_path(artifact_server).write_text(f"{'00' * 32}  uv-some-other-platform\n")
    proc = subprocess.run(
        ["sh", str(BOOTSTRAP)], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "FIREKEEP_DIST_BASE": artifact_server["base"]},
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode != 0
    assert "no SHA256SUMS entry" in proc.stderr, (
        f"a missing entry must not be reported as a checksum mismatch:\n{proc.stderr}"
    )
    assert not (tmp_path / ".firekeep" / "venv").exists()


def test_install_sh_reattaches_the_terminal_when_piped_to_sh(tmp_path, artifact_server):
    """THE curl|sh TRAP, tested for real.

    Piping the script to `sh` makes the SCRIPT stdin. If the handoff doesn't reopen
    /dev/tty, the wizard's sys.stdin.isatty() is False, every prompt is skipped, and
    agent_id lands as CHANGEME — the exact bug fixed in 7ab5e31, reintroduced by the
    delivery mechanism.

    We fork a pty so the child has a real controlling terminal (pytest itself has none),
    then run `cat install.sh | sh` inside it — reproducing curl|sh exactly. The stub `firekeep`
    echoes its argv, so we assert on what the handoff actually invoked: NOT
    --non-interactive. Grepping the script's source for "/dev/tty" would pass even with the
    redirect on the wrong line."""
    import pty

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "FIREKEEP_DIST_BASE": artifact_server["base"],
    }
    pid, fd = pty.fork()
    if pid == 0:  # child: has a controlling tty, so /dev/tty opens
        os.execve("/bin/sh", ["/bin/sh", "-c", f"cat {BOOTSTRAP} | sh"], env)

    out = b""
    with contextlib.suppress(OSError):  # EIO on child exit is the normal pty EOF
        while chunk := os.read(fd, 1024):
            out += chunk
    os.waitpid(pid, 0)
    text = out.decode(errors="replace")

    assert "FIREKEEP_INSTALL_CALLED" in text, f"handoff never ran firekeep install:\n{text}"
    # THE load-bearing assertion. The stub inspects its OWN stdin, so this fails if the
    # `< /dev/tty` redirect is missing, misplaced, or on the wrong branch — none of which an
    # argv-only assertion would notice.
    assert "FIREKEEP_STDIN_IS_TTY" in text, (
        "firekeep install ran with the PIPE as its stdin, not the terminal — the wizard's "
        f"isatty() would be False and agent_id would silently land as CHANGEME:\n{text}"
    )
    assert "--non-interactive" not in text, f"a terminal was available; do not skip prompts:\n{text}"
    assert "--dist-base" in text, "the handoff must record where the kit came from"


def test_install_sh_falls_back_when_there_is_no_controlling_terminal(tmp_path, artifact_server):
    """The headless path (CI, `docker run` without -t, cron): no controlling terminal, so
    /dev/tty cannot be OPENED even though it exists and is mode crw-rw-rw-. The guard must
    try the open, not test the path — a path test takes the interactive branch here and then
    dies with a raw shell I/O error on the redirect. pytest itself runs without a controlling
    terminal, so a plain subprocess reproduces this exactly."""
    proc = subprocess.run(
        ["sh", str(BOOTSTRAP)], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "FIREKEEP_DIST_BASE": artifact_server["base"]},
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, f"headless install must not fail:\n{proc.stderr}"
    assert "--non-interactive" in proc.stdout, (
        f"no terminal available — the wizard must not be asked to prompt:\n{proc.stdout}"
    )
    assert "no terminal available" in proc.stderr


def test_install_sh_uses_the_baked_dist_base_when_env_is_unset(tmp_path, artifact_server):
    """A release-published (baked) bootstrap must work as a plain `curl | sh` —
    no FIREKEEP_DIST_BASE env var (board 2026-07-14: zero-config one-liner). The
    env var, when set, still wins (covered implicitly by every other test here,
    which sets it against un-baked copies)."""
    baked = tmp_path / "install-baked.sh"
    baked.write_text(
        BOOTSTRAP.read_text(encoding="utf-8").replace(
            "__FIREKEEP_DIST_BASE_DEFAULT__", artifact_server["base"]),
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["sh", str(baked)], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},  # note: no FIREKEEP_DIST_BASE
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, f"baked headless install must not fail:\n{proc.stderr}"
    assert "FIREKEEP_INSTALL_CALLED" in proc.stdout
