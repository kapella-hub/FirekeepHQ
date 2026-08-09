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
    match SHA256SUMS, not real wheel contents.

    Since the side-by-side layout (0.1.35) the stub venv must also carry `bin/python`:
    the script's venv_version() health probe runs `<venv>/bin/python -I -c` and
    point_current() flips the `current` symlink by running os.replace THROUGH the target
    venv's own python (mv cannot replace a symlink-to-directory portably). The stub python
    execs the real python3, which keeps both faithful: os.replace really renames, and the
    probe's `import firekeep_client` really fails (the stub venv holds no such module —
    exactly a fresh machine's state, so the handoff stays interactive)."""
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
        "  printf '%s\\n' '#!/bin/sh' 'exec \"$(command -v python3)\" \"$@\"' > \"$2/bin/python\"\n"
        '  chmod +x "$2/bin/python"\n'
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

    # Request log: the two-fetch-split tests below must PROVE the bootstrap made
    # no second SHA256SUMS fetch, which only a server-side record can show —
    # asserting on the script's output would trust the very code under test.
    requests: list[str] = []

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def do_GET(self):
            requests.append(self.path)
            super().do_GET()

        def log_message(self, *args):  # keep pytest output readable
            pass

    handler = functools.partial(_Handler)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"

    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"root": root, "base": base, "target": target, "version": VERSION,
           "wheel_name": wheel_name, "requests": requests}
    srv.shutdown()


def _assert_nothing_provisioned(home):
    """The side-by-side layout's 'must not proceed' shape: a failed install leaves
    NO venvs/<V> and NO `current` alias (pre-0.1.35 this was `~/.firekeep/venv`
    not existing). lexists on `current`, not exists: a dangling symlink would be
    exactly the kind of half-flip these tests exist to rule out."""
    fk = home / ".firekeep"
    assert not (fk / "venvs" / VERSION).exists(), (
        "the versioned venv must not be provisioned past a failure"
    )
    assert not os.path.lexists(fk / "current"), (
        "`current` must not exist (not even dangling) after a failed install — "
        "the flip is the last observable act, after both wheels verify"
    )


def _assert_current_selects_the_new_venv(home):
    """The side-by-side layout's success shape: venvs/<V> exists at its FINAL
    path (a venv is not relocatable — it must be born where it lives) and
    `current` is a SYMLINK resolving to it, because every rendered surface
    (shims, adapters, the wizard hand-off itself) routes through the alias."""
    venv = home / ".firekeep" / "venvs" / VERSION
    cur = home / ".firekeep" / "current"
    assert (venv / "bin" / "firekeep").exists(), "venvs/<V> was not provisioned"
    assert cur.is_symlink(), "`current` must be a symlink, not a copied directory"
    assert Path(os.path.realpath(cur)) == Path(os.path.realpath(venv)), (
        f"`current` resolves to {os.path.realpath(cur)}, not the freshly "
        f"installed {venv}"
    )


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
    _assert_nothing_provisioned(tmp_path)  # must not proceed past a bad checksum


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
    # A wheel that fails verification must never reach `uv pip install`, and — since
    # verification happens before the venv is provisioned at all — venvs/<V> must not
    # exist and `current` must not have been flipped, as if nothing happened.
    _assert_nothing_provisioned(tmp_path)


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
    _assert_nothing_provisioned(tmp_path)


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
    _assert_current_selects_the_new_venv(tmp_path)


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
    _assert_current_selects_the_new_venv(tmp_path)


# --- release signing: best-effort minisign verification (docs/RELEASE-SIGNING.md) ---
#
# A stub `minisign` on PATH decides the verdict, so these exercise the script's WIRING
# (gate, fetch, fatal-vs-warn split) without needing the real binary in CI.

def _stub_minisign(tmp_path, exit_code):
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "minisign"
    stub.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    stub.chmod(0o755)
    return stub_dir


def _sign_env(tmp_path, artifact_server, stub_dir=None):
    path = f"{stub_dir}:/usr/bin:/bin" if stub_dir else "/usr/bin:/bin"
    return {"PATH": path, "HOME": str(tmp_path),
            "FIREKEEP_DIST_BASE": artifact_server["base"],
            "FIREKEEP_SIGNING_PUB": "RWTfakekeyforwiringtests"}


