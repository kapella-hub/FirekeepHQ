"""The Autopilot panel — and, above all, the fact that it does not write.

Why this file exists
--------------------
Round 1 of the Knowledge Autopilot proposes and reports; it never mutates. That
is a design commitment, and design commitments that live only in a plan document
decay the first time someone adds "just an approve button" to the panel that
already has every draft skill on screen. An aggregator that also mutates becomes
a second write path for five subsystems' invariants — the Skills lifecycle, the
procedures proposal store, the eval DLQ — and its first bug is silent, because
it would act on a stale read of somebody else's state. So THE ABSENCE OF A WRITE
IS THE INVARIANT here, asserted directly rather than left as an accident of
nobody having written one yet.

The second half pins the honesty rules. `total_actionable` is the sum of the
queues that could actually be READ, so a failed queue contributes zero
indistinguishably from an empty one — "3 items waiting" then reads identically
whether the rest are clear or unreachable, and only one of those is good news.
Same for a capped digest scan: Qdrant scroll pages by point ID, uncorrelated
with time, so a capped scan is an arbitrary sample and rendering it as a census
is the confident-wrong-signal failure this repo bans.

The rendering is a pure function behind sentinels for the same reason
`renderProceduresPanel` is: this file executes the SHIPPED source under node,
not a copy.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard" / "index.html"
START, END = ">>> autopilotPanel", "<<< autopilotPanel"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _src() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def _extract() -> str:
    src = _src()
    try:
        body = src.split(START, 1)[1].split(END, 1)[0]
    except IndexError:  # pragma: no cover - only when someone deletes the markers
        pytest.fail(
            f"sentinels {START!r}/{END!r} missing from dashboard/index.html. "
            "They are load-bearing: this test executes the shipped function."
        )
    # Both sentinels live INSIDE /* */ comments (the proceduresPanel precedent),
    # so a raw split leaves comment fragments at each end.
    return body.split("*/", 1)[1].rsplit("/*", 1)[0]


def _autopilot_js() -> str:
    """The panel block PLUS its loaders and handlers — the whole surface, which
    is the scope the read-only invariant has to be checked over. Checking only
    the pure block would miss a write added in the loader, which is exactly
    where one would be added."""
    src = _src()
    start = src.index(START)
    end = src.index("window.autopilotOpenSkills = autopilotOpenSkills;")
    return src[start:end]


def _render(fn: str, *args) -> str:
    js = _extract() + (
        "\nprocess.stdout.write(String(%s(%s)));\n"
        % (fn, ", ".join(json.dumps(a) for a in args))
    )
    p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, f"node failed: {p.stderr[:600]}"
    return p.stdout


# ---------------------------------------------------------------- fixtures --
# Shapes copied from cortex/app/autopilot/api.py's real responses, not invented.

DIGEST = {
    "generated_at": "2026-08-09T12:00:00+00:00",
    "window_days": 7,
    "since": "2026-08-02T12:00:00+00:00",
    "counts": {
        "memories_learned": 12, "memories_archived": 3, "memories_superseded": 2,
        "dream_insights": 1, "skills_drafted": 4, "skills_activated": 2,
        "feedback_given": 5, "gc_actions": 7,
    },
    "approximate": False,
    "scanned": 812,
    "summary": "In the last 7 days Firekeep learned 12 memories, superseded 2 and archived 3.",
    "notes": ["skills_activated counts human blessings via stale_reviewed_at."],
}

INBOX = {
    "generated_at": "2026-08-09T12:00:00+00:00",
    "items": {
        "draft_skills": {"count": 2, "approximate": False, "items": [
            {"id": "d1", "title": "Rotate the Neo4j password", "trigger": "t",
             "source_doc": "Runbook", "created": "2026-08-08T00:00:00+00:00"},
            {"id": "d2", "title": "Restore from backup", "trigger": "t",
             "source_doc": "Runbook", "created": "2026-08-08T00:00:00+00:00"},
        ]},
        "stale_skills": {"count": 0, "approximate": False, "items": []},
        "rereview_skills": {"count": 0, "approximate": False, "items": []},
        "procedure_proposals": {"enabled": True, "count": 1, "items": [
            {"id": "p1", "kind": "dead_step", "skill_id": "sk1", "step_id": "b",
             "detail": "skipped in 9 of 10 executions at no measurable cost"},
        ]},
        "contested_memories": {"count": 0, "approximate": False, "pairs": []},
        "eval_dlq": {"count": 1, "approximate": False, "items": [
            {"session_id": "sess-1", "error": "qdrant timeout",
             "failure_type": "infra", "timestamp": "2026-08-08T00:00:00+00:00"},
        ]},
    },
    "total_actionable": 4,
}


def _inbox(**overrides):
    body = json.loads(json.dumps(INBOX))
    for key, value in overrides.items():
        if key == "items":
            body["items"].update(value)
        else:
            body[key] = value
    return body


# ------------------------------------------------------------- read-only --

class TestRoundOneIsReadOnly:
    """THE invariant. Everything else in this file is about honesty; this is
    about scope."""

    def test_the_panel_issues_no_write_requests(self):
        block = _autopilot_js()
        for verb in ("POST", "PUT", "PATCH", "DELETE"):
            assert f"'{verb}'" not in block and f'"{verb}"' not in block, (
                f"the autopilot panel names the {verb} method. Round 1 proposes "
                f"and reports; it must hand every action back to the surface "
                f"that owns it rather than becoming a second write path."
            )

    def test_the_panel_calls_no_mutating_helper(self):
        block = _autopilot_js()
        for helper in ("method:", "deleteMemory", "patchSkill", "approveSkill",
                       "dismissProcedureProposal"):
            assert helper not in block, (
                f"{helper} appears in the autopilot panel — round 1 is read-only"
            )

    def test_every_fetch_is_a_bare_get_of_an_autopilot_route(self):
        block = _autopilot_js()
        urls = re.findall(r"fetchJSON\(CONFIG\.CORTEX_API \+ '([^']+)'", block)
        assert urls, "expected the loader to fetch something"
        for url in urls:
            assert url.startswith("/autopilot/"), (
                f"the panel reads {url}, outside its own read-only surface"
            )
        assert sorted(urls) == ["/autopilot/digest?days=7", "/autopilot/inbox"]


# ------------------------------------------------------------------ digest --

class TestDigest:
    def test_the_summary_sentence_is_rendered(self):
        html = _render("renderAutopilotDigest", DIGEST)
        assert "learned 12 memories" in html

    def test_counts_render_as_chips_and_zeroes_are_omitted(self):
        d = json.loads(json.dumps(DIGEST))
        d["counts"]["dream_insights"] = 0
        html = _render("renderAutopilotDigest", d)
        assert "learned" in html and "12" in html
        assert "consolidated" not in html, (
            "a zero chip is noise: the sentence already says nothing happened"
        )

    def test_a_capped_scan_is_labelled_a_lower_bound(self):
        """Scroll pages by point ID, not by time, so a capped scan is an
        arbitrary sample. Rendering it as a census is the failure."""
        d = dict(DIGEST, approximate=True, scanned=5000)
        html = _render("renderAutopilotDigest", d)
        assert "LOWER BOUND" in html
        assert "5000" in html

    def test_a_complete_scan_makes_no_such_claim(self):
        assert "LOWER BOUND" not in _render("renderAutopilotDigest", DIGEST)

    def test_an_unreadable_source_says_missing_not_zero(self):
        """A source that could not be read has no number, and zero is a number.
        Reporting one as the other is how a dead dependency reads as a quiet
        week."""
        d = dict(DIGEST, errors={"gc_actions": "redis down"})
        html = _render("renderAutopilotDigest", d)
        assert "gc_actions" in html
        assert "missing rather than zero" in html

    def test_the_proxy_notes_reach_the_reader(self):
        html = _render("renderAutopilotDigest", DIGEST)
        assert "stale_reviewed_at" in html, (
            "the activation proxy is documented in the response; hiding it in "
            "the UI puts the label's precision back on the reader's trust"
        )

    def test_a_missing_body_does_not_throw(self):
        assert "unavailable" in _render("renderAutopilotDigest", None)


# ------------------------------------------------------------------- inbox --

class TestInbox:
    def test_the_total_and_the_populated_sections_render(self):
        html = _render("renderAutopilotInbox", INBOX)
        assert "4 items waiting" in html
        assert "Draft skills awaiting approval" in html
        assert "Rotate the Neo4j password" in html
        assert "skipped in 9 of 10 executions" in html
        assert "sess-1" in html

    def test_empty_queues_are_reported_as_clear_rather_than_omitted(self):
        """Silence is also what "I never checked" looks like."""
        html = _render("renderAutopilotInbox", INBOX)
        assert "Nothing waiting in:" in html
        assert "Skills gone stale" in html
        assert "Contested memories" in html

    def test_a_disabled_subsystem_is_named_as_disabled(self):
        body = _inbox(items={"procedure_proposals": {
            "enabled": False, "count": 0, "items": []}})
        html = _render("renderAutopilotInbox", body)
        assert "Procedure proposals (disabled)" in html, (
            "'disabled' and 'nothing to propose' have different remedies and an "
            "empty list cannot tell them apart"
        )

    def test_a_degraded_queue_makes_the_total_say_it_is_incomplete(self):
        """THE case that matters. Without this line the total is a confident
        lie: it is the sum of what could be read."""
        body = _inbox(degraded=["eval_dlq"], items={
            "eval_dlq": {"count": 0, "error": "replay redis down"}})
        html = _render("renderAutopilotInbox", body)
        assert "INCOMPLETE" in html
        assert "eval_dlq" in html

    def test_a_degraded_queue_renders_as_unreadable_not_as_empty(self):
        body = _inbox(degraded=["contested_memories"], items={
            "contested_memories": {"count": 0, "error": "payload index missing"}})
        html = _render("renderAutopilotInbox", body)
        assert "unreadable" in html
        assert "payload index missing" in html
        assert "Nothing waiting in: " in html
        assert "Nothing waiting in: Contested memories" not in html, (
            "a queue that could not be read must never be listed as clear"
        )

    def test_a_contested_row_says_how_long_the_dispute_has_stood(self):
        """A contested pair is the one lifecycle state the system deliberately
        refuses to decide on its own (`POST /memory/contested/resolve` needs a
        human), so it accumulates forever unless somebody is told. How long it
        has sat is what decides whether it matters."""
        body = _inbox(items={"contested_memories": {"count": 1, "pairs": [
            {"id": "m1", "contested_with": "m2",
             "contested_at": "2026-08-08T00:00:00+00:00",
             "text_preview": "the VPS is at 10.0.0.1"}]}})
        html = _render("renderAutopilotInbox", body)
        assert "contests m2 since 2026-08-08T00:00:00+00:00" in html

    def test_a_quiet_inbox_says_so(self):
        body = {"items": {}, "total_actionable": 0}
        html = _render("renderAutopilotInbox", body)
        assert "Nothing needs your attention." in html

    def test_a_truncated_section_says_how_many_are_not_shown(self):
        """The server caps item lists at 20 while the count is the real total.
        A card showing 20 rows under a badge reading 35 must explain itself."""
        body = _inbox(items={"draft_skills": {
            "count": 35, "approximate": False,
            "items": INBOX["items"]["draft_skills"]["items"]}})
        html = _render("renderAutopilotInbox", body)
        assert "33 more not shown here." in html

    def test_an_approximate_count_is_marked_on_the_badge(self):
        body = _inbox(items={"draft_skills": dict(
            INBOX["items"]["draft_skills"], approximate=True)})
        html = _render("renderAutopilotInbox", body)
        assert "2+" in html, "a capped section count must not read as exact"

    def test_a_missing_body_does_not_throw(self):
        assert "unavailable" in _render("renderAutopilotInbox", None)


class TestEscaping:
    def test_hostile_skill_text_is_escaped(self):
        body = _inbox(items={"draft_skills": {"count": 1, "items": [
            {"id": "d1", "title": "<script>alert(1)</script>", "source_doc": ""}]}})
        html = _render("renderAutopilotInbox", body)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_a_hostile_summary_is_escaped(self):
        d = dict(DIGEST, summary="<img src=x onerror=alert(1)>")
        assert "<img" not in _render("renderAutopilotDigest", d)

    def test_a_quote_in_a_session_id_cannot_break_out_of_the_onclick(self):
        """HTML-entity escaping ALONE is not enough: the parser decodes &#39;
        back to ' BEFORE the JS in the attribute is parsed, so a lone
        entity-escaped quote still closes the string and the rest executes."""
        body = _inbox(items={"eval_dlq": {"count": 1, "items": [
            {"session_id": "');alert(1);//", "error": "e", "failure_type": "infra"}]}})
        html = _render("renderAutopilotInbox", body)
        assert "('&#39;);alert" not in html, (
            "an entity-only escape decodes straight back into an argument break"
        )
        assert "\\&#39;);alert" in html, (
            "the quote must be JS-escaped BEFORE it is HTML-escaped"
        )

    def test_the_attribute_escaper_is_not_merely_the_html_escaper(self):
        """Proves discrimination: if apAttr ever degrades to apEsc this goes
        red, which is the only way to notice."""
        assert _render("apEsc", "a'b") != _render("apAttr", "a'b")


# ------------------------------------------------------------------ wiring --

class TestWiring:
    def test_the_nav_button_exists(self):
        src = _src()
        assert 'data-tab="autopilot"' in src
        assert 'href="#i-autopilot"' in src

    def test_the_sprite_symbol_exists(self):
        assert 'symbol id="i-autopilot"' in _src(), (
            "the nav button's <use href='#i-autopilot'> resolves to nothing "
            "without it, and a missing sprite renders as blank space, not an error"
        )

    def test_the_panel_container_exists(self):
        src = _src()
        assert 'id="panel-autopilot"' in src
        assert 'id="autopilotDigest"' in src
        assert 'id="autopilotInbox"' in src

    def test_the_panel_loads_when_the_tab_opens(self):
        m = re.search(r"else if \(name === 'autopilot'\) \{([^}]*)\}", _src())
        assert m, "could not find the autopilot branch of switchTab"
        assert "loadAutopilot()" in m.group(1)

    def test_the_loader_uses_fetchJSON_not_bare_fetch(self):
        """The older tabs use bare fetch with no timeout and no status check;
        new handlers do not inherit that."""
        m = re.search(r"function loadAutopilot\([\s\S]*?\n\}", _src())
        assert m, "loadAutopilot not found"
        body = m.group(0)
        assert "fetchJSON(" in body
        assert not re.search(r"[^J]\bfetch\(", body)

    def test_both_loads_have_their_own_catch(self):
        """The routes are admin-scoped, so 403 is a REACHABLE outcome for a
        non-owner dashboard key. Without a catch the panel sits on its spinner
        forever, which reads as "still loading" rather than "refused"."""
        m = re.search(r"function loadAutopilot\([\s\S]*?\n\}", _src())
        assert m.group(0).count(".catch(") == 2, (
            "the digest and the inbox are fetched independently and must fail "
            "independently — one being unavailable is not a reason to withhold "
            "the other"
        )

    def test_every_handler_used_from_an_onclick_in_this_panel_is_exported(self):
        """The hazard: an inline onclick handler that is not window.-exported
        does nothing at click time, with no build- or test-time detection."""
        block = _extract()
        handlers = set(re.findall(r'onclick="([A-Za-z_$][\w$]*)\(', block))
        handlers |= set(re.findall(r"onclick=\\?'([A-Za-z_$][\w$]*)\(", block))
        assert handlers, "expected at least one inline onclick handler in the panel"
        src = _src()
        for h in sorted(handlers):
            assert re.search(r"\bwindow\.%s\s*=" % re.escape(h), src), (
                f"{h}() is called from an inline onclick but is never "
                f"window.-exported — the button will silently do nothing"
            )

    def test_the_refresh_button_is_bound(self):
        src = _src()
        assert 'id="btnRefreshAutopilot"' in src
        assert "$('btnRefreshAutopilot').addEventListener('click', loadAutopilot)" in src

    def test_the_skill_filter_is_set_before_the_tab_switch(self):
        """switchTab('skills') calls loadSkills() itself. Setting the filter
        afterwards would render the PREVIOUS filter's results and then silently
        disagree with the dropdown the operator is looking at."""
        m = re.search(r"function autopilotOpenSkills\([\s\S]*?\n\}", _src())
        assert m, "autopilotOpenSkills not found"
        body = m.group(0)
        assert body.index("sel.value = filter") < body.index("switchTab('skills')")
