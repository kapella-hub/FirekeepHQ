"""Codex adapter: ~/.codex/config.toml [mcp_servers.*] (stdio shim commands), no hooks.

TOML has no stdlib writer, so the firekeep MCP servers are managed as a marker-delimited
TEXT block. Foreign [mcp_servers.*] sections outside the markers are never parsed or
rewritten, so they survive render/unrender untouched (non-clobbering).
"""
from __future__ import annotations

from pathlib import Path

from firekeep_client.adapters.base import Adapter, read_pin, shim_servers, strip_block, upsert_block

CODEX_START = "# >>> firekeep-client (managed — do not edit below) >>>"
CODEX_END = "# <<< firekeep-client (managed) <<<"


def _toml_block(venv_bin: Path, pin: str | None = None) -> str:
    lines: list[str] = []
    for name, (cmd, args) in shim_servers(venv_bin).items():
        arglist = ", ".join(f'"{a}"' for a in args)
        lines.append(f"[mcp_servers.{name}]")
        # TOML literal (single-quoted) string: no escape processing -> Windows backslashes safe.
        lines.append(f"command = '{cmd}'")
        lines.append(f"args = [{arglist}]")
        if pin:
            lines.append(f'env = {{ FIREKEEP_PROFILE = "{pin}" }}')
        lines.append("")
    return "\n".join(lines).rstrip()


class CodexAdapter(Adapter):
    name = "codex"

    def _path(self) -> Path:
        return Path.home() / ".codex" / "config.toml"

    def render(self, *, venv_bin: Path) -> None:
        path = self._path()
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        updated = upsert_block(text, _toml_block(venv_bin, read_pin(self.name)), CODEX_START, CODEX_END)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated, encoding="utf-8")

    def unrender(self) -> None:
        path = self._path()
        if not path.exists():
            return
        stripped = strip_block(path.read_text(encoding="utf-8"), CODEX_START, CODEX_END)
        path.write_text(stripped, encoding="utf-8")
