"""Regression: every scope demanded by require_scope() must be grantable.

Catches the eval:write class of bug permanently (SP1a §7): create_key
validates requested scopes against SCOPES, so a scope string used in code
but absent from SCOPES is ungrantable — only "*" wildcard keys would pass.
"""

from __future__ import annotations

import re
from pathlib import Path

from auth.keys import SCOPES

REPO_ROOT = Path(__file__).resolve().parents[2]
_PATTERN = re.compile(r'require_scope\(\s*"([^"]+)"\s*\)')
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".claude",
    ".superpowers",
    ".playwright-mcp",
}


def _iter_scope_strings():
    for py in REPO_ROOT.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in py.parts):
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        yield from _PATTERN.findall(text)


def test_every_required_scope_is_grantable():
    used = set(_iter_scope_strings())
    assert used, "found no require_scope() usages — search pattern is broken"
    # Sanity anchor: the scope that was broken when this test was written.
    assert "eval:write" in used  # cortex/app/agent_gateway/api.py:43
    ungrantable = used - SCOPES
    assert not ungrantable, (
        f"Scopes required in code but absent from SCOPES (ungrantable): "
        f"{sorted(ungrantable)}"
    )


def test_scope_table_contents():
    assert "eval:write" in SCOPES, "POST /agent/action/after needs a grantable eval:write"
    assert "twin:read" not in SCOPES, "twin module deleted — dangling scope must go"
