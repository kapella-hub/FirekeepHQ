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
    raise ValueError(f"unknown adapter: {name!r} (expected claude|codex|kiro|opencode)")
