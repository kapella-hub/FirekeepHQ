"""Enrollment may grant every ordinary runtime scope, but never administration."""

from __future__ import annotations

import json
import re
from pathlib import Path

from auth.keys import ENROLLABLE_SCOPES, SCOPES


def test_enrollable_scope_ceiling():
    assert ENROLLABLE_SCOPES == frozenset(SCOPES - {"admin", "*"})
    assert "vault:read" in ENROLLABLE_SCOPES
    assert "admin" not in ENROLLABLE_SCOPES
    assert "*" not in ENROLLABLE_SCOPES


def test_admin_shell_scope_literal_matches():
    script = (Path(__file__).resolve().parents[2] / "deploy" / "firekeep-admin").read_text()
    match = re.search(r"^NON_ADMIN_SCOPES='([^']+)'$", script, re.MULTILINE)
    assert match, "deploy/firekeep-admin must expose one auditable scope literal"
    assert set(json.loads(match.group(1))) == set(ENROLLABLE_SCOPES)
