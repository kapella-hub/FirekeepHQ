"""Runs `_dom_probe.js` for real, through `node`, against a tiny stubbed DOM
built entirely in this file's own JS preamble (`document`/`window` objects
with just enough surface for the probe to walk). No jsdom, no headless
browser — that dependency would buy little here since the probe's own logic
(what counts as visible, what becomes a control's name, filtering, ref
round-tripping) is plain JS with no real rendering behind it, and pulling in
jsdom would be the first non-stdlib runtime dependency this wheel has ever
needed for a test. Skipped outright when `node` is not on PATH: this is a
belt-and-braces check for people who have it, not something CI should require.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_PROBE_PATH = Path(__file__).resolve().parent.parent / "src" / "firekeep_hands" / "_dom_probe.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")

# A minimal fake DOM: `spec` dicts become elements with just the methods the
# probe calls (`getBoundingClientRect`, `getAttribute`/`setAttribute`,
# `tagName`, `innerText`, `value`). `global.document`/`global.window` are
# plain objects assigned directly — Node resolves an undeclared bare
# identifier through the global object, which is exactly how the probe reads
# `document`/`window`/`__hands` when Chrome evaluates it for real (there,
# `__hands` is a plain assignment `browser.py` makes; here it is a global for
# the same reason `document` is: nothing else defines it in this scope).
# `__hands` itself is set per-op by `_run_ops`, not here — a single script
# can run several probe calls in sequence against the SAME `document`, the
# way `browser.py`'s repeated `Runtime.evaluate` calls all run against the
# same live page, which is what makes `window.__hands_gen` persist the way
# the probe's generation staleness check relies on.
_RUNNER_PREAMBLE = r"""
function makeElement(spec) {
    var attrs = Object.assign({}, spec.attrs || {});
    return {
        tagName: spec.tag || "DIV",
        value: spec.value !== undefined ? spec.value : "",
        innerText: spec.text || "",
        getBoundingClientRect: function () {
            return spec.rect || { left: 0, top: 0, width: 0, height: 0 };
        },
        getAttribute: function (name) {
            return Object.prototype.hasOwnProperty.call(attrs, name) ? attrs[name] : null;
        },
        setAttribute: function (name, value) { attrs[name] = value; },
    };
}