def test_install_sh_dies_when_minisign_rejects_the_signature(tmp_path, artifact_server):
    """A PRESENT signature that fails verification is tampering evidence and must stop
    the install before any artifact is trusted — unlike absence, which only warns."""
    (artifact_server["root"] / artifact_server["version"] / "SHA256SUMS.minisig").write_text(
        "untrusted comment: x\nAAAA\ntrusted comment: x\nAAAA\n"
    )
    proc = subprocess.run(
        ["sh", str(BOOTSTRAP)], capture_output=True, text=True,
        env=_sign_env(tmp_path, artifact_server, _stub_minisign(tmp_path, 1)),
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode != 0
    assert "signature verification FAILED" in proc.stderr
    # A failed signature must stop the install before anything is provisioned.
    _assert_nothing_provisioned(tmp_path)


def test_install_sh_verifies_and_proceeds_when_minisign_accepts(tmp_path, artifact_server):
    (artifact_server["root"] / artifact_server["version"] / "SHA256SUMS.minisig").write_text(
        "untrusted comment: x\nAAAA\ntrusted comment: x\nAAAA\n"
    )
    proc = subprocess.run(
        ["sh", str(BOOTSTRAP)], capture_output=True, text=True,
        env=_sign_env(tmp_path, artifact_server, _stub_minisign(tmp_path, 0)),
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, f"verified install must complete:\n{proc.stderr}"
    assert "signature verified" in proc.stdout
    assert "FIREKEEP_INSTALL_CALLED" in proc.stdout


def test_install_sh_warns_but_continues_when_the_release_is_unsigned(tmp_path, artifact_server):
    """verify-if-present: releases predating signing publish no .minisig, and
    `firekeep update --to <old>` must keep working — with a visible one-line warning,
    never silence and never a failure (until [dist] require_signed flips client-side)."""
    proc = subprocess.run(
        ["sh", str(BOOTSTRAP)], capture_output=True, text=True,
        env=_sign_env(tmp_path, artifact_server, _stub_minisign(tmp_path, 0)),
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, f"unsigned release must still install:\n{proc.stderr}"
    assert "not signed" in proc.stderr
    assert "FIREKEEP_INSTALL_CALLED" in proc.stdout


def test_install_sh_skips_signing_silently_on_a_bare_machine(tmp_path, artifact_server):
    """No minisign binary -> the whole block is a no-op: constraint 'must not break
    bare machines'. The checksum + TLS layer still applies in full."""
    proc = subprocess.run(
        ["sh", str(BOOTSTRAP)], capture_output=True, text=True,
        env=_sign_env(tmp_path, artifact_server, stub_dir=None),
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, f"install without minisign must succeed:\n{proc.stderr}"
    assert "FIREKEEP_INSTALL_CALLED" in proc.stdout


# --- the two-fetch split (security review, HIGH) --------------------------------
#
# `firekeep update` verifies <version>/SHA256SUMS.minisig against the client's pinned
# key — then re-execs this script, which used to fetch its OWN SHA256SUMS over the
# network and verify uv + the wheels against THAT. The client's fetch (urllib) and
# the bootstrap's (curl) are trivially distinguishable, so a malicious host could
# serve honest bytes to the verifier and attacker bytes to the installer: exit 0,
# attacker wheel installed, even under require_signed=true. The fix threads the
# verified bytes through as FIREKEEP_SUMS_FILE; under it the bootstrap makes NO
# sums/.minisig fetch at all. The server-side request log is what makes these
# tests proof rather than trust.


def _sums_requests(artifact_server):
    return [p for p in artifact_server["requests"] if "SHA256SUMS" in p]


def _handed_env(tmp_path, artifact_server, sums_file):
    return {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
            "FIREKEEP_DIST_BASE": artifact_server["base"],
            "FIREKEEP_VERSION": artifact_server["version"],
            "FIREKEEP_SUMS_FILE": str(sums_file)}


def test_install_sh_two_fetch_split_no_longer_works(tmp_path, artifact_server):
    """THE regression test for the HIGH finding, both halves proven:

    1. CONTROL: the server's artifacts and its served SHA256SUMS are mutually
       consistent (the exact state a split-serving host presents to fetch #2), so
       a network-fetching bootstrap installs them with exit 0. That is the attack
       working — on the path where it is still by-design accepted (manual TOFU
       install, no handed file).
    2. With the CLIENT-VERIFIED sums handed through FIREKEEP_SUMS_FILE — sums that
       describe the artifacts the signature actually covered, not what the host
       chose to serve — the same server is REFUSED on checksum mismatch, no venv
       is created, and the request log shows the bootstrap never fetched
       SHA256SUMS (or its .minisig) at all: fetch #2 does not happen.
    """
    # CONTROL — the attack-shaped server is accepted when nothing is handed:
    proc = subprocess.run(
        ["sh", str(BOOTSTRAP)], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "FIREKEEP_DIST_BASE": artifact_server["base"]},
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, f"control (manual install) should accept the host:\n{proc.stderr}"
    assert any(p.endswith("/SHA256SUMS") for p in _sums_requests(artifact_server))
    artifact_server["requests"].clear()

    # The client verified DIFFERENT sums than the host now serves: same uv (the
    # attacker only swapped the wheel), different wheel hash.
    vdir = artifact_server["root"] / artifact_server["version"]
    uv_digest = hashlib.sha256((vdir / f"uv-{artifact_server['target']}").read_bytes()).hexdigest()
    genuine_wheel_digest = hashlib.sha256(b"the wheel the signature covered\n").hexdigest()
    verified = tmp_path / "SHA256SUMS.verified"
    verified.write_text(
        f"{uv_digest}  uv-{artifact_server['target']}\n"
        f"{genuine_wheel_digest}  {artifact_server['wheel_name']}\n"
    )

    home2 = tmp_path / "home2"
    home2.mkdir()
    proc = subprocess.run(
        ["sh", str(BOOTSTRAP)], capture_output=True, text=True,
        env=_handed_env(home2, artifact_server, verified),
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode != 0, (
        "a host serving different bytes than the client verified must be REFUSED:\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
    assert "checksum mismatch" in proc.stderr.lower()
    _assert_nothing_provisioned(home2)
    assert _sums_requests(artifact_server) == [], (
        "under a handed FIREKEEP_SUMS_FILE the bootstrap must make NO SHA256SUMS "
        f"fetch — fetch #2 is the vulnerability. Saw: {_sums_requests(artifact_server)}"
    )


def test_install_sh_handed_sums_installs_without_any_sums_fetch(tmp_path, artifact_server):
    """The success half: handed sums matching the artifacts -> full install, zero
    SHA256SUMS/.minisig requests, and the hand-off announced on stdout."""
    verified = tmp_path / "SHA256SUMS.verified"
    verified.write_text(
        (artifact_server["root"] / artifact_server["version"] / "SHA256SUMS")
        .read_text()
    )
    artifact_server["requests"].clear()
    proc = subprocess.run(
        ["sh", str(BOOTSTRAP)], capture_output=True, text=True,
        env=_handed_env(tmp_path, artifact_server, verified),
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, f"handed-sums install must complete:\n{proc.stderr}"
    assert "FIREKEEP_INSTALL_CALLED" in proc.stdout
    assert "handed by firekeep update" in proc.stdout
    assert _sums_requests(artifact_server) == []
    _assert_current_selects_the_new_venv(tmp_path)


def test_install_sh_ignores_the_handed_sums_without_a_pinned_version(tmp_path, artifact_server):
    """FIREKEEP_SUMS_FILE without FIREKEEP_VERSION is not the client's hand-off shape
    (cmd_update always pins the version) — a manual run with a stray env var must
    fetch from the network exactly as before, not trust the stray file."""
    stray = tmp_path / "stray-sums"
    stray.write_text("00" * 32 + "  something\n")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           "FIREKEEP_DIST_BASE": artifact_server["base"],
           "FIREKEEP_SUMS_FILE": str(stray)}
    artifact_server["requests"].clear()
    proc = subprocess.run(["sh", str(BOOTSTRAP)], capture_output=True, text=True,
                          env=env, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, f"manual install must proceed on network sums:\n{proc.stderr}"
    assert any(p.endswith("/SHA256SUMS") for p in _sums_requests(artifact_server)), (
        "without the client's hand-off shape the sums must come from the network"
    )


def test_install_sh_dies_when_the_handed_sums_file_is_unusable(tmp_path, artifact_server):
    """Set-but-unreadable must be FATAL, never a silent fallback to a network fetch —
    the fallback IS the two-fetch vulnerability."""
    artifact_server["requests"].clear()
    proc = subprocess.run(
        ["sh", str(BOOTSTRAP)], capture_output=True, text=True,
        env=_handed_env(tmp_path, artifact_server, tmp_path / "does-not-exist"),
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode != 0
    assert "FIREKEEP_SUMS_FILE" in proc.stderr
    assert _sums_requests(artifact_server) == [], (
        "an unusable handed file must not degrade to the network fetch it replaces"
    )
    _assert_nothing_provisioned(tmp_path)
