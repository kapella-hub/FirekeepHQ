"""Put a single `firekeep` launcher on the user's PATH — cross-platform, idempotent.

WHY A SHIM DIR, NOT ~/.firekeep/venv/bin: that venv bin dir holds the kit's
standalone CPython (`python`, `python3`, `pip`) and every INTERNAL entry point
(`firekeep-shim`, `firekeep-sidecar`, `firekeep-decision`, `firekeep-symdex`). Prepending it
to PATH would shadow the user's own `python3` and litter their PATH with scripts
they must never run directly. Instead we drop ONE launcher — `firekeep` — into a
dedicated `~/.firekeep/shims` dir and PATH only that (the pipx/rustup pattern). A
one-command dir also makes prepend safe: there is nothing real to shadow.

Stdlib-only. Best-effort by contract: the caller wraps this so a PATH failure
never fails the install. Idempotent: re-running collapses to one marker block
(POSIX) / one PATH entry (Windows).
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

SHIM_DIR_NAME = "shims"

# Shell-comment markers delimiting the firekeep-owned block in an rc file. Only text
# between these is ever touched; user content on either side survives byte-for-byte.
_BEGIN = "# >>> firekeep (managed by `firekeep install`; do not edit) >>>"
_END = "# <<< firekeep <<<"

_BLOCK_RE = re.compile(
    re.escape(_BEGIN) + r".*?" + re.escape(_END) + r"[ \t]*\n?",
    re.DOTALL,
)


# --- marker-block upsert / strip ---------------------------------------------

def _strip_blocks(text: str) -> str:
    """Remove EVERY firekeep block (collapse duplicates a re-install could accumulate)."""
    return _BLOCK_RE.sub("", text)


def _upsert_block(text: str, body: str) -> str:
    stripped = _strip_blocks(text)
    block = f"{_BEGIN}\n{body}\n{_END}\n"
    if not stripped:
        return block
    if not stripped.endswith("\n"):
        stripped += "\n"
    return stripped + block


def _atomic_write(path: Path, data: str) -> None:
    """Write via a sibling temp file + os.replace so the target is swapped in one
    atomic rename — a crash mid-write can never leave a truncated shell rc. Bytes
    that aren't valid UTF-8 round-trip via surrogateescape (a user's Latin-1 .profile
    must not crash, nor be corrupted)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".firekeep-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _upsert_file(path: Path, body: str) -> None:
    # surrogateescape on read too, so a non-UTF-8 rc doesn't raise (and PATH setup
    # silently no-op) — the bytes round-trip unchanged through the rewrite.
    text = path.read_text(encoding="utf-8", errors="surrogateescape") if path.exists() else ""
    _atomic_write(path, _upsert_block(text, body))


def _sh_dq(s: str) -> str:
    """Escape a string for a POSIX double-quoted context (`"..."`). A home dir with
    a `$`, backtick, `"`, or `\\` in it would otherwise break the generated line."""
    for ch in ("\\", '"', "$", "`"):
        s = s.replace(ch, "\\" + ch)
    return s


def _fish_dq(s: str) -> str:
    """Escape for a fish double-quoted string (fish treats `\\`, `"`, `$` specially;
    backticks are literal)."""
    for ch in ("\\", '"', "$"):
        s = s.replace(ch, "\\" + ch)
    return s


# --- POSIX -------------------------------------------------------------------

def _posix_rc_targets(shell: str, home: Path) -> tuple[Path, list[Path]]:
    """(primary, extras). `primary` is created if missing; each `extra` is updated
    ONLY when it already exists — creating a bare .bash_profile/.profile can break a
    login shell's existing sourcing chain."""
    if shell == "zsh":
        return home / ".zshrc", []
    if shell == "bash":
        return home / ".bashrc", [home / ".bash_profile", home / ".profile"]
    # sh, dash, ksh, unset, or anything unrecognized: the POSIX login fallback.
    return home / ".profile", []


