"""kiro-cli adapter: ~/.kiro/agents/firekeep.json — stdio MCP servers + inline agent hooks.

Even though firekeep.json is a dedicated agent file, render is non-clobbering: a teammate
may hand-add an mcpServer or hook entry, and those foreign entries must survive.

VALIDATED against kiro-cli 2.12.1 (see docs/KIRO-VALIDATION.md for the empirical runs):
  1. Agent file is `~/.kiro/agents/firekeep.json`; top-level keys `mcpServers` (stdio entries
     `{"command": str, "args": [str, ...]}`, no `"type"` discriminator the way Claude's
     `~/.claude.json` needs one) and `hooks` (event name -> FLAT list of
     `{"command": str, "matcher"?: str, "timeout_ms"?: int}`). `kiro-cli agent validate
     --path <file>` accepts this shape.
  2. The 5 lifecycle events are `agentSpawn`, `userPromptSubmit`, `preToolUse`,
     `postToolUse`, `stop`, mapping 1:1 to Claude's SessionStart/UserPromptSubmit/
     PreToolUse/PostToolUse/Stop. VALIDATED 2026-07-28 (KIRO-VALIDATION.md rows 7-8),
     including the cadence the mapping had only ever asserted: `stop` fires PER TURN,
     exactly like Claude's Stop (3 prompts in one session -> agentSpawn 1, stop 3).
     Two consequences: kiro carried the same turn-1 presence bug and is fixed by the
     same change; and kiro is deliberately NOT wired to `session_end`, because it has
     no session-end event AND its hook payload carries no session id (keys are only
     cwd / hook_event_name / prompt|assistant_response), so a per-session marker
     cannot be keyed either. See hooks/session_end.py.
  3. `matcher` is an EXACT kiro tool-name/alias match, NOT a regex: `.*` and Claude's
     `Edit|Write` match NOTHING (the old values meant the hook never fired at all); `"*"`
     matches every tool. kiro's file create/edit tool is `fs_write` (alias `write`), so the
     pre-edit gate and the post reconcile both match `fs_write`. Hooks receive a JSON event
     on stdin carrying `tool_name`/`tool_input` (confirmed: `{"tool_name":"fs_write",...}`).
  4. Blocking: kiro's documented contract is preToolUse exit 0 = allow, 2 = block, other =
     warn+allow — same shape as Claude. pre_tool.run() returns 1 for a gateway block/rethink
     and 2 for a lease conflict, so — like the claude adapter — pre_tool gets the
     `--block-exit 2` remap (rc=1 block -> exit 2), which is what kiro's contract expects.
     EMPIRICAL CAVEAT (kiro-cli 2.12.1): the hook FIRES and kiro waits for it, but exit 2
     does NOT actually veto the tool — kiro reports "0 of 1 hooks finished" and proceeds.
     So on 2.12.1 the pre-edit hook is ADVISORY (the agent-gateway before-call still runs
     and records), not a hard block; `contract.matrix` rates it accordingly, and the remap
     is rendered so blocking works as soon as kiro enforces its own contract.
  5. Non-clobbering merge granularity: per-entry, keyed on the firekeep hook marker in the
     command, so a re-render replaces the firekeep entry in place (foreign entries survive).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from firekeep_client.adapters.base import (
    FIREKEEP_INSTRUCTIONS,
    FIREKEEP_MCP_KEYS,
    LEGACY_MCP_KEYS,
    Adapter,
    drop_owned,
    hook_command,
    merge_owned,
    prune_flat_hooks,
    read_json,
    shim_servers,
    upsert_flat_hook,
    write_json,
    write_text_if_changed,
)

# Whole-file firekeep-owned steering doc (kiro's instruction surface). The marker
# guards unrender the same way the claude /personal command file is guarded:
# we only ever delete OUR file, never a hand-written steering doc.
STEERING_MARKER = "firekeep-owned: cognitive-stack instructions"

# (kiro event, hook core, matcher | None). matcher is an EXACT kiro tool name, not a regex:
# `fs_write` is kiro's file create/edit tool, so the pre-edit gate and post reconcile match
# it. (Claude's `Edit|Write` names do not exist in kiro and matched nothing — the hook never
# fired. See the module docstring.)
KIRO_HOOKS = (
    ("agentSpawn", "session_start", None),
    ("userPromptSubmit", "prompt", None),
    ("preToolUse", "pre_tool", "fs_write"),
    ("postToolUse", "post_tool", "fs_write"),
    ("stop", "stop", None),
)


def _is_legacy_firekeep_key(key: str) -> bool:
    # Exact kit names plus parked variants like `firekeep-cortex_DISABLED` — the pre-kit
    # manual setup's entries in ~/.kiro/settings/mcp.json. Deliberately NOT a bare
    # `firekeep-` prefix match: a user's own `firekeep-somethingelse` server is foreign.
    #
    # LEGACY_MCP_KEYS covers the PREDECESSOR kit's six entries for the same reason: an
    # upgraded machine otherwise keeps them alongside ours, and they point at a config
    # path that no longer exists.
    owned = FIREKEEP_MCP_KEYS + LEGACY_MCP_KEYS
    return key in owned or key.startswith(tuple(f"{k}_" for k in owned))


def _migrate_legacy(home: Path) -> None:
    """One-way, best-effort pre-kit cleanup (spec §2b; mirrors claude's
    LEGACY_HOOK_MARKERS precedent). Never raises: a missing file is a no-op, a
    malformed OR structurally-wrong mcp.json (non-object top level, non-dict
    mcpServers) is left byte-identical — a legacy artifact must never be able to
    fail an install, and a skipped mcp.json edit must never abort the archival
    step below."""
    mcp_json = home / ".kiro" / "settings" / "mcp.json"
    try:
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        # Structure-validate before editing: json.loads happily returns a list/str/int
        # top level (data.get -> AttributeError) or a dict whose mcpServers is a list
        # (del servers[str] -> TypeError). Wrong shape -> skip the edit, file untouched.
        if isinstance(data, dict):
            servers = data.get("mcpServers", {})
            if isinstance(servers, dict):
                stale = [k for k in servers if _is_legacy_firekeep_key(k)]
                if stale:
                    for k in stale:
                        del servers[k]
                    write_text_if_changed(mcp_json, json.dumps(data, indent=2) + "\n")
    except Exception:  # noqa: BLE001 — total backstop: a legacy artifact must never fail an install
        # Missing, unparsable, shape-hostile, or anything else (e.g. RecursionError from
        # pathologically deep JSON — a RuntimeError subclass no enumerated tuple covers).
        # The isinstance guards above remain the primary, legible defense; this is the
        # any-input guarantee. Leave the file alone and fall through to the archival step.
        pass
    cli_json = home / ".kiro" / "settings" / "cli.json"
    try:
        settings = json.loads(cli_json.read_text(encoding="utf-8"))
        if isinstance(settings, dict) and settings.get("chat.defaultAgent") == "nexus":
            # The predecessor setup made its `nexus` agent the chat default. Firekeep writes
            # a distinct named agent, so leaving this value behind makes plain `kiro-cli chat`
            # keep launching the old adapter and makes a successful Firekeep install look
            # inert. This is a predecessor-owned value, so migrating it preserves intent.
            settings["chat.defaultAgent"] = "firekeep"
            write_text_if_changed(cli_json, json.dumps(settings, indent=2) + "\n")
    except Exception:  # noqa: BLE001 — total backstop, same contract as the mcp.json block
        pass
    # NO LEGACY-ARTIFACT ARCHIVING HERE. This loop used to move
    # ~/.kiro/agents/firekeep.json and ~/.kiro/firekeep.env aside to .bak.
    #
    # In the predecessor product those were two DISTINCT paths under
    # ~/.kiro/agents/: the kit wrote its own agent file under a SHORT product
    # name, and the pre-kit manual-setup artifact it cleaned up used the LONGER
    # full product name. The rename mapped both of those onto `firekeep`,
    # collapsing them into one path — so render() archived ITS OWN OUTPUT. Every
    # `firekeep install --runtime kiro` moved the user's live config, including
    # any MCP servers and hooks they had added themselves, to .bak and wrote a
    # fresh file. That is precisely the clobbering this module's docstring
    # promises not to do.
    #
    # Removed rather than re-pointed at some other name: the artifacts it
    # cleaned up belonged to a product Firekeep customers never ran, so there is
    # nothing here to migrate. Two tests asserted opposite things about this
    # path after the rename, which is what surfaced it.


class KiroAdapter(Adapter):
    name = "kiro"

    def _path(self) -> Path:
        return Path.home() / ".kiro" / "agents" / "firekeep.json"

    def _steering_path(self) -> Path:
        # Deliberately NOT "firekeep.md": machines set up pre-kit have a
        # HAND-WRITTEN ~/.kiro/steering/firekeep.md (observed in the field,
        # 2026-07-14) and a plain overwrite would eat it. A kit-distinct name
        # means render never has to touch a file we don't own.
        return Path.home() / ".kiro" / "steering" / "firekeep-instructions.md"

    def _set_default_agent(self) -> None:
        """Point plain `kiro-cli chat` at the firekeep agent when NO default is set.

        The kit wires everything into the NAMED agent (~/.kiro/agents/firekeep.json)
        — a fresh machine launching plain `kiro-cli chat` gets kiro's default
        agent, which has none of it, and /mcp shows nothing (field report,
        2026-07-14: teammate install looked dead). A user's own configured
        default is never overridden. Best-effort like every migration step:
        no kiro-cli on PATH, or any failure, must not fail the install.
        """
        try:
            if shutil.which("kiro-cli") is None:
                return
            probe = subprocess.run(
                ["kiro-cli", "settings", "all"],
                capture_output=True, text=True, timeout=15,
            )
            if probe.returncode == 0 and "chat.defaultAgent" in probe.stdout:
                return  # a default exists (ours or the user's own) — leave it
            subprocess.run(
                ["kiro-cli", "agent", "set-default", "firekeep"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:  # noqa: BLE001
            pass

    def _render_steering(self) -> None:
        """Write the decision-board trigger into kiro's steering dir (its
        instruction surface — kiro loads ~/.kiro/steering/*.md globally).
        Entirely firekeep-owned file under a kit-distinct name — plain overwrite;
        best-effort like _migrate_legacy: never fails the install."""
        path = self._steering_path()
        body = (
            "---\n"
            "name: firekeep-instructions\n"
            "description: Firekeep decision-board + knowledge-ingest usage (rendered by firekeep install).\n"
            "---\n"
            f"<!-- {STEERING_MARKER} -->\n"
            f"{FIREKEEP_INSTRUCTIONS}"
        )
        try:
            write_text_if_changed(path, body)
        except OSError:
            pass

    def _unrender_steering(self) -> None:
        path = self._steering_path()
        try:
            if path.exists() and STEERING_MARKER in path.read_text(encoding="utf-8"):
                path.unlink()
        except OSError:
            pass

    def render(self, *, venv_bin: Path) -> None:
        _migrate_legacy(Path.home())
        path = self._path()
        data = read_json(path)
        data.setdefault("name", "firekeep")

        servers = data.setdefault("mcpServers", {})
        entries = {
            name: {"command": cmd, "args": args}
            for name, (cmd, args) in shim_servers(venv_bin).items()
        }
        merge_owned(servers, entries)

        # kiro only exposes MCP tools that the agent's `tools` list GRANTS —
        # without it the servers connect (visible in /mcp) but the model can
        # never call a single tool (field bug, 2026-07-14: memory_recall
        # "not in my toolset" while firekeep-cortex showed running). The pre-kit
        # hand-made agent carried tools=["*"] + @firekeep-* in allowedTools; the
        # kit render dropped both. Union, never clobber: user-added entries
        # survive, and an existing "*" already grants everything.
        tools = data.setdefault("tools", ["*"])
        if isinstance(tools, list) and "*" not in tools:
            for key in FIREKEEP_MCP_KEYS:
                if f"@{key}" not in tools:
                    tools.append(f"@{key}")
        # allowedTools = pre-trusted (no per-call permission prompt). The hooks
        # call memory/relay tools on every prompt — a permission dialog per
        # recall would make the kit unusable, so kit servers are trusted.
        allowed = data.setdefault("allowedTools", [])
        if isinstance(allowed, list):
            for key in FIREKEEP_MCP_KEYS:
                if f"@{key}" not in allowed:
                    allowed.append(f"@{key}")

        hooks = data.setdefault("hooks", {})
        for event, core, matcher in KIRO_HOOKS:
            # kiro's contract blocks a tool call only on hook exit 2 (like Claude);
            # pre_tool.run() returns 1 for a gateway block/rethink and 2 for a lease conflict,
            # so remap block->2 (without it a gateway block, rc=1, is warn-and-ALLOW). On
            # kiro-cli 2.12.1 the block is not actually enforced (see docstring) — the remap
            # is still rendered so it works the moment kiro honors its own contract.
            extra_args = "--block-exit 2" if core == "pre_tool" else ""
            entry = {"command": hook_command(venv_bin, core, extra_args=extra_args)}
            if matcher:
                entry["matcher"] = matcher
            upsert_flat_hook(hooks.setdefault(event, []), entry)

        write_json(path, data)
        self._render_steering()  # decision-board trigger (firekeep-owned steering doc)
        self._set_default_agent()  # plain `kiro-cli chat` must find the firekeep agent

    def unrender(self) -> None:
        path = self._path()
        data = read_json(path)
        drop_owned(data.get("mcpServers", {}), FIREKEEP_MCP_KEYS)
        prune_flat_hooks(data.get("hooks", {}))
        for field in ("tools", "allowedTools"):
            entries = data.get(field)
            if isinstance(entries, list):
                data[field] = [t for t in entries
                               if t not in {f"@{k}" for k in FIREKEEP_MCP_KEYS}]
        write_json(path, data)
        self._unrender_steering()
