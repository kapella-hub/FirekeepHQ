"""OpenCode adapter: XDG opencode.json `mcp` servers + a rendered JS plugin bridge.

Two rendered surfaces (opencode follows XDG — `$XDG_CONFIG_HOME/opencode`, default
`~/.config/opencode`):
  1. `opencode.json` `mcp` key — stdio MCP entries in opencode's native shape
     (`{"type": "local", "command": [cmd, ...args], "enabled": true}`, env via
     `"environment"`). Non-clobbering: only firekeep-owned keys are merged/dropped,
     foreign servers and every other top-level key survive.
  2. `plugins/firekeep-hooks.js` — a Bun plugin bridging opencode's hook surface to the
     SAME five hook cores claude/kiro use, via the dispatcher
     (`{venv}/python -m firekeep_client.hooks <core>`), stdin JSON in, exit codes out.

VALIDATED live on opencode 1.14.22 (2026-07-18, docs/OPENCODE-VALIDATION.md —
mirrors the kiro-validation precedent): the pre-edit gate HARD-BLOCKS (write tool
aborted with the policy reason, file untouched), prompt-core inbox surfaced on
session.idle, stop fired on session.deleted:
  1. Plugin hook surface per https://opencode.ai/docs/plugins/: named hooks
     `tool.execute.before`/`tool.execute.after` receive `(input, output)` with
     `input.tool` (lowercase opencode tool name), `input.sessionID`, `output.args`
     (`filePath`, `command`); a THROWN error in `tool.execute.before` blocks the
     tool call. The generic `event` hook receives `{event: {type, properties}}`.
  2. Event mapping vs claude: session_start fires from the FIRST hook the bridge
     sees (empirical 1.14.22: in `opencode run` mode session.created is published
     BEFORE plugins subscribe, so a created-only wiring never fires headless;
     briefing goes to console — opencode has no systemMessage channel, so the
     briefing/inbox text is LOGGED, not injected into model context),
     `session.idle` (turn end) -> prompt (heartbeat + inbox poll cadence),
     `session.deleted` -> stop. Deviation from claude, documented: claude fires
     Stop at every turn end; here stop runs only at real session deletion, so a
     hard quit relies on the briefing's crash detection (active session, no
     presence) exactly like a claude crash does.
  3. Tool-name translation: opencode `edit`/`write` -> Claude-shaped `Edit`/`Write`,
     `bash` -> `Bash`; `output.args.filePath` -> `tool_input.file_path`. The hook
     cores only understand the Claude names (pre_tool._EDIT_TOOLS).
  4. Blocking: pre_tool renders with `--block-exit 2` (dispatcher remaps any nonzero
     rc — gateway block rc=1, lease conflict rc=2 — to 2); the bridge throws ONLY on
     exit 2, so spawn failures/timeouts degrade to allow (availability over
     enforcement, same as the cores' own unreachable-server contract).

The plugin file is firekeep-owned at a kit-distinct name. Per the kiro clobber lesson
(2026-07-13): render still refuses to overwrite an existing file that lacks our
marker, and unrender deletes only a marker-bearing file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from firekeep_client.adapters.base import (
    FIREKEEP_INSTRUCTIONS,
    FIREKEEP_MCP_KEYS,
    Adapter,
    console_script_path,
    drop_owned,
    merge_owned,
    read_json,
    read_pin,
    shim_servers,
    strip_marked_block,
    upsert_marked_block,
    write_json,
)

PLUGIN_MARKER = "firekeep-owned: opencode hook bridge"

# @-token substitution (not str.format) so the JS braces need no escaping.
_JS_TEMPLATE = """\
/* @MARKER@
   Rendered by `firekeep install --runtime opencode`. Do not hand-edit — a re-render
   overwrites this file; `firekeep` unrender removes it (marker-guarded). */
import { spawnSync } from "node:child_process"

const PYTHON = "@PYTHON@"
const PROFILE_ARGS = @PROFILE_ARGS@
// opencode tool names -> the Claude-shaped names the firekeep hook cores expect.
const TOOL_NAMES = { edit: "Edit", write: "Write", bash: "Bash" }

function runCore(core, payload, timeoutMs, extra = []) {
  try {
    return spawnSync(PYTHON, ["-m", "firekeep_client.hooks", core, ...extra, ...PROFILE_ARGS], {
      input: JSON.stringify(payload || {}),
      timeout: timeoutMs,
      encoding: "utf8",
    })
  } catch {
    return null // a broken bridge must never break the session
  }
}

function emit(res) {
  if (!res || !res.stdout) return
  try {
    const msg = JSON.parse(res.stdout).systemMessage
    if (msg) console.log("[firekeep] " + msg)
  } catch {}
}