def _ensure_posix(shim_dir: Path) -> list[str]:
    home = Path.home()
    shell = os.path.basename(os.environ.get("SHELL", "")).strip().lower()
    entry = str(shim_dir)

    if shell == "fish":
        # fish reads neither .zshrc nor .profile and uses its own syntax — a dedicated
        # conf.d file (idempotent by way of fish_add_path) rather than a silent no-op.
        conf = home / ".config" / "fish" / "conf.d" / "firekeep.fish"
        _atomic_write(conf, f'{_BEGIN}\nfish_add_path "{_fish_dq(entry)}"\n{_END}\n')
        return [f"added {entry} to PATH via {conf}",
                "open a new terminal to use `firekeep`"]

    primary, extras = _posix_rc_targets(shell, home)
    body = f'export PATH="{_sh_dq(entry)}:$PATH"'
    touched = [primary]
    _upsert_file(primary, body)
    for rc in extras:
        if rc.exists():
            _upsert_file(rc, body)
            touched.append(rc)
    # bash login shells (macOS Terminal, `ssh host`) read the FIRST of
    # .bash_profile/.bash_login/.profile and never .bashrc. On a pristine home where
    # none exist, the primary .bashrc write above would leave `firekeep` off PATH in a
    # new login terminal — so create .profile (the lowest-priority login file: it
    # shadows nothing) to close that gap. If any login file already exists we leave
    # the chain alone (that's the extras-only-if-exist rule above).
    if shell == "bash":
        login_files = [home / ".bash_profile", home / ".bash_login", home / ".profile"]
        if not any(f.exists() for f in login_files):
            _upsert_file(home / ".profile", body)
            touched.append(home / ".profile")
    names = ", ".join(str(p) for p in touched)
    return [f"added {entry} to PATH in {names}",
            f"run `source {primary}` or open a new terminal to use `firekeep`"]


# --- Windows -----------------------------------------------------------------

def _win_norm(p: str) -> str:
    """Windows PATH-entry comparison key: separator- and case-insensitive."""
    return p.replace("/", "\\").rstrip("\\").lower()


def _windows_merge_path(current: str | None, entry: str) -> str | None:
    """Return the new user PATH with `entry` prepended, or None if already present."""
    parts = [p for p in (current or "").split(";") if p]
    target = _win_norm(entry)
    if any(_win_norm(p) == target for p in parts):
        return None
    return ";".join([entry, *parts])


class _WinRegistry:
    """Thin winreg seam over HKCU\\Environment `Path`. Injectable so the merge logic
    is testable off-Windows; this class itself only runs on real Windows."""

    _SUBKEY = "Environment"

    def read(self) -> tuple[str | None, int]:
        import winreg  # noqa: PLC0415 — Windows-only, imported at call time
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._SUBKEY) as key:
            try:
                value, regtype = winreg.QueryValueEx(key, "Path")
                return value, regtype
            except FileNotFoundError:
                # No user Path yet: default to REG_EXPAND_SZ (what Windows itself uses).
                return None, winreg.REG_EXPAND_SZ

    def write(self, value: str, regtype: int) -> None:
        import winreg  # noqa: PLC0415
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, self._SUBKEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "Path", 0, regtype, value)

    def broadcast(self) -> None:
        # Tell running shells/Explorer the environment changed so a new terminal
        # sees the PATH without a logoff. Best-effort — never fatal.
        try:
            import ctypes  # noqa: PLC0415

            HWND_BROADCAST = 0xFFFF
            WM_SETTINGCHANGE = 0x1A
            SMTO_ABORTIFHUNG = 0x2
            ctypes.windll.user32.SendMessageTimeoutW(
                HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
                SMTO_ABORTIFHUNG, 5000, ctypes.byref(ctypes.c_ulong()),
            )
        except Exception:  # noqa: BLE001 — cosmetic refresh only
            pass


