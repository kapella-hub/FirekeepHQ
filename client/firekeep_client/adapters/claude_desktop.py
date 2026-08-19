"""Claude Desktop adapter: the `firekeep` entry in `claude_desktop_config.json`.

Claude Desktop is the first NON-CODING host the kit ships a bespoke adapter for
— the consumer chat app, not an agentic IDE. It runs local stdio MCP servers
from one JSON config file, so this adapter delivers exactly the generic tier
with the friction removed: the same gateway entry the generic adapter PRINTS
for pasting is WRITTEN here, because unlike a client we know nothing about,
Claude Desktop's config path is documented and stable per platform.

What it deliberately does NOT do:
- No hooks. Claude Desktop exposes no hook surface — no session lifecycle, no
  prompt interception, no pre-edit gate. Its column in contract/matrix.py is
  the generic floor, and claiming more would be the lie the matrix exists to
  prevent.
- No instruction file. Claude Desktop reads no rules file the kit could safely
  own; the cognitive protocol arrives through the MCP `initialize` handshake
  (GATEWAY_INSTRUCTIONS), the same second channel every runtime gets.

The config is JSON, which has no comments and no partial-edit syntax, so the
file is parsed and re-serialized rather than marker-blocked. Only the
`mcpServers.firekeep` key is ever set or removed; every other key — other
servers, app settings — survives byte-for-byte at the VALUE level (formatting
is normalized to indent=2, which Claude Desktop itself also does on save).
A file that does not parse is REFUSED loudly and left exactly as it is:
clobbering a consumer app's config because we could not read it would be the
worst possible outcome of an install, and the render loop has no per-runtime
catch — so the refusal must not raise.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from firekeep_client.adapters.base import Adapter, shim_servers, write_text_if_changed

_CONFIG_NAME = "claude_desktop_config.json"


def _config_dir() -> Path:
    """Claude Desktop's config directory for the current platform.

    Resolved on every call, never cached — tests steer it via APPDATA /
    XDG_CONFIG_HOME / HOME, and the opencode adapter set the precedent that
    an env-following path must follow the env at call time."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "Claude"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "Claude"


def config_path() -> Path:
    return _config_dir() / _CONFIG_NAME


def app_present() -> bool:
    """Whether Claude Desktop appears installed on this machine.

    The config directory is the evidence: the app creates it on first run.
    This is the auto-render gate — `firekeep install` (no --runtime) mounts
    the gateway here only when the app exists, because writing another
    vendor's consumer-app config onto machines that never ran the app would
    scatter orphan files for zero benefit. Explicit
    `--runtime claude-desktop` bypasses the gate (creating the directory),
    for a user installing Firekeep ahead of the app."""
    return _config_dir().is_dir()


def _expected_entry(venv_bin: Path) -> dict:
    command, args = shim_servers(venv_bin, ClaudeDesktopAdapter.name)["firekeep"]
    return {"command": command, "args": list(args)}


def _load(path: Path) -> dict | None:
    """Parse the config, or None when it cannot be safely rewritten.

    Missing file -> {} (we will create it). Unreadable or unparseable or
    non-object -> None, and the caller warns and leaves the file alone."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError):
        return None
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def mcp_entry_is_current(text: str, venv_bin: Path) -> bool:
    """Whether `text` carries the exact gateway entry this adapter would render."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(parsed, dict):
        return False
    servers = parsed.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    return servers.get("firekeep") == _expected_entry(venv_bin)


class ClaudeDesktopAdapter(Adapter):
    name = "claude-desktop"

    def render(self, *, venv_bin: Path) -> None:
        path = config_path()
        data = _load(path)
        if data is None:
            print(
                f"firekeep: WARNING — {path} exists but is not valid JSON; "
                "left untouched. Fix the file, then run "
                "`firekeep install --runtime claude-desktop`.",
                file=sys.stderr,
            )
            return
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            print(
                f"firekeep: WARNING — {path} has a non-object mcpServers; "
                "left untouched.",
                file=sys.stderr,
            )
            return
        servers["firekeep"] = _expected_entry(venv_bin)
        if write_text_if_changed(path, json.dumps(data, indent=2) + "\n"):
            # Only on a real change: this render re-runs on every
            # `firekeep update`, and a restart nag for a byte-identical
            # config would train users to ignore it.
            print("firekeep: Claude Desktop configured — restart the app to load Firekeep.")

    def unrender(self) -> None:
        path = config_path()
        if not path.exists():
            return
        data = _load(path)
        if data is None:
            print(
                f"firekeep: WARNING — could not parse {path}; remove the "
                "'firekeep' entry from mcpServers yourself.",
                file=sys.stderr,
            )
            return
        servers = data.get("mcpServers")
        if not isinstance(servers, dict) or "firekeep" not in servers:
            return
        del servers["firekeep"]
        # The file itself stays — it belongs to Claude Desktop, not to us.
        write_text_if_changed(path, json.dumps(data, indent=2) + "\n")
