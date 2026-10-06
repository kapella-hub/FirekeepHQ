"""The dashboard's recall cards show Cortex's trust marker (THREAT-MODEL §5.20).

Cortex tiers every recalled memory against the caller's verified principal and
ships `metadata.claim` plus the rendered `metadata.trust_note` ("claim from
teammate \\"Bob\\"", "claim, unattributed", ...). The Memory tab and the global
search both render recall sources as cards; without this a teammate's memory
looked exactly like the viewer's own.

The decision lives in one pure function between sentinels, executed here under
node exactly as shipped. The cards attach its output with `textContent` (via
`badge()`), so the note is never parsed as markup.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard" / "index.html"
START, END = ">>> recallTrustNote", "<<< recallTrustNote"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _extract() -> str:
    src = DASHBOARD.read_text(encoding="utf-8")
    try:
        body = src.split(START, 1)[1].split(END, 1)[0]
    except IndexError:
        pytest.fail(f"sentinels {START!r}/{END!r} missing from dashboard/index.html")
    return body.split("*/", 1)[1].rsplit("/*", 1)[0]


def _note(source) -> str:
    js = _extract() + "\nprocess.stdout.write(recallTrustNote(%s));\n" % json.dumps(source)
    p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, f"node failed: {p.stderr[:400]}"
    return p.stdout


def test_a_claim_shows_the_server_note():
    assert _note({"metadata": {"claim": True, "trust_note": 'claim from teammate "Bob"'}}) \
        == 'claim from teammate "Bob"'


def test_own_memory_shows_nothing():
    assert _note({"metadata": {"claim": False, "trust_note": ""}}) == ""


def test_a_pre_520_server_shows_nothing():
    assert _note({"metadata": {"raw_score": 0.8}}) == ""
    assert _note({}) == ""


def test_a_non_string_note_is_ignored_and_a_long_one_is_bounded():
    assert _note({"metadata": {"claim": True, "trust_note": {"x": 1}}}) == ""
    assert len(_note({"metadata": {"claim": True, "trust_note": "c" * 500}})) <= 96


def test_both_recall_card_renderers_use_it_via_badge():
    src = DASHBOARD.read_text(encoding="utf-8")
    for fn in ("function renderRecallCard(", "function renderResultCard("):
        body = src.split(fn, 1)[1].split("\nfunction ", 1)[0]
        assert "recallTrustNote(" in body, fn
