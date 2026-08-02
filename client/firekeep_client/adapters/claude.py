"""Claude Code adapter: ~/.claude.json mcpServers + ~/.claude/settings.json hooks + env.

Non-clobbering: foreign MCP servers, foreign hook groups, and foreign env keys survive.
Connection/auth/identity live in ~/.firekeep/config (read live by the shim) — so the only
firekeep-owned env key here is the Claude agent-teams toggle; URLs are no longer written.
"""
from __future__ import annotations

from pathlib import Path

from firekeep_client.adapters.base import (
    FIREKEEP_INSTRUCTIONS,
    LEGACY_ENV_KEYS,
    LEGACY_INSTRUCTION_MARKERS,
    LEGACY_MCP_KEYS,
    FIREKEEP_ENV_KEYS,
    FIREKEEP_MCP_KEYS,
    Adapter,
    console_script_path,
    drop_owned,
    find_legacy_block_bounds,
    hook_command,
    merge_owned,
    prune_hook_groups,
    read_json,
    read_pin,
    shim_servers,
    strip_marked_block,
    strip_span,
    upsert_hook_group,
    upsert_marked_block,
    write_json,
    write_text_if_changed,
)

# Stable marker embedded in the rendered /personal command file so unrender only ever
# deletes OUR file, never a hand-written ~/.claude/commands/personal.md a user owns.
COMMAND_MARKER = "firekeep-owned: personal-mode toggle"

# (Claude event, hook core, matcher | None, timeout) — 7 lifecycle hooks -> hook-core entry points.
#
# SessionEnd is what makes the presence lifecycle correct: Stop fires at EVERY
# assistant turn end, so the deregister that used to ride it deleted presence
# after turn 1 and heartbeat (update-only) could never restore it. SessionEnd is
# the only Claude event carrying the turn-vs-session distinction. See
# hooks/session_end.py.
CLAUDE_HOOKS = (
    ("SessionStart", "session_start", None, 15),
    ("Stop", "stop", None, 5),
    ("SessionEnd", "session_end", None, 5),
    ("UserPromptSubmit", "prompt", None, 8),
    ("PreCompact", "precompact", None, 15),
    # Bash is included since 2026-08-02: pre_tool has always mapped it to "run_command",
    # but this matcher excluded it, so the BLOCKING gate never saw a shell command while
    # PostToolUse below watched them execute. `git checkout -- cortex/app/` destroyed
    # nine files of uncommitted work through that gap. Bash takes the snapshot-then-allow
    # path in pre_tool and never reaches the network.
    ("PreToolUse", "pre_tool", "^(Edit|Write|Bash)$", 5),
    ("PostToolUse", "post_tool", "^(Edit|Write|MultiEdit|Bash)$", 10),
)


