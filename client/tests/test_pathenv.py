"""PATH management: put a single `firekeep` launcher on the user's PATH.

Design (pipx/rustup pattern): NEVER PATH the venv bin dir itself — it holds the
kit's standalone python/pip and every internal firekeep-* script, and prepending it
would shadow the user's own python3. Instead drop ONE launcher in ~/.firekeep/shims
and PATH only that dir.

Side-by-side venvs (client 0.1.35): the launcher targets the venv root the caller
rendered against — `current` (the alias flipped by updates; ~/.firekeep/current/bin
on POSIX, a `..\\current\\Scripts` relative hop in the Windows .cmd) on the new
layout, or the legacy `venv` on a not-yet-migrated install. NEVER a versioned
venvs/<X.Y.Z> path: the shim is the ONE stable path external schedulers may hold
(night-shift cron), and a versioned target dies the day GC removes that venv.

The Windows registry write is a thin injectable seam (`registry=`), so the merge
logic is exercised here on any host; the live winreg round-trip is unexercised on
macOS/Linux (documented).
"""
import os

import pytest

from firekeep_client import pathenv


# --- marker-block upsert / strip (idempotency) -------------------------------

def test_upsert_block_into_empty_is_a_single_block():
    out = pathenv._upsert_block("", 'export PATH="/x:$PATH"')
    assert out.count(pathenv._BEGIN) == 1
    assert out.count(pathenv._END) == 1
    assert 'export PATH="/x:$PATH"' in out


def test_upsert_is_idempotent():
    once = pathenv._upsert_block("existing\n", "BODY")
    twice = pathenv._upsert_block(once, "BODY")
    assert once == twice
    assert twice.count(pathenv._BEGIN) == 1
    assert twice.startswith("existing\n")


def test_upsert_collapses_all_stale_blocks():
    """A re-installed machine must not accumulate duplicate blocks (upsert_hook_group
    lesson): collapse EVERY prior firekeep block, not just the first."""
    b, e = pathenv._BEGIN, pathenv._END
    text = f"user\n{b}\nold1\n{e}\nmid\n{b}\nold2\n{e}\n"
    out = pathenv._upsert_block(text, "NEW")
    assert out.count(b) == 1
    assert "NEW" in out
    assert "old1" not in out and "old2" not in out
    assert "user" in out and "mid" in out


def test_strip_blocks_preserves_user_content():
    b, e = pathenv._BEGIN, pathenv._END
    text = f"line1\n{b}\nblock\n{e}\nline2\n"
    out = pathenv._strip_blocks(text)
    assert "block" not in out
    assert "line1" in out and "line2" in out


# --- POSIX rc-file target selection ------------------------------------------

@pytest.mark.parametrize("shell,primary", [
    ("zsh", ".zshrc"),
    ("bash", ".bashrc"),
    ("", ".profile"),
    ("dash", ".profile"),
    ("ksh", ".profile"),
])
def test_posix_rc_primary_by_shell(shell, primary, tmp_path):
    got_primary, _ = pathenv._posix_rc_targets(shell, tmp_path)
    assert got_primary == tmp_path / primary


def test_bash_targets_include_profile_extras(tmp_path):
    primary, extras = pathenv._posix_rc_targets("bash", tmp_path)
    assert primary == tmp_path / ".bashrc"
    assert tmp_path / ".bash_profile" in extras
    assert tmp_path / ".profile" in extras


# --- POSIX ensure_on_path end to end -----------------------------------------
# Skipped on Windows, structurally: since Python 3.8 expanduser/Path.home() on
# Windows ignore a monkeypatched HOME (USERPROFILE wins), so these tests cannot
# sandbox the rc-file writes — they would hit the runner's real profile. The
# POSIX branch is POSIX logic; the Windows branch is covered by the registry-seam
# tests below, which run on every host.
_posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX rc/launcher branch; HOME is not sandboxable on Windows")


def _mk_home(tmp_path):
    # The side-by-side layout: cmd_install renders against _venv_root(home) ==
    # home/current once the alias exists. A plain dir stands in for the symlink —
    # the launcher only embeds the PATH STRING, and the link primitive itself is
    # exercised by test_cli_install/test_kit_smoke via cli._point_current.
    home = tmp_path / ".firekeep"
    venv_bin = home / "current" / "bin"
    venv_bin.mkdir(parents=True)
    return home, venv_bin


