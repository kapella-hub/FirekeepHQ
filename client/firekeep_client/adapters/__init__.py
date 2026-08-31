"""firekeep_client.adapters — runtime config adapters + registry."""
from firekeep_client.adapters.base import Adapter, hook_command, shim_servers  # re-export

__all__ = ["Adapter", "shim_servers", "hook_command", "get_adapter"]


def get_adapter(name: str) -> Adapter:
    """Return the adapter for a runtime. Lazy per-name import keeps get_adapter usable
    before every adapter module exists (TDD build order: base lands before claude/codex/kiro)."""
    if name == "claude":
        from firekeep_client.adapters.claude import ClaudeAdapter
        return ClaudeAdapter()
    if name == "codex":
        from firekeep_client.adapters.codex import CodexAdapter
        return CodexAdapter()
    if name == "kiro":
        from firekeep_client.adapters.kiro import KiroAdapter
        return KiroAdapter()
    if name == "opencode":
        from firekeep_client.adapters.opencode import OpencodeAdapter
        return OpencodeAdapter()
    if name == "claude-desktop":
        from firekeep_client.adapters.claude_desktop import ClaudeDesktopAdapter
        return ClaudeDesktopAdapter()
    if name == "pi":
        from firekeep_client.adapters.pi import PiAdapter
        return PiAdapter()
    if name == "generic":
        # The target file (if any) lives in the kit config, not in the render
        # loop's signature — resolver, never cli: adapters -> cli would be a cycle.
        from firekeep_client.adapters.generic import GenericAdapter
        from firekeep_client.resolver import generic_agents_md
        return GenericAdapter(agents_md=generic_agents_md())
    raise ValueError(
        f"unknown adapter: {name!r} (expected claude|codex|kiro|opencode|pi|claude-desktop|generic)")
