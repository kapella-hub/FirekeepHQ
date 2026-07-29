"""The bootstrap must never delete a venv that a live session is running from.

The bug
-------
`install.sh` provisioned with ``uv venv "${VENV}" --clear``: it DELETED
``~/.firekeep/venv`` and then took 30-120s to repopulate it. The rationale, stated
in `autoupdate.py`, was that this is safe on POSIX because unlink keeps running
processes alive on their old inodes.

That is only half true. Unlink protects files a process has **already mapped**. It
does nothing for a **new exec** — and every lifecycle hook spawns a fresh
``${VENV}/bin/python``: PreToolUse (blocking, gates every Edit), PostToolUse,
UserPromptSubmit, SessionStart, Stop, plus three stdio MCP servers on reconnect.

So for the whole reinstall window every hook on every live macOS/Linux session
failed with "No such file or directory" — and background auto-update is ON by
default, so nobody asked for that window to open. Windows was never exposed: its
bootstrap refuses outright to overwrite a venv held by live processes. POSIX had no
equivalent guard.

The fix is build-beside-then-swap: provision into ``${VENV}.new``, verify it
imports, then two ``mv`` calls. The window where nothing is at ``${VENV}`` shrinks
from a full reinstall to one ``rename(2)``.

Why these tests are shaped like this
------------------------------------
`test_bootstrap_sh.py` drives the whole real script, but it is unrunnable on a
Windows dev box for two independent reasons (`os.name == "nt"` skip, and
`conftest._uv_target()` raising KeyError for `platform.system() == "Windows"`),
so it verified nothing about this change here.

These tests split the difference: the SEMANTIC tests extract the swap block from
the real `install.sh` and execute it under `sh` with a fake venv tree, which works
anywhere `sh` exists — including Git Bash. The STRUCTURAL tests read the script and
hold the invariants that make the semantics possible. Together they fail if the
`--clear` behaviour comes back.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

BOOTSTRAP = Path(__file__).resolve().parents[1] / "bootstrap" / "install.sh"
SH = shutil.which("sh")

SRC = BOOTSTRAP.read_text(encoding="utf-8")


# ─── structural: the invariants the semantics rest on ────────────────────────

class TestTheLiveVenvIsNeverCleared:
    def test_uv_venv_does_not_target_the_live_venv(self):
        """`uv venv "${VENV}"` is the bug. It must target the staging dir."""
        for line in SRC.splitlines():
            s = line.strip()
            if s.startswith("#") or "uv" not in s or " venv " not in s:
                continue
            assert '"${VENV}"' not in s, (
                "uv venv targets the LIVE venv. Every lifecycle hook execs "
                f"${{VENV}}/bin/python, so this deletes the interpreter out from under "
                f"running sessions:\n  {s}"
            )

    def test_no_clear_flag_survives(self):
        """--clear is what made the deletion destructive rather than a refusal."""
        for line in SRC.splitlines():
            s = line.strip()
            if s.startswith("#") or " venv " not in s:
                continue
            assert "--clear" not in s, (
                f"--clear deletes an existing venv in place; staging removes the need "
                f"for it entirely:\n  {s}"
            )

    def test_provisioning_targets_a_staging_directory(self):
        assert 'STAGE="${VENV}.new"' in SRC, (
            "no staging directory. Provisioning must build beside the live venv."
        )

    def test_wheels_install_into_the_staging_tree(self):
        installs = [
            ln.strip() for ln in SRC.splitlines()
            if "pip install" in ln and not ln.strip().startswith("#")
        ]
        assert installs, "no pip install lines found"
        for ln in installs:
            assert "${STAGE}" in ln, (
                f"a wheel installs somewhere other than the staging tree:\n  {ln}"
            )

    def test_the_staged_tree_is_smoke_tested_before_the_swap(self):
        """Provisioning can succeed and still produce something unusable. Staging
        only buys safety if the swap is gated on the new tree actually working.

        The gate checks that `bin/firekeep` is EXECUTABLE, not that
        `firekeep_client` imports. The import form was written first and failed the
        POSIX bootstrap suite on CI: the harness's stub uv creates bin/firekeep and
        deliberately no-ops `pip install`, so nothing was importable and the guard
        aborted every install. A runnable firekeep is also the ACTUAL precondition,
        since running it is the bootstrap's very next act.
        """
        i, j = SRC.find('STAGE="${VENV}.new"'), SRC.find('mv "${STAGE}" "${VENV}"')
        assert i != -1 and j != -1 and i < j
        assert 'STAGE}/bin/firekeep' in SRC[i:j], (
            "nothing verifies the staged kit is usable before it replaces a working "
            "install — a bad wheel would be swapped in and break the machine"
        )

    def test_a_failed_swap_restores_the_previous_install(self):
        i = SRC.find('if ! mv "${STAGE}" "${VENV}"')
        assert i != -1, "the second rename is unguarded"
        block = SRC[i:i + 400]
        assert 'mv "${OLD}" "${VENV}"' in block, (
            "if the new tree cannot be moved into place, the old one must go back — "
            "otherwise the machine is left with no venv at all"
        )

    def test_the_old_tree_is_removed_only_after_the_swap(self):
        """Deleting before the swap would reintroduce the original bug."""
        swap = SRC.find('mv "${STAGE}" "${VENV}"')
        cleanup = SRC.find('rm -rf "${OLD}"')
        assert cleanup > swap, "the old venv is deleted before the swap completes"


# ─── semantic: run the real swap block ───────────────────────────────────────

SWAP_START = '# --- 7c. SWAP'
SWAP_END = '# --- 8.'


def _swap_block() -> str:
    i, j = SRC.find(SWAP_START), SRC.find(SWAP_END)
    assert i != -1 and j != -1, "swap block markers missing from install.sh"
    body = SRC[i:j]
    # Drop the usability guard: it needs a real staged tree containing a firekeep
    # executable, and these tests exercise the RENAME semantics against a fake one.
    # Its presence is asserted structurally above.
    #
    # Matches on the PATH anywhere in the line, because the guard reads
    # `if [ ! -x "${STAGE}/bin/firekeep" ]; then` -- a match anchored to the start
    # of the expression would never fire, and every semantic test would then abort
    # inside the guard rather than exercising the renames.
    out, in_guard = [], False
    for ln in body.splitlines():
        if not in_guard and "STAGE}/bin/firekeep" in ln and ln.strip().startswith("if"):
            in_guard = True
            continue
        if in_guard:
            if ln.strip() == "fi":
                in_guard = False
            continue
        out.append(ln)
    return "\n".join(out)


@pytest.mark.skipif(SH is None, reason="sh required")
class TestSwapSemantics:
    """Execute the real block against a fake tree. Runs anywhere sh exists."""

    def _run(self, tmp_path: Path, *, with_existing: bool) -> subprocess.CompletedProcess:
        venv = tmp_path / "venv"
        stage = tmp_path / "venv.new"
        (stage / "bin").mkdir(parents=True)
        (stage / "bin" / "python").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (stage / "MARKER_NEW").write_text("new", encoding="utf-8")
        if with_existing:
            (venv / "bin").mkdir(parents=True)
            (venv / "MARKER_OLD").write_text("old", encoding="utf-8")

        script = (
            "set -e\n"
            f'VENV="{venv.as_posix()}"\n'
            f'STAGE="{stage.as_posix()}"\n'
            'die() { echo "$1" >&2; exit 1; }\n'
            + _swap_block()
        )
        return subprocess.run(
            [SH, "-c", script], capture_output=True, text=True, timeout=60
        )

    def test_a_fresh_install_lands_the_new_tree(self, tmp_path):
        r = self._run(tmp_path, with_existing=False)
        assert r.returncode == 0, r.stderr
        assert (tmp_path / "venv" / "MARKER_NEW").is_file(), "new tree not in place"
        assert not (tmp_path / "venv.new").exists(), "staging dir left behind"

    def test_an_upgrade_replaces_the_old_tree(self, tmp_path):
        r = self._run(tmp_path, with_existing=True)
        assert r.returncode == 0, r.stderr
        assert (tmp_path / "venv" / "MARKER_NEW").is_file(), "new tree not in place"
        assert not (tmp_path / "venv" / "MARKER_OLD").exists(), "old content survived"

    def test_no_old_directory_is_left_behind(self, tmp_path):
        r = self._run(tmp_path, with_existing=True)
        assert r.returncode == 0, r.stderr
        leftovers = [p.name for p in tmp_path.iterdir() if ".old." in p.name]
        assert not leftovers, f"orphaned old tree(s): {leftovers}"

    def test_the_live_venv_is_never_absent_for_long(self, tmp_path):
        """The point of the whole change. The old code deleted ${VENV} and then ran
        a 30-120s install; the new code only renames, so ${VENV} is missing for the
        duration of two rename() calls and nothing else.

        Asserted structurally rather than by timing: no command that provisions or
        installs may appear between the two renames."""
        block = _swap_block()
        i = block.find('mv "${VENV}" "${OLD}"')
        j = block.find('mv "${STAGE}" "${VENV}"')
        assert i != -1 and j != -1 and i < j
        between = block[i:j]
        for forbidden in ("pip install", "uv venv", "fetch ", "curl", "wget"):
            assert forbidden not in between, (
                f"{forbidden!r} runs while ${{VENV}} does not exist — that is the "
                f"original bug in a new shape"
            )