@_posix_only
def test_ensure_posix_writes_executable_launcher_and_rc(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    home, venv_bin = _mk_home(tmp_path)

    pathenv.ensure_on_path(home, venv_bin, windows=False)

    launcher = home / "shims" / "firekeep"
    assert launcher.exists()
    assert os.access(launcher, os.X_OK), "launcher must be executable"
    # It must exec the REAL venv firekeep, not shadow anything. The POSIX
    # launcher embeds the venv_bin path ABSOLUTELY — home/current/bin under the
    # side-by-side layout, so updates flip the symlink and this file never
    # changes — and must never embed a versioned venvs/ path (GC removes those).
    text = launcher.read_text()
    assert f'"{venv_bin}/firekeep"' in text
    assert "venvs" not in text

    rc = tmp_path / ".zshrc"
    assert rc.exists()
    body = rc.read_text()
    assert str(home / "shims") in body
    # Only the shim dir goes on PATH — never venv/bin.
    assert str(venv_bin) not in body


@_posix_only
def test_ensure_posix_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "zsh")
    home, venv_bin = _mk_home(tmp_path)

    pathenv.ensure_on_path(home, venv_bin, windows=False)
    pathenv.ensure_on_path(home, venv_bin, windows=False)

    rc = (tmp_path / ".zshrc").read_text()
    assert rc.count(pathenv._BEGIN) == 1


@_posix_only
def test_ensure_posix_updates_existing_extras_only(tmp_path, monkeypatch):
    """bash: create .bashrc (primary), update .bash_profile IFF it already exists,
    and never CREATE .profile (creating a .bash_profile-that-shadows-.profile, or a
    bare .profile, would disrupt the login-shell sourcing chain)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/usr/bin/bash")
    home, venv_bin = _mk_home(tmp_path)
    (tmp_path / ".bash_profile").write_text("# pre-existing\n")

    pathenv.ensure_on_path(home, venv_bin, windows=False)

    assert (tmp_path / ".bashrc").exists()  # primary created
    assert pathenv._BEGIN in (tmp_path / ".bash_profile").read_text()  # extra updated
    assert "# pre-existing" in (tmp_path / ".bash_profile").read_text()  # not clobbered
    assert not (tmp_path / ".profile").exists()  # extra NOT created


@_posix_only
def test_ensure_fish_writes_conf_d_not_a_silent_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/usr/local/bin/fish")
    home, venv_bin = _mk_home(tmp_path)

    pathenv.ensure_on_path(home, venv_bin, windows=False)

    conf = tmp_path / ".config" / "fish" / "conf.d" / "firekeep.fish"
    assert conf.exists()
    txt = conf.read_text()
    assert "fish_add_path" in txt
    assert str(home / "shims") in txt


@_posix_only
def test_bash_pristine_home_creates_profile_for_login_shells(tmp_path, monkeypatch):
    """bash login shells (macOS Terminal, ssh) read .bash_profile/.bash_login/.profile,
    never .bashrc. On a pristine home, .profile must be created so `firekeep` is on PATH
    in a new login terminal."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    home, venv_bin = _mk_home(tmp_path)

    pathenv.ensure_on_path(home, venv_bin, windows=False)

    assert (tmp_path / ".bashrc").exists()  # primary
    prof = tmp_path / ".profile"
    assert prof.exists(), "pristine bash home must get a .profile for login shells"
    assert str(home / "shims") in prof.read_text()