def _ensure_windows(shim_dir: Path, registry) -> list[str]:
    entry = str(shim_dir)
    current, regtype = registry.read()
    new = _windows_merge_path(current, entry)
    if new is None:
        return [f"{entry} is already on your user PATH"]
    registry.write(new, regtype)
    registry.broadcast()
    return [f"added {entry} to your user PATH (registry); "
            "open a new terminal to use `firekeep`"]


# --- launcher ----------------------------------------------------------------

def _write_launcher(shim_dir: Path, venv_bin: Path, windows: bool) -> Path:
    shim_dir.mkdir(parents=True, exist_ok=True)
    if windows:
        launcher = shim_dir / "firekeep.cmd"
        # %~dp0 is the .cmd's own dir (…/shims). The venv root and shims are
        # siblings under ~/.firekeep, so a relative hop avoids freezing an
        # absolute home path AND sidesteps path-separator rendering. The hop
        # targets whatever root the caller rendered against — `current` (the
        # side-by-side junction, so this file never changes across updates) or
        # the legacy `venv` on a not-yet-migrated install. Never a versioned
        # venvs\<X.Y.Z> path: that would pin the launcher to a venv GC removes.
        root_name = venv_bin.parent.name
        launcher.write_text(
            f'@echo off\r\n"%~dp0..\\{root_name}\\Scripts\\firekeep.exe" %*\r\n',
            encoding="utf-8",
        )
        return launcher
    launcher = shim_dir / "firekeep"
    launcher.write_text(
        f'#!/bin/sh\n# firekeep launcher (managed by `firekeep install`)\n'
        f'exec "{_sh_dq(str(venv_bin))}/firekeep" "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher


# --- public API --------------------------------------------------------------

def ensure_on_path(home: Path, venv_bin: Path, *, windows: bool | None = None,
                   registry=None) -> list[str]:
    """Write the `firekeep` launcher into ~/.firekeep/shims and put that dir on PATH.

    Returns human-readable message lines describing what changed (for the installer
    to print). Idempotent. `windows`/`registry` are injectable for tests."""
    if windows is None:
        windows = os.name == "nt"
    shim_dir = home / SHIM_DIR_NAME
    launcher = _write_launcher(shim_dir, venv_bin, windows)
    msgs = [f"wrote launcher {launcher}"]
    if windows:
        msgs += _ensure_windows(shim_dir, registry or _WinRegistry())
    else:
        msgs += _ensure_posix(shim_dir)
    return msgs


def remove_from_path(home: Path, *, windows: bool | None = None,
                     registry=None) -> list[str]:
    """Inverse of ensure_on_path — strip the block/entry and delete the shim dir.
    Ready for a future `firekeep uninstall`; not wired to a command today."""
    if windows is None:
        windows = os.name == "nt"
    shim_dir = home / SHIM_DIR_NAME
    entry = str(shim_dir)
    msgs: list[str] = []

    if windows:
        reg = registry or _WinRegistry()
        current, regtype = reg.read()
        parts = [p for p in (current or "").split(";")
                 if p and _win_norm(p) != _win_norm(entry)]
        reg.write(";".join(parts), regtype)
        reg.broadcast()
        msgs.append(f"removed {entry} from your user PATH")
    else:
        home_dir = Path.home()
        for rc in (home_dir / ".zshrc", home_dir / ".bashrc",
                   home_dir / ".bash_profile", home_dir / ".profile"):
            if not rc.exists():
                continue
            text = rc.read_text(encoding="utf-8", errors="surrogateescape")
            stripped = _strip_blocks(text)
            if stripped != text:
                _atomic_write(rc, stripped)
                msgs.append(f"removed firekeep block from {rc}")
        fish = home_dir / ".config" / "fish" / "conf.d" / "firekeep.fish"
        if fish.exists():
            fish.unlink()
            msgs.append(f"removed {fish}")

    if shim_dir.exists():
        for child in shim_dir.iterdir():
            child.unlink()
        shim_dir.rmdir()
        msgs.append(f"removed {shim_dir}")
    return msgs
