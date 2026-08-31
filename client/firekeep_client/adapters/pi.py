"""Pi adapter: `~/.pi/agent/settings.json` package entry + a python sidecar.

Pi (https://pi.dev) is a minimal agent harness. It deliberately ships NO MCP —
its docs list MCP alongside sub-agents and permission popups as "extension or
package; not built-in" — so unlike every other adapter here there is no `mcp`
key to merge servers into. The integration is the `firekeep-pi` EXTENSION, which
bridges Pi's event surface to the same seven hook cores claude/kiro/opencode use
(`{venv}/python -m firekeep_client.hooks <core> --runtime pi`).

Three rendered surfaces (Pi follows `~/.pi/agent` for global config):
  1. `settings.json` `packages` — the `firekeep-pi` npm package. Pi resolves and
     installs packages named here into `~/.pi/agent/npm/`. Only firekeep-owned
     entries are added or removed; foreign packages and every other key survive.
  2. `~/.firekeep/pi-extension.json` — names the kit's venv interpreter, because
     the extension ships from npm and cannot know where this machine's
     `firekeep_client` lives. The extension falls back to `python3`/`python` on
     PATH when the sidecar is absent, which is what keeps the npm package usable
     for a Pi user who never ran `firekeep install`.
  3. `AGENTS.md` — Pi loads `~/.pi/agent/AGENTS.md` at startup (its quickstart
     documents exactly this path), so the decision-board instruction block lands
     the same way it does for opencode. Marker-delimited: only our block is
     touched, per the claude-CLAUDE.md precedent.

CAPABILITY BOUNDARY, stated plainly because the matrix must not overstate it.
This adapter delivers the HOOK surface — briefings, the pre-edit gate, presence,
lifecycle. It does NOT deliver the MCP TOOL surface: with no MCP client in Pi,
an agent cannot call `memory_recall` or `ctx_update` unless the user separately
installs one of the community MCP extensions. Firekeep does not ship or control
those, so nothing here configures them.

VALIDATED live on Pi 0.84.4 (2026-08-29, docs/PI-VALIDATION.md — the kiro and
opencode validation precedents). 16/16 assertions, including the two claims that
put Pi above every other non-Claude runtime:
  1. The briefing REACHES THE MODEL. Pi's `before_agent_start` may return
     `{ systemPrompt }`; the harness captures `context.systemPrompt` inside the
     provider and finds the briefing in it. opencode can only log the same text.
  2. The pre-edit gate HARD-BLOCKS. Pi's `tool_call` takes
     `{ block: true, reason, terminate }`; the harness shows the target file is
     never written when the gate exits 2, and IS written when it exits 0.
Unproven and therefore unclaimed: `precompact` (wired, never exercised), and any
behaviour against a real Keep — every assertion used a stub dispatcher.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from firekeep_client.adapters.base import (
    FIREKEEP_INSTRUCTIONS,
    Adapter,
    console_script_path,
    read_json,
    strip_marked_block,
    upsert_marked_block,
    write_json,
    write_text_if_changed,
)

#: The npm package that carries the bridge. Sources live at `client/pi/`.
PACKAGE = "firekeep-pi"

#: Sidecar naming the interpreter that owns `firekeep_client`. Read by the
#: extension; adapter-owned, so unrender deletes it outright.
SIDECAR_NAME = "pi-extension.json"


def _config_dir() -> Path:
    """Pi's global config directory.

    `PI_CODING_AGENT_CONFIG_DIR` is honoured first so a test (or a user with a
    relocated config) is not forced through the real home directory — the same
    escape hatch `resolver._config_path` gives `~/.firekeep/config`.
    """
    override = os.environ.get("PI_CODING_AGENT_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".pi" / "agent"


def _sidecar_path() -> Path:
    # Beside ~/.firekeep/config, and honouring the same FIREKEEP_CONFIG override,
    # so an isolated test never writes to the real home directory.
    from firekeep_client.resolver import _config_path

    return _config_path().parent / SIDECAR_NAME


def app_present() -> bool:
    """Whether Pi appears installed on this machine.

    The config directory is the evidence: Pi creates `~/.pi/agent` on first run.
    This is the auto-render gate for `firekeep install` (no --runtime), exactly
    as claude_desktop.app_present() is — writing another tool's settings.json
    onto machines that never ran it would scatter orphan files for zero benefit,
    and the `packages` entry would make Pi try to install an extension the user
    never asked for. Explicit `--runtime pi` bypasses the gate, for a user
    installing Firekeep ahead of Pi.
    """
    return _config_dir().is_dir()


class PiAdapter(Adapter):
    name = "pi"

    def _config_path(self) -> Path:
        return _config_dir() / "settings.json"

    def _instructions_path(self) -> Path:
        # Pi's global context file, loaded at startup (quickstart.md: "`~/.pi/
        # agent/AGENTS.md` for global instructions"). User-owned, so only the
        # marker-delimited block is ever written.
        return _config_dir() / "AGENTS.md"

    def render(self, *, venv_bin: Path) -> None:
        config = read_json(self._config_path())
        packages = config.setdefault("packages", [])
        if isinstance(packages, list):
            # Idempotent and non-clobbering: a re-render must not append a second
            # copy, and a user's own packages are never reordered or dropped.
            if PACKAGE not in packages:
                packages.append(PACKAGE)
            write_json(self._config_path(), config)

        # The sidecar and the instruction block are INDEPENDENT firekeep-owned
        # artifacts. One failing must never skip the other or fail the install —
        # the claude adapter's decision-board regression, applied here from day
        # one exactly as opencode does.
        try:
            self._render_sidecar(venv_bin)
        except Exception:  # noqa: BLE001 — best-effort; must not skip the block below
            pass
        try:
            self._render_instructions()
        except Exception:  # noqa: BLE001 — best-effort; must not fail the install
            pass

    def _render_sidecar(self, venv_bin: Path) -> None:
        # Forward slashes: the value is read by JS and passed to spawnSync as an
        # argv[0], where forward slashes are valid for Windows CreateProcess too.
        python = console_script_path(venv_bin / "python").replace("\\", "/")
        path = _sidecar_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_if_changed(
            path,
            json.dumps({"python": python, "runtime": self.name}, indent=2) + "\n",
        )

    def _render_instructions(self) -> None:
        path = self._instructions_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            write_text_if_changed(path, upsert_marked_block(existing, FIREKEEP_INSTRUCTIONS))
        except OSError:
            pass

    def _unrender_instructions(self) -> None:
        path = self._instructions_path()
        try:
            if not path.exists():
                return
            existing = path.read_text(encoding="utf-8")
            stripped = strip_marked_block(existing)
            if stripped != existing:
                path.write_text(stripped, encoding="utf-8")
        except OSError:
            pass

    def unrender(self) -> None:
        config = read_json(self._config_path())
        packages = config.get("packages")
        if isinstance(packages, list) and PACKAGE in packages:
            config["packages"] = [p for p in packages if p != PACKAGE]
            write_json(self._config_path(), config)

        try:
            _sidecar_path().unlink(missing_ok=True)
        except OSError:
            pass
        self._unrender_instructions()