var __elements = (%(specs)s).map(makeElement);
global.window = { getComputedStyle: function () { return { visibility: "visible" }; } };
global.document = {
    querySelectorAll: function () { return __elements; },
    querySelector: function (sel) {
        var match = /data-hands-ref="([^"]+)"/.exec(sel);
        var ref = match ? match[1] : null;
        for (var i = 0; i < __elements.length; i++) {
            if (__elements[i].getAttribute("data-hands-ref") === ref) return __elements[i];
        }
        return null;
    },
};
"""


def _run_ops(specs: list[dict], ops: list[dict]) -> list[dict]:
    """Runs the probe once per entry in `ops`, all against the SAME stubbed
    `document`/`window` in one `node` process — so `window.__hands_gen`
    (and any `data-hands-ref` attributes a scan set) carry over from one op
    to the next exactly as they would across separate `Runtime.evaluate`
    calls on one real page. Returns one result per op, in order."""
    probe_source = _PROBE_PATH.read_text(encoding="utf-8")
    script = _RUNNER_PREAMBLE % {"specs": json.dumps(specs)}
    script += "\nvar __results = [];\n"
    for op in ops:
        # The probe file is itself a single `(function () { ... })()`
        # expression, so its value can be pushed directly.
        script += f"global.__hands = {json.dumps(op)};\n"
        script += f"__results.push({probe_source});\n"
    script += "\nprocess.stdout.write(JSON.stringify(__results));\n"

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        path = Path(handle.name)
    try:
        completed = subprocess.run(
            ["node", str(path)], capture_output=True, text=True, timeout=10,
        )
    finally:
        path.unlink(missing_ok=True)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _run_probe(specs: list[dict], hands: dict) -> dict:
    return _run_ops(specs, [hands])[0]


def test_probe_source_parses() -> None:
    completed = subprocess.run(
        ["node", "--check", str(_PROBE_PATH)], capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


def test_scan_skips_invisible_elements_and_tags_visible_ones() -> None:
    specs = [
        {"tag": "BUTTON", "text": "Save", "rect": {"left": 10, "top": 20, "width": 80, "height": 30}},
        {"tag": "INPUT", "text": "", "rect": {"left": 0, "top": 0, "width": 0, "height": 0}},  # empty rect
    ]
    result = _run_probe(specs, {"op": "scan", "max_nodes": 200})

    assert result["truncated"] is False
    assert len(result["controls"]) == 1
    control = result["controls"][0]
    assert control["role"] == "button"
    assert control["name"] == "Save"
    assert control["rect"] == [10, 20, 80, 30]
    assert control["ref"]


def test_scan_respects_max_nodes_and_reports_truncated() -> None:
    specs = [
        {"tag": "BUTTON", "text": f"item {i}",
         "rect": {"left": 0, "top": i * 10, "width": 50, "height": 10}}
        for i in range(5)
    ]
    result = _run_probe(specs, {"op": "scan", "max_nodes": 2})

    assert len(result["controls"]) == 2
    assert result["truncated"] is True


def test_find_filters_by_case_insensitive_substring() -> None:
    specs = [
        {"tag": "BUTTON", "text": "Sign in", "rect": {"left": 0, "top": 0, "width": 50, "height": 20}},
        {"tag": "A", "text": "Docs", "attrs": {"href": "https://example.com/docs"},
         "rect": {"left": 0, "top": 30, "width": 50, "height": 20}},
        {"tag": "BUTTON", "text": "Cancel", "rect": {"left": 0, "top": 60, "width": 50, "height": 20}},
    ]
    result = _run_probe(specs, {"op": "find", "query": "SIGN", "max_nodes": 200, "limit": 10})

    names = [c["name"] for c in result["controls"]]
    assert names == ["Sign in"]


def test_find_matches_on_href_too() -> None:
    specs = [
        {"tag": "A", "text": "Read more", "attrs": {"href": "https://example.com/pricing"},
         "rect": {"left": 0, "top": 0, "width": 50, "height": 20}},
    ]
    result = _run_probe(specs, {"op": "find", "query": "pricing", "max_nodes": 200, "limit": 10})

    assert len(result["controls"]) == 1
    assert result["controls"][0]["href"] == "https://example.com/pricing"


def test_focus_returns_ok_and_rect_for_a_known_ref() -> None:
    # "g0-d1": generation 0 is what `window.__hands_gen` defaults to before
    # any scan/find has run in this process, matching a ref hand-tagged (as
    # if by some earlier scan) at that same generation.
    specs = [
        {"tag": "INPUT", "text": "", "attrs": {"data-hands-ref": "g0-d1"},
         "rect": {"left": 5, "top": 6, "width": 100, "height": 20}},
    ]
    result = _run_probe(specs, {"op": "focus", "ref": "g0-d1", "max_nodes": 200})

    assert result["ok"] is True
    assert result["rect"] == [5, 6, 100, 20]


def test_focus_returns_not_ok_for_a_well_formed_but_absent_ref() -> None:
    result = _run_probe([], {"op": "focus", "ref": "g0-d999", "max_nodes": 200})

    assert result["ok"] is False
    assert result["reason"] == "not_found"


def test_focus_returns_not_ok_for_a_malformed_ref() -> None:
    result = _run_probe([], {"op": "focus", "ref": "not-a-ref", "max_nodes": 200})

    assert result["ok"] is False
    assert result["reason"] == "malformed"


def test_focus_rejects_a_ref_from_a_stale_generation() -> None:
    """A ref minted by an earlier scan must be refused once a later
    scan/find has run — even though nothing ever removes the
    `data-hands-ref` attribute a scan leaves behind, so the literal
    attribute value from the first scan is technically still sitting on the
    element when the second one hasn't re-tagged it (only one element here,
    so it IS re-tagged — this asserts the OLD ref is rejected regardless)."""
    specs = [
        {"tag": "BUTTON", "text": "Save", "rect": {"left": 0, "top": 0, "width": 50, "height": 20}},
    ]
    scan1, scan2, stale_focus, fresh_focus = _run_ops(specs, [
        {"op": "scan", "max_nodes": 200},
        {"op": "scan", "max_nodes": 200},
        {"op": "focus", "ref": "g1-d1", "max_nodes": 200},  # minted by the FIRST scan
        {"op": "focus", "ref": "g2-d1", "max_nodes": 200},  # minted by the SECOND (current) scan
    ])

    assert scan1["controls"][0]["ref"] == "g1-d1"
    assert scan2["controls"][0]["ref"] == "g2-d1"
    assert stale_focus == {"ok": False, "reason": "stale"}
    assert fresh_focus["ok"] is True
