"""Generic adapter: any MCP client the kit ships no bespoke adapter for.

It delivers the UNIVERSAL FLOOR — the one MCP gateway, and the instruction
protocol — and nothing else, because there is nothing else to deliver: a client
we know nothing about exposes no hook surface, no settings file we can safely
write, and no config path we could guess without risking a clobber. So render
PRINTS the gateway snippet for the user to paste, and (only when pointed at a
rules file with --agents-md) upserts the hook-free instruction block.

The honest degraded tier — see the `generic` column in contract/matrix.py.
Codex is the precedent for a no-hooks runtime; the difference here is that
generic owns no native config file at all.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from firekeep_client.adapters.base import (
    GENERIC_INSTRUCTIONS,
    Adapter,
    has_marked_begin,
    rendered_instructions_path,
    shim_servers,
    strip_marked_block,
    upsert_marked_block,
    write_text_if_changed,
)

# The runtimes that own a fixed instruction path. Generic must never be pointed
# at one of them: every block shares the same BEGIN prefix, so two adapters on
# one file would replace each other's block on alternate renders.
_FOUR = ("claude", "codex", "kiro", "opencode")

_NOTE = """
You get: all MCP tools (memory, sessions, coordination, code intelligence), and
the cognitive protocol is delivered automatically when your client connects.
You do NOT get (a generic client exposes no hooks Firekeep can wire):
auto-briefing, the pre-edit blocking gate, stop->learn, and the pre-compaction
checkpoint. Point --agents-md at your client's rules file to also install the
protocol as text.
"""


def known_instruction_paths() -> list[tuple[str, Path]]:
    """(runtime, path) for each adapter that owns a fixed instruction file.

    Resolved on every call, never cached: opencode's path follows
    XDG_CONFIG_HOME, which can differ between one install and the next."""
    return [(rt, p) for rt in _FOUR if (p := rendered_instructions_path(rt)) is not None]


class GenericAdapter(Adapter):
    name = "generic"

    def __init__(self, agents_md: Path | None = None) -> None:
        self.agents_md = Path(agents_md).expanduser().resolve() if agents_md else None

    def render(self, *, venv_bin: Path) -> None:
        command, args = shim_servers(venv_bin, self.name)["firekeep"]
        snippet = json.dumps(
            {"mcpServers": {"firekeep": {"command": command, "args": list(args)}}},
            indent=2,
        )
        print("Firekeep works with any MCP client. Paste this into your client's MCP config:\n")
        print(snippet)
        print(_NOTE)
        if self.agents_md is not None:
            self._render_block(self.agents_md)

    def _render_block(self, target: Path) -> None:
        # Collision check first, and BEFORE any write: refusing after we have
        # already clobbered another adapter's file would be no refusal at all.
        for runtime, owned in known_instruction_paths():
            if target == owned.expanduser().resolve():
                raise ValueError(
                    f"{target} is already managed by the {runtime} adapter; "
                    "point --agents-md at a different file."
                )
        # Best-effort, mirroring codex: the printed snippet is the load-bearing
        # half of this adapter, so an unwritable rules file warns and continues.
        try:
            existing = target.read_text(encoding="utf-8") if target.exists() else ""
            write_text_if_changed(target, upsert_marked_block(existing, GENERIC_INSTRUCTIONS))
        except (OSError, UnicodeDecodeError) as exc:
            print(f"firekeep: WARNING — could not update {target}: {exc}", file=sys.stderr)

    def unrender(self) -> None:
        target = self.agents_md
        if target is None or not target.exists():
            return  # never opted in, or the file is already gone
        try:
            text = target.read_text(encoding="utf-8")
            if has_marked_begin(text):
                write_text_if_changed(target, strip_marked_block(text))
        except (OSError, UnicodeDecodeError) as exc:
            print(f"firekeep: WARNING — could not clean {target}: {exc}", file=sys.stderr)
