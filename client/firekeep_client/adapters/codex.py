"""Codex adapter: ~/.codex/config.toml [mcp_servers.*] plus ~/.codex/AGENTS.md.

TOML has no stdlib writer, so the firekeep MCP servers are managed as a marker-delimited
TEXT block. Foreign [mcp_servers.*] sections outside the markers are never parsed or
rewritten, so they survive render/unrender untouched (non-clobbering).

Codex has NO HOOK SURFACE — no session_start, no pre_tool, nothing. Every other
runtime gets the cognitive protocol two ways (the rendered instruction block AND
the hook-delivered briefing); Codex can only get it the first way. Until 2026-07-29
it got neither: this adapter rendered MCP servers and no instruction file at all,
so a Codex user received ~94 tools and not one word about when to use them, while
docs/SETUP-CODEX.md described an AGENTS.md that was never written.

That made Codex the worst-affected runtime for the failure this fixes — an agent
told "deploy to my vps" answering that it did not know, with the answer sitting in
memory at 100% confidence.
"""
from __future__ import annotations

from pathlib import Path

from firekeep_client.adapters.base import (
    FIREKEEP_INSTRUCTIONS,
    Adapter,
    shim_servers,
    strip_block,
    strip_marked_block,
    upsert_block,
    upsert_marked_block,
    write_text_if_changed,
)

CODEX_START = "# >>> firekeep-client (managed — do not edit below) >>>"
CODEX_END = "# <<< firekeep-client (managed) <<<"


def _toml_block(venv_bin: Path) -> str:
    lines: list[str] = []
    for name, (cmd, args) in shim_servers(venv_bin).items():
        arglist = ", ".join(f'"{a}"' for a in args)
        lines.append(f"[mcp_servers.{name}]")
        # TOML literal (single-quoted) string: no escape processing -> Windows backslashes safe.
        lines.append(f"command = '{cmd}'")
        lines.append(f"args = [{arglist}]")
        lines.append("")
    return "\n".join(lines).rstrip()


class CodexAdapter(Adapter):
    name = "codex"

    def _path(self) -> Path:
        return Path.home() / ".codex" / "config.toml"

    def _instructions_path(self) -> Path:
        # Codex's global rules file. A USER-OWNED file, so only the
        # marker-delimited block is ever touched — the claude-CLAUDE.md precedent,
        # not kiro's whole-file steering doc.
        return Path.home() / ".codex" / "AGENTS.md"

    def _render_instructions(self) -> None:
        # Best-effort, mirroring the opencode adapter: an unwritable AGENTS.md must
        # never fail the install. The MCP servers are the load-bearing part; losing
        # the instruction block degrades behaviour, losing the servers breaks it.
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
        path = self._path()
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        updated = upsert_block(text, _toml_block(venv_bin), CODEX_START, CODEX_END)
        write_text_if_changed(path, updated)
        self._render_instructions()

    def unrender(self) -> None:
        self._unrender_instructions()
        path = self._path()
        if not path.exists():
            return
        stripped = strip_block(path.read_text(encoding="utf-8"), CODEX_START, CODEX_END)
        write_text_if_changed(path, stripped)