@_posix_only
def test_bash_leaves_existing_login_chain_alone(tmp_path, monkeypatch):
    """If a login file already exists, don't inject a new .profile (that could shadow
    the user's existing chain) — update the existing one instead."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "bash")
    home, venv_bin = _mk_home(tmp_path)
    (tmp_path / ".bash_profile").write_text("# mine\n")

    pathenv.ensure_on_path(home, venv_bin, windows=False)

    assert pathenv._BEGIN in (tmp_path / ".bash_profile").read_text()
    assert not (tmp_path / ".profile").exists()  # not created — chain untouched


@_posix_only
def test_non_utf8_rc_file_does_not_crash_and_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "zsh")
    home, venv_bin = _mk_home(tmp_path)
    # A Latin-1 comment byte (0xe9 = 'é') that is not valid UTF-8.
    (tmp_path / ".zshrc").write_bytes(b"# caf\xe9 config\n")

    pathenv.ensure_on_path(home, venv_bin, windows=False)  # must not raise

    raw = (tmp_path / ".zshrc").read_bytes()
    assert b"\xe9" in raw  # original non-UTF-8 byte preserved
    assert pathenv._BEGIN.encode() in raw  # block appended


def test_sh_dq_escapes_shell_metacharacters():
    out = pathenv._sh_dq(r'/home/a$b`c"d\e')
    assert out == r'/home/a\$b\`c\"d\\e'


def test_atomic_write_replaces_and_leaves_no_tmp(tmp_path):
    target = tmp_path / "rc"
    target.write_text("old\n")
    pathenv._atomic_write(target, "new\n")
    assert target.read_text() == "new\n"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".firekeep-")]
    assert leftovers == [], f"temp files must be cleaned up, found {leftovers}"


# --- Windows: pure merge logic (host-independent) ----------------------------

def test_windows_merge_prepends_when_absent():
    assert pathenv._windows_merge_path(None, r"C:\shims") == r"C:\shims"
    assert pathenv._windows_merge_path("", r"C:\shims") == r"C:\shims"
    assert pathenv._windows_merge_path(r"C:\foo", r"C:\shims") == r"C:\shims;C:\foo"


def test_windows_merge_dedups_case_and_sep_insensitive():
    assert pathenv._windows_merge_path(r"C:\Shims;C:\foo", r"c:\shims") is None
    assert pathenv._windows_merge_path("C:/shims", r"C:\shims") is None


# --- Windows: ensure_on_path with an injected registry seam ------------------

class _FakeReg:
    def __init__(self, value=None, regtype=1):
        self.value = value
        self.regtype = regtype
        self.writes = []
        self.broadcasts = 0

    def read(self):
        return self.value, self.regtype

    def write(self, value, regtype):
        self.value = value
        self.writes.append((value, regtype))

    def broadcast(self):
        self.broadcasts += 1


def test_ensure_windows_writes_cmd_and_registry(tmp_path):
    home = tmp_path / ".firekeep"
    venv_bin = home / "current" / "Scripts"  # side-by-side alias layout
    venv_bin.mkdir(parents=True)
    reg = _FakeReg(value=r"C:\existing", regtype=2)  # 2 == REG_EXPAND_SZ

    pathenv.ensure_on_path(home, venv_bin, windows=True, registry=reg)

    cmd = home / "shims" / "firekeep.cmd"
    assert cmd.exists()
    # The EXACT relative hop, not a substring of the exe name — see
    # test_windows_launcher_hop_is_exact_for_the_current_layout for why the
    # loose assertion was a silent-green gap.
    assert '"%~dp0..\\current\\Scripts\\firekeep.exe" %*' in cmd.read_text()

    assert reg.writes, "registry must be written when the entry is absent"
    written, regtype = reg.writes[0]
    assert str(home / "shims") in written
    assert r"C:\existing" in written  # existing PATH preserved
    assert regtype == 2, "REG_EXPAND_SZ type must be preserved, not frozen to REG_SZ"
    assert reg.broadcasts == 1  # new shells pick it up without logoff


def test_ensure_windows_idempotent_when_already_present(tmp_path):
    home = tmp_path / ".firekeep"
    venv_bin = home / "current" / "Scripts"
    venv_bin.mkdir(parents=True)
    reg = _FakeReg(value=str(home / "shims"), regtype=1)

    pathenv.ensure_on_path(home, venv_bin, windows=True, registry=reg)

    assert reg.writes == [], "already present: registry must not be rewritten"


# --- Windows launcher hop: exact target per layout (side-by-side venvs) -------

def _windows_launcher_text(tmp_path, root_name):
    home = tmp_path / ".firekeep"
    venv_bin = home / root_name / "Scripts"
    venv_bin.mkdir(parents=True)
    launcher = pathenv._write_launcher(home / "shims", venv_bin, windows=True)
    return launcher.read_text(encoding="utf-8")


def test_windows_launcher_hop_is_exact_for_the_current_layout(tmp_path):
    """CLOSES A SILENT-GREEN GAP: the old assertion was `"firekeep.exe" in text`,
    which stays green no matter WHERE the hop points — a launcher targeting
    ..\\venv\\, ..\\venvs\\0.1.35\\, or an absolute C:\\ path all contain that
    substring. The hop is the load-bearing part: rendered against the
    side-by-side alias it must be exactly ..\\current\\Scripts\\firekeep.exe,
    because `current` is what update flips — this file is written once and must
    keep working across every future version without a re-render."""
    text = _windows_launcher_text(tmp_path, "current")
    assert '"%~dp0..\\current\\Scripts\\firekeep.exe" %*' in text


def test_windows_launcher_hop_stays_venv_on_a_legacy_install(tmp_path):
    """A pre-0.1.35 install (no `current` alias yet) renders against the legacy
    home/venv — the hop must then be ..\\venv\\..., or `firekeep install` run on
    a not-yet-migrated machine writes a launcher pointing at a dir that does not
    exist and the command dies on the spot."""
    text = _windows_launcher_text(tmp_path, "venv")
    assert '"%~dp0..\\venv\\Scripts\\firekeep.exe" %*' in text


def test_windows_launcher_never_embeds_a_versioned_venvs_path(tmp_path):
    """The .cmd shim is the ONE path external callers (user PATH, schedulers)
    hold forever. A `venvs` component in it would pin the launcher to a
    versioned venv that a later update's GC deletes — the launcher keeps
    resolving right up to the sweep, then every `firekeep` invocation dies
    file-not-found with nothing left to name the cause. Both legitimate render
    inputs must produce a venvs-free launcher."""
    for root_name in ("current", "venv"):
        text = _windows_launcher_text(tmp_path / root_name, root_name)
        assert "venvs" not in text, (
            f"launcher rendered against {root_name!r} embeds a GC-able venvs path"
        )


# --- removal (future uninstall) ----------------------------------------------

@_posix_only
def test_remove_from_path_posix_strips_block_and_launcher(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "zsh")
    home, venv_bin = _mk_home(tmp_path)
    pathenv.ensure_on_path(home, venv_bin, windows=False)

    pathenv.remove_from_path(home, windows=False)

    assert pathenv._BEGIN not in (tmp_path / ".zshrc").read_text()
    assert not (home / "shims").exists()
