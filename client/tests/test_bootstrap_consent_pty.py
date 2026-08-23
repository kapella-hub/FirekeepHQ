# client/tests/test_bootstrap_consent_pty.py
"""The consent prompt under a REAL controlling terminal (spec Testing,
'Real-terminal acceptance'). The existing e2e deliberately DETACHES the tty
(test_e2e_bootstrap.py:36), so the interactive branch has zero coverage
without this. POSIX-only: Windows has no stdlib ConPTY binding and the client
test suite is stdlib-only — the ps1 prompt is covered by the literal test
above plus the manual checklist in the plan's final verification.

Exercises ONLY the consent block: the script is cut down to everything up to
(and excluding) the TLS section, plus a probe line — provisioning under a PTY
is the e2e suite's job, not this test's.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty only")

# `pty` unconditionally imports `tty` -> `termios`, which does not exist on Windows.
# The skipif marker above only skips test *execution*; collection still imports this
# module on every platform, so the import itself must be guarded or a Windows run
# fails at collection with ModuleNotFoundError instead of skipping cleanly.
if sys.platform != "win32":
    import pty
    import select

BOOTSTRAP = Path(__file__).resolve().parents[1] / "bootstrap" / "install.sh"


def _consent_snippet(tmp_path):
    """install.sh from the consent block start to report_failure's end, with a
    probe that prints the resulting state and exits before any real work."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    start = text.index("# --- field-failure consent")
    end = text.index("# --- TLS trust", start)
    snippet = ("#!/bin/sh\nset -u\nPYTHON_VERSION=3.12\n"
               + text[start:end]
               + '\nprintf "CONSENT=%s EXPORTED=%s\\n" '
                 '"${REPORT_CONSENT}" "${FIREKEEP_REPORT_CONSENT-unset}"\n')
    script = tmp_path / "consent-only.sh"
    script.write_text(snippet, encoding="utf-8")
    script.chmod(0o755)
    return script


def _run_under_pty(script, keys, env=None):
    parent, child = pty.openpty()
    proc = subprocess.Popen(["sh", str(script)], stdin=child, stdout=child,
                            stderr=child, env=dict(os.environ, **(env or {})),
                            close_fds=True, start_new_session=False)
    os.close(child)
    output = b""
    wrote = False
    try:
        while proc.poll() is None or select.select([parent], [], [], 0.1)[0]:
            r, _, _ = select.select([parent], [], [], 2.0)
            if not r:
                if proc.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(parent, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
            if not wrote and b"[Y/n]" in output:
                os.write(parent, keys)
                wrote = True
    finally:
        os.close(parent)
        proc.wait(10)
    return output.decode("utf-8", "replace")


def test_enter_records_yes(tmp_path):
    out = _run_under_pty(_consent_snippet(tmp_path), b"\n")
    assert "CONSENT=1 EXPORTED=1" in out


def test_n_records_no(tmp_path):
    out = _run_under_pty(_consent_snippet(tmp_path), b"n\n")
    assert "CONSENT=0 EXPORTED=0" in out


def test_eof_records_nothing(tmp_path):
    # ^D at the prompt: unanswered — nothing exported, nothing reported
    out = _run_under_pty(_consent_snippet(tmp_path), b"\x04")
    assert "CONSENT=0 EXPORTED=unset" in out


def test_env_opt_out_never_asks(tmp_path):
    out = _run_under_pty(_consent_snippet(tmp_path), b"",
                         env={"FIREKEEP_NO_FAILURE_REPORT": "1"})
    assert "[Y/n]" not in out
    assert "CONSENT=0 EXPORTED=unset" in out