class ClaudeAdapter(Adapter):
    name = "claude"

    def _paths(self) -> tuple[Path, Path]:
        home = Path.home()
        return home / ".claude.json", home / ".claude" / "settings.json"

    def _command_path(self) -> Path:
        return Path.home() / ".claude" / "commands" / "personal.md"

    def _firekeep_cmd(self, venv_bin: Path) -> str:
        """Absolute path to the venv `firekeep` console script, forward-slashed and
        quoted-if-spaced — same treatment hook_command applies, because Claude runs a
        command's `!`-exec through bash (git-bash on Windows), where a native backslash
        path collapses under escaping."""
        firekeep = console_script_path(venv_bin / "firekeep").replace("\\", "/")
        if any(ch.isspace() for ch in firekeep):
            firekeep = f'"{firekeep}"'
        return firekeep

    def _render_command(self, venv_bin: Path) -> None:
        """Write the `/personal` slash command: it runs `firekeep personal toggle` and
        prints the resulting state. Entirely firekeep-owned (own dedicated file), so a
        plain overwrite is safe; the marker lets unrender remove only our copy."""
        firekeep = self._firekeep_cmd(venv_bin)
        body = (
            "---\n"
            "description: Toggle Firekeep personal (bypass) mode for this session.\n"
            f"allowed-tools: Bash({firekeep} personal:*)\n"
            "---\n"
            f"<!-- {COMMAND_MARKER} -->\n"
            "Firekeep personal mode toggled:\n\n"
            f"!`{firekeep} personal toggle`\n\n"
            "While personal mode is ON, Firekeep is bypassed for this session — no "
            "briefing, memory, presence, or logging — and you should not call firekeep_* "
            "tools. It auto-clears when the session ends.\n"
        )
        write_text_if_changed(self._command_path(), body)

    def _unrender_command(self) -> None:
        path = self._command_path()
        try:
            if path.exists() and COMMAND_MARKER in path.read_text(encoding="utf-8"):
                path.unlink()
        except OSError:
            pass

    def _instructions_path(self) -> Path:
        return Path.home() / ".claude" / "CLAUDE.md"

    def _migrate_legacy_instructions(self) -> None:
        """Archive the PREDECESSOR's own instruction block out of the user's global
        ~/.claude/CLAUDE.md -- never delete: this file is user-owned prose that we
        only ever partially own (one marked region), so removing content out of it
        without a backup is a straight data-loss bug, not a cleanup.

        Only the `nexus:instructions` pair (LEGACY_INSTRUCTION_MARKERS) is touched.
        The sibling `Agent Guidelines` block -- a distinct marker pair the
        predecessor also wrote to this file -- is 0.03-similar to
        FIREKEEP_INSTRUCTIONS -- not a duplicate, it is content the user still has
        -- and is deliberately left alone; see the tuple's comment in base.py.

        Idempotent by construction: once the block is stripped from the live file,
        a later render finds no marker here and this is a no-op (no re-archive, no
        rewrite) -- what keeps `firekeep update`'s mid-session re-render byte-stable.
        """
        path = self._instructions_path()
        try:
            if not path.exists():
                return
            original = path.read_text(encoding="utf-8")
            stripped = original
            found = False
            for begin_prefix, end_marker in LEGACY_INSTRUCTION_MARKERS:
                bounds = find_legacy_block_bounds(stripped, begin_prefix, end_marker)
                if bounds is None:
                    continue
                found = True
                stripped = strip_span(stripped, *bounds)
            if not found:
                return
            # Archive the file AS IT WAS (with the legacy block) before rewriting it --
            # this is what preserves the block's content, since we are removing it from
            # the live file, not just formatting it. Never overwrite a .bak that is
            # already there -- it may be the user's own file, or another tool's, and
            # this module's whole premise is archive-never-delete; the same collision
            # class made kiro.py drop its own .bak path entirely (see the comment on
            # the removed loop above KiroAdapter). Migration still proceeds -- the
            # block is stripped from the live file either way -- we just skip writing
            # a second archive over an existing one.
            bak_path = path.with_name(path.name + ".bak")
            if not bak_path.exists():
                write_text_if_changed(bak_path, original)
            path.write_text(stripped, encoding="utf-8")
        except OSError:
            pass

    def _render_instructions(self) -> None:
        """Upsert the firekeep-owned instruction block (decision-board trigger) into the
        user's global ~/.claude/CLAUDE.md. Only the marker-delimited block is ever
        touched — user content on either side survives byte-for-byte. Best-effort:
        an unreadable/unwritable file must not fail the install."""
        path = self._instructions_path()
        try:
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

    def render(self, *, venv_bin: Path) -> None:
        claude_json, settings_json = self._paths()

        pin = read_pin(self.name)
        pin_env = {"env": {"FIREKEEP_PROFILE": pin}} if pin else {}

        config = read_json(claude_json)
        servers = config.setdefault("mcpServers", {})
        # Migration: drop the PREDECESSOR kit's server entries. Firekeep registers its
        # own six; leaving these makes twelve, half of them pointing at a config path
        # that no longer exists and failing to connect on every session start.
        drop_owned(servers, LEGACY_MCP_KEYS)
        entries = {
            name: {"type": "stdio", "command": cmd, "args": args, **pin_env}
            for name, (cmd, args) in shim_servers(venv_bin).items()
        }
        merge_owned(servers, entries)
        write_json(claude_json, config)

        settings = read_json(settings_json)
        env = settings.setdefault("env", {})
        env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"
        # Migration: the retired local-setup.* wrote service URLs here. Nothing reads them
        # any more (the resolver owns URL/auth/TLS), so leave no stale copy behind to
        # contradict ~/.firekeep/config. FIREKEEP_AGENT_ID is NOT dropped — it is a live,
        # documented per-process identity override.
        drop_owned(env, LEGACY_ENV_KEYS)
        hooks = settings.setdefault("hooks", {})
        for event, core, matcher, timeout in CLAUDE_HOOKS:
            # Claude's PreToolUse process gate blocks ONLY on exit code 2; pre_tool.run()
            # returns 1 for an agent-gateway block/rethink and 2 for a lease conflict. Without
            # this remap, a gateway 'block' (rc=1) would silently fall through as non-blocking.
            extra_args = "--block-exit 2" if core == "pre_tool" else ""
            if pin:
                extra_args = f"{extra_args} --profile {pin}".strip()
            group = {"hooks": [{"type": "command",
                                "command": hook_command(venv_bin, core, extra_args=extra_args),
                                "timeout": timeout}]}
            if matcher:
                group["matcher"] = matcher
            upsert_hook_group(hooks, event, group)
        write_json(settings_json, settings)

        # /personal command and the instruction block are INDEPENDENT firekeep-owned
        # artifacts — a failure in one must never skip the other or fail the
        # install. (Regression: an unguarded _render_command once threw and
        # silently skipped _render_instructions, so the decision-board trigger
        # never reached ~/.claude/CLAUDE.md and never fired.)
        try:
            self._render_command(venv_bin)  # /personal slash command
        except Exception:  # noqa: BLE001 — best-effort; must not skip the blocks below
            pass
        try:
            self._migrate_legacy_instructions()  # archive the predecessor's own block
        except Exception:  # noqa: BLE001 — best-effort; must not skip the block below
            pass
        try:
            self._render_instructions()  # decision-board + knowledge-ingest block
        except Exception:  # noqa: BLE001 — best-effort; must not fail the install
            pass

    def unrender(self) -> None:
        claude_json, settings_json = self._paths()

        config = read_json(claude_json)
        drop_owned(config.get("mcpServers", {}), FIREKEEP_MCP_KEYS + LEGACY_MCP_KEYS)
        write_json(claude_json, config)

        settings = read_json(settings_json)
        drop_owned(settings.get("env", {}), FIREKEEP_ENV_KEYS + LEGACY_ENV_KEYS)
        prune_hook_groups(settings.get("hooks", {}))  # legacy bash groups included
        write_json(settings_json, settings)

        self._unrender_command()  # remove our /personal command (marker-guarded)
        self._unrender_instructions()  # remove our CLAUDE.md block (marker-delimited)