export const FirekeepHooks = async () => {
  // Validated live (opencode 1.14.22, `opencode run` mode): session.created is
  // PUBLISHED on the bus before plugins finish subscribing, so a created-only
  // wiring never fires session_start in headless runs. Fallback: the first hook
  // of any kind that reaches us runs session_start exactly once.
  let started = false
  const ensureStarted = (sid) => {
    if (started) return
    started = true
    emit(runCore("session_start", { session_id: sid || "" }, 15000))
  }
  return {
  event: async ({ event }) => {
    if (!event) return
    const sid = (event.properties && event.properties.info && event.properties.info.id) || ""
    if (event.type === "session.created") {
      ensureStarted(sid)
    } else if (event.type === "session.idle") {
      ensureStarted(sid)
      emit(runCore("prompt", {}, 8000))
    } else if (event.type === "session.deleted") {
      runCore("stop", {}, 5000)
    }
  },
  "tool.execute.before": async (input, output) => {
    const tool = TOOL_NAMES[input && input.tool]
    if (!tool || tool === "Bash") return // pre-gate guards edits only (claude parity)
    ensureStarted(input && input.sessionID)
    const args = (output && output.args) || {}
    const res = runCore("pre_tool", {
      session_id: (input && input.sessionID) || "",
      tool_name: tool,
      tool_input: { file_path: args.filePath || args.file_path || "" },
    }, 5000, ["--block-exit", "2"])
    if (res && res.status === 2) {
      throw new Error("[firekeep] " + String(res.stderr || "pre-edit gate blocked this change").trim())
    }
  },
  "tool.execute.after": async (input, output) => {
    const tool = TOOL_NAMES[input && input.tool]
    if (!tool) return
    const args = (output && output.args) || {}
    runCore("post_tool", {
      session_id: (input && input.sessionID) || "",
      tool_name: tool,
      tool_input: tool === "Bash"
        ? { command: args.command || "" }
        : { file_path: args.filePath || args.file_path || "" },
      tool_response: {},
    }, 10000)
  },
  }
}
"""


def _config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "opencode"


class OpencodeAdapter(Adapter):
    name = "opencode"

    def _config_path(self) -> Path:
        return _config_dir() / "opencode.json"

    def _plugin_path(self) -> Path:
        return _config_dir() / "plugins" / "firekeep-hooks.js"

    def _instructions_path(self) -> Path:
        # opencode's global rules file — read automatically at session start. A
        # user-owned file, so only the marker-delimited block is ever touched
        # (claude-CLAUDE.md precedent, not kiro's whole-file steering doc).
        return _config_dir() / "AGENTS.md"

    def render(self, *, venv_bin: Path) -> None:
        pin = read_pin(self.name)

        config = read_json(self._config_path())
        servers = config.setdefault("mcp", {})
        entries = {
            name: {"type": "local", "command": [cmd, *args], "enabled": True,
                   **({"environment": {"FIREKEEP_PROFILE": pin}} if pin else {})}
            for name, (cmd, args) in shim_servers(venv_bin).items()
        }
        merge_owned(servers, entries)
        write_json(self._config_path(), config)

        # Plugin bridge and instruction block are INDEPENDENT firekeep-owned
        # artifacts — one failing must never skip the other or fail the install
        # (the claude adapter's decision-board regression, applied here from day
        # one).
        try:
            self._render_plugin(venv_bin, pin)
        except Exception:  # noqa: BLE001 — best-effort; must not skip the block below
            pass
        try:
            self._render_instructions()
        except Exception:  # noqa: BLE001 — best-effort; must not fail the install
            pass

    def _render_instructions(self) -> None:
        path = self._instructions_path()
        try:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(upsert_marked_block(existing, FIREKEEP_INSTRUCTIONS),
                            encoding="utf-8")
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

    def _render_plugin(self, venv_bin: Path, pin: str | None) -> None:
        # spawnSync takes an argv ARRAY, so unlike hook_command's shell strings the
        # python path needs no whitespace quoting — only forward slashes (JS string
        # literal safety; also valid for Windows CreateProcess).
        python = console_script_path(venv_bin / "python").replace("\\", "/")
        body = (_JS_TEMPLATE
                .replace("@MARKER@", PLUGIN_MARKER)
                .replace("@PYTHON@", python)
                .replace("@PROFILE_ARGS@", json.dumps(["--profile", pin] if pin else [])))
        path = self._plugin_path()
        try:
            if path.exists() and PLUGIN_MARKER not in path.read_text(encoding="utf-8"):
                return  # foreign file at our path — never clobber
        except OSError:
            return  # unreadable existing file: leave it alone rather than guess
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def unrender(self) -> None:
        config = read_json(self._config_path())
        drop_owned(config.get("mcp", {}), FIREKEEP_MCP_KEYS)
        write_json(self._config_path(), config)

        path = self._plugin_path()
        try:
            if path.exists() and PLUGIN_MARKER in path.read_text(encoding="utf-8"):
                path.unlink()
        except OSError:
            pass

        self._unrender_instructions()
