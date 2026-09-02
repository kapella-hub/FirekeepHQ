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
    # encoding pinned: node emits UTF-8; text=True alone decodes with the
    # locale codepage on Windows, turning '—' into mojibake mid-assertion.
    p = subprocess.run(["node", "-e", js], capture_output=True, text=True,
                       encoding="utf-8", timeout=30)
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
        assert sorted(urls) == [
            "/autopilot/compliance", "/autopilot/digest?days=7", "/autopilot/inbox",
        ]


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


# ------------------------------------- inbox: runbook deviations (Phase C) --
# Enforced Runbooks Phase C adds a "runbook_deviations" inbox section reading
# the deviation ledger (blocks, acks, failed attempts) for the deployment's
# own workspace. The key is OPTIONAL on the wire: an older server never sends
# it, and a queue nobody checked must not be reported as clear.

DEVIATION_SECTION = {
    "enabled": True, "count": 2, "approximate": False, "items": [
        {"at": "2026-08-14T10:00:00+00:00", "kind": "block", "skill_id": "sk1",
         "step_id": "a", "session": "s1", "member": "morgan", "agent": "claude",
         "command_hash": "deadbeef", "detail": ""},
        {"at": "2026-08-13T09:00:00+00:00", "kind": "ack", "skill_id": "sk1",
         "step_id": "b", "session": "s2", "member": "morgan", "agent": "claude",
         "command_hash": "cafef00d", "detail": "hotfix, dry run skipped"},
    ],
}


class TestRunbookDeviations:
    def test_the_section_renders_kind_place_reason_and_time(self):
        body = _inbox(items={"runbook_deviations": DEVIATION_SECTION})
        html = _render("renderAutopilotInbox", body)
        assert "Runbook deviations" in html
        assert 'badge-red">block<' in html
        assert "sk1 / a" in html
        assert "hotfix, dry run skipped" in html
        assert "2026-08-14T10:00:00+00:00" in html, (
            "the timestamp renders RAW — an invented 'ago' is a number nobody "
            "measured (the proceduresPanel precedent)"
        )

    def test_an_absent_key_is_not_listed_as_clear(self):
        """THE backward-compat case: a server predating Phase C sends no
        runbook_deviations key at all. Listing the section under 'Nothing
        waiting in' would report a check nobody made."""
        html = _render("renderAutopilotInbox", INBOX)
        assert "Runbook deviations" not in html

    def test_a_present_empty_section_is_listed_as_clear(self):
        body = _inbox(items={"runbook_deviations": {
            "enabled": True, "count": 0, "approximate": False, "items": []}})
        html = _render("renderAutopilotInbox", body)
        assert "Nothing waiting in:" in html
        assert "Runbook deviations" in html

    def test_a_disabled_deployment_is_named_as_disabled(self):
        body = _inbox(items={"runbook_deviations": {
            "enabled": False, "count": 0, "items": []}})
        html = _render("renderAutopilotInbox", body)
        assert "Runbook deviations (disabled)" in html

    def test_a_truncated_section_says_how_many_are_not_shown(self):
        body = _inbox(items={"runbook_deviations": dict(DEVIATION_SECTION, count=30)})
        html = _render("renderAutopilotInbox", body)
        assert "28 more not shown here." in html

    def test_hostile_detail_is_escaped(self):
        sec = json.loads(json.dumps(DEVIATION_SECTION))
        sec["items"][1]["detail"] = "<script>alert(1)</script>"
        body = _inbox(items={"runbook_deviations": sec})
        html = _render("renderAutopilotInbox", body)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_the_cta_hands_the_operator_to_the_procedures_surface(self):
        """Read-only round 1 discipline: the row acts by handing the operator
        to the surface that owns the action, and nothing else."""
        body = _inbox(items={"runbook_deviations": DEVIATION_SECTION})
        html = _render("renderAutopilotInbox", body)
        # procedure_proposals (count 1 in the fixture) uses the same CTA, so
        # the count pins that the deviations card carries its own.
        assert html.count("autopilotOpen('skills')") == 2


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

    def test_each_load_has_its_own_catch(self):
        """The routes are admin-scoped, so 403 is a REACHABLE outcome for a
        non-owner dashboard key. Without a catch the panel sits on its spinner
        forever, which reads as "still loading" rather than "refused"."""
        m = re.search(r"function loadAutopilot\([\s\S]*?\n\}", _src())
        assert m.group(0).count(".catch(") == 3, (
            "the digest, the inbox and the compliance table are fetched "
            "independently and must fail independently — one being "
            "unavailable is not a reason to withhold the others"
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


# -------------------------------------------- compliance (Living Instructions) --
# Shape copied from cortex/app/autopilot/compliance.py's real response.

COMPLIANCE = {
    "generated_at": "2026-08-11T12:00:00+00:00",
    "sessions_evaluated": 32,
    "unparsed": 0,
    "approximate": False,
    "instructions": [
        {"key": "recall_before_work", "instruction": "Recall before you answer",
         "predicate": "memory_read_count > 0", "hits": 18, "total": 32,
         "rate": 0.5625, "earlier_rate": 0.5, "recent_rate": 0.625},
        {"key": "declared_predictions", "instruction": "Declare consequential actions",
         "predicate": "brier_score is not None", "hits": 0, "total": 32,
         "rate": 0.0, "earlier_rate": 0.0, "recent_rate": 0.0},
    ],
    "notes": [
        "Compliance measures BEHAVIOR — whether sessions did the instructed "
        "thing. It does not measure whether doing it helped.",
    ],
}


class TestComplianceTable:
    def test_rows_render_with_rate_and_predicate(self):
        html = _render("renderAutopilotCompliance", COMPLIANCE)
        assert "Recall before you answer" in html
        assert "56%" in html
        assert "18/32" in html
        assert "memory_read_count &gt; 0" in html

    def test_the_behavior_not_quality_caveat_always_renders(self):
        """The spec's honesty section, enforced at the surface: the table must
        be impossible to show without the caveat that compliance is behavior,
        not quality."""
        html = _render("renderAutopilotCompliance", COMPLIANCE)
        assert "BEHAVIOR" in html

    def test_trend_absence_renders_no_arrow(self):
        body = json.loads(json.dumps(COMPLIANCE))
        for row in body["instructions"]:
            row.pop("earlier_rate")
            row.pop("recent_rate")
        html = _render("renderAutopilotCompliance", body)
        assert "▲" not in html and "▼" not in html

    def test_unparsed_records_are_disclosed(self):
        body = json.loads(json.dumps(COMPLIANCE))
        body["unparsed"] = 3
        html = _render("renderAutopilotCompliance", body)
        assert "3 eval record(s) could not be parsed" in html

    def test_zero_sessions_is_an_empty_state_not_a_zero_table(self):
        html = _render("renderAutopilotCompliance",
                       {"sessions_evaluated": 0, "unparsed": 0,
                        "instructions": [], "notes": []})
        assert "No evaluated sessions yet" in html

    def test_a_missing_body_does_not_throw(self):
        assert "unavailable" in _render("renderAutopilotCompliance", None)

    def test_a_capped_scan_is_disclosed(self):
        """External review, 2026-08-11: the API said approximate: true and the
        renderer never showed it — a sample displayed as a census."""
        body = json.loads(json.dumps(COMPLIANCE))
        body["approximate"] = True
        html = _render("renderAutopilotCompliance", body)
        assert "Scan capped" in html

    def test_a_complete_scan_makes_no_cap_claim(self):
        html = _render("renderAutopilotCompliance", COMPLIANCE)
        assert "Scan capped" not in html


# ------------------------------------- compliance round 2 (attribution) --
# Round 2 of the measurement contract (spec 2026-08-11, "Round 2" section)
# adds two ADDITIVE per-row fields. Shape confirmed against the cortex
# implementation's contract:
#   by_runtime: {"<runtime>": {"hits": int, "total": int}, ...,
#                "unattributed": {...}}   — same frozen predicate, sliced
#   exposure:   {"exposed", "not_exposed", "unknown", "exposed_hits",
#                "exposed_rate"(float|None)} — or None for the two derived
#                rows (recall_visibly_used, outcome_bearing), which have no
#                instruction text to be exposed to.
# The renderer must feature-detect: an old server sends neither field and the
# table renders exactly as round 1 did — no empty columns, no invented zeros.


def _compliance_r2():
    body = json.loads(json.dumps(COMPLIANCE))
    body["instructions"][0].update({
        "by_runtime": {
            "claude": {"hits": 10, "total": 15},
            "codex": {"hits": 1, "total": 2},
            "unattributed": {"hits": 7, "total": 15},
        },
        "exposure": {"exposed": 5, "not_exposed": 3, "unknown": 24,
                     "exposed_hits": 4, "exposed_rate": 0.8},
    })
    body["instructions"][1].update({
        "by_runtime": {"unattributed": {"hits": 0, "total": 32}},
        "exposure": {"exposed": 5, "not_exposed": 3, "unknown": 24,
                     "exposed_hits": 0, "exposed_rate": 0.0},
    })
    body["instructions"].append({
        "key": "recall_visibly_used",
        "instruction": "Recalled knowledge used (temporal proxy)",
        "predicate": "recall_used_rate > 0",
        "hits": 9, "total": 32, "rate": 0.2813,
        "by_runtime": {"unattributed": {"hits": 9, "total": 32}},
        "exposure": None,
    })
    return body


class TestComplianceAttribution:
    def test_by_runtime_renders_each_slice_with_unattributed_last(self):
        """'unattributed' is the disclosure bucket — sessions whose client
        predates the headers — not a runtime, so it closes the list."""
        html = _render("renderAutopilotCompliance", _compliance_r2())
        assert "claude 10/15" in html
        assert "codex 1/2" in html
        assert "unattributed 7/15" in html
        assert html.index("claude 10/15") < html.index("unattributed 7/15")
        assert "By runtime" in html

    def test_exposure_renders_the_tri_state_and_the_exposed_only_rate(self):
        html = _render("renderAutopilotCompliance", _compliance_r2())
        assert "5 exp" in html and "3 not" in html and "24 unk" in html
        assert "80% of exposed" in html
        assert "(4/5)" in html, (
            "the exposed-only rate must show its own numerator/denominator — "
            "exposed_hits/exposed — or an 80% over 5 sessions reads like an "
            "80% over 32"
        )

    def test_a_null_exposure_renders_as_absent_not_as_zeros(self):
        """The derived rows have no instruction text to be exposed to. An
        exposure split there would be a category error, and rendering 0/0/0
        would present that category error as a measurement."""
        html = _render("renderAutopilotCompliance", _compliance_r2())
        assert "No instruction text to be exposed to" in html
        assert "0 exp · 0 not · 0 unk" not in html

    def test_a_zero_exposed_bucket_yields_no_rate_claim(self):
        """exposed_rate is null when exposed == 0: a rate over an empty
        denominator is not 0%, it is undefined, and the cell must say which."""
        body = _compliance_r2()
        body["instructions"][0]["exposure"] = {
            "exposed": 0, "not_exposed": 0, "unknown": 32,
            "exposed_hits": 0, "exposed_rate": None}
        html = _render("renderAutopilotCompliance", body)
        assert "no exposed sessions" in html
        # Row 1 (0/5 exposed hits) still carries its genuine 0% — the absence
        # is row 0's alone, which the ordering pins:
        assert html.index("no exposed sessions") < html.index("0% of exposed")

    def test_an_old_server_response_renders_no_attribution_columns(self):
        """THE degradation case: a pre-0.1.41 server sends rows with neither
        field. The table must render exactly the round-1 surface — no new
        headers, no dash-filled columns, nothing inventing attribution."""
        html = _render("renderAutopilotCompliance", COMPLIANCE)
        assert "By runtime" not in html
        assert "Exposure" not in html
        assert "of exposed" not in html
        assert "unattributed" not in html

    def test_a_row_missing_both_fields_in_a_mixed_response_degrades_to_dashes(self):
        """Defensive per-row tolerance: if one row carries attribution and
        another does not, the bare row renders '—' cells rather than throwing
        or fabricating zeros."""
        body = _compliance_r2()
        del body["instructions"][1]["by_runtime"]
        del body["instructions"][1]["exposure"]
        html = _render("renderAutopilotCompliance", body)
        assert "claude 10/15" in html, "the attributed row still renders"
        assert "Declare consequential actions" in html, "the bare row still renders"
        assert "—" in html

    def test_a_hostile_runtime_name_is_escaped(self):
        """X-Firekeep-Runtime is an untrusted observability label; a hostile
        agent controls it end-to-end, so it lands in the table as text."""
        body = _compliance_r2()
        body["instructions"][0]["by_runtime"] = {
            "<script>alert(1)</script>": {"hits": 1, "total": 2}}
        html = _render("renderAutopilotCompliance", body)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_the_headline_rate_keeps_the_all_sessions_denominator(self):
        """Additive only: attribution appears BESIDE the headline numbers, and
        the headline hits/total/rate stay over all evaluated sessions —
        baseline comparability is the round-2 contract's hard constraint."""
        html = _render("renderAutopilotCompliance", _compliance_r2())
        assert "18/32" in html and "56%" in html

    def test_unattributed_sorts_last_even_when_the_server_emits_it_first(self):
        """The fixture above happens to emit `unattributed` last, so an
        order-PRESERVING renderer would pass the ordering assertion by luck.
        This one reverses the emission order to pin the sort itself."""
        body = _compliance_r2()
        body["instructions"][0]["by_runtime"] = {
            "unattributed": {"hits": 7, "total": 15},
            "claude": {"hits": 10, "total": 15},
        }
        html = _render("renderAutopilotCompliance", body)
        assert html.index("claude 10/15") < html.index("unattributed 7/15")

    def test_a_runtime_bucket_missing_its_counts_is_skipped_not_zeroed(self):
        body = _compliance_r2()
        body["instructions"][0]["by_runtime"] = {
            "kiro": {"hits": None, "total": None}}
        html = _render("renderAutopilotCompliance", body)
        assert "kiro" not in html, (
            "a bucket without counts must not render at all — '?/?' or '0/0' "
            "would both be inventions"
        )

    def test_every_note_reaches_the_reader_not_only_the_first(self):
        """Round 2's unattributed-window disclosure rides the notes array
        behind the behavior caveat; a renderer showing notes[0] only would
        strand it at the API — the same stop-at-the-API failure the
        approximate flag had (external review, 2026-08-11)."""
        body = _compliance_r2()
        body["notes"] = list(body.get("notes") or []) + [
            "Sessions from clients predating 0.1.41 carry no attribution "
            "headers and count as unknown, forever — nothing backfills."]
        html = _render("renderAutopilotCompliance", body)
        assert "nothing backfills" in html


# -------------------------------------------------- trust ledger (round 1) --
# Shape copied from cortex/app/autopilot/trust.py's real build_trust response.
# The card is visibility-only: a null biased metric renders "—" with its
# reason (never a measured 0), truncation shows a banner, invalids are footnoted
# rather than dropped, and an empty ledger says so instead of inventing a row.


def render_trust(data):
    # Reuses the file's node-extraction harness, pointed at the ledger renderer.
    return _render("renderAutopilotTrust", data)


class TestTrustCard:
    def test_rows_render_with_components(self):
        data = {"agents": [{"agent_id": "agent-x", "declared": 214, "reconciled": 205,
                            "reconciliation_rate": 0.96, "scored_predictions": 180,
                            "calibration": 0.11, "calibration_trend": -0.03, "reversals": 3,
                            "sessions": 28, "first_seen_in_window": "2026-08-01T00:00:00+00:00",
                            "last_seen_in_window": "2026-08-16T00:00:00+00:00"}],
                "window_days": 30, "scanned": 900, "truncated": False,
                "invalid": {"unattributed_predict": 0, "missing_action_id": 0,
                            "malformed": 0, "bad_timestamp": 0}, "generated_at": "2026-08-16T00:00:00+00:00"}
        html = render_trust(data)  # helper in the test that extracts renderAutopilotTrust
        assert "agent-x" in html and "214" in html and "no agent" not in html

    def test_null_calibration_shows_dash_not_zero(self):
        data = {"agents": [{"agent_id": "a", "declared": 3, "reconciled": 2,
                            "reconciliation_rate": None, "scored_predictions": 1,
                            "calibration": None, "calibration_trend": None, "reversals": 0,
                            "sessions": 1, "first_seen_in_window": None,
                            "last_seen_in_window": "2026-08-16T00:00:00+00:00"}],
                "window_days": 30, "scanned": 3, "truncated": False,
                "invalid": {"unattributed_predict": 0, "missing_action_id": 0,
                            "malformed": 0, "bad_timestamp": 0}, "generated_at": "x"}
        html = render_trust(data)
        assert "—" in html and ">0<" not in html.split("agent")[1][:200]

    def test_truncation_banner(self):
        data = {"agents": [], "window_days": 30, "scanned": 50000, "truncated": True,
                "invalid": {"unattributed_predict": 0, "missing_action_id": 0,
                            "malformed": 0, "bad_timestamp": 0}, "generated_at": "x"}
        assert "truncat" in render_trust(data).lower()

    def test_empty_says_no_declarations(self):
        data = {"agents": [], "window_days": 30, "scanned": 0, "truncated": False,
                "invalid": {"unattributed_predict": 0, "missing_action_id": 0,
                            "malformed": 0, "bad_timestamp": 0}, "generated_at": "x"}
        assert "no agent" in render_trust(data).lower()


# ------------------------------------------------------------------- fleet --
# Shape copied from cortex/app/fleet/ledger.py's real summarize() response
# (Task 7) and surfaced at digest.fleet.jobs (Task 7's digest.py wiring).

FLEET = {
    "enabled": True,
    "jobs": {
        "distill_session": {
            "window": {"produced": 0, "approved": 0, "rejected": 0, "approval_rate": None},
            "all_time": {"produced": 0, "approved": 0, "rejected": 0, "approval_rate": None, "pending": 0}},
        "reauthor_stale_skill": {
            "window": {"produced": 3, "approved": 2, "rejected": 1, "approval_rate": 0.667},
            "all_time": {"produced": 9, "approved": 5, "rejected": 2, "approval_rate": 0.714, "pending": 2}},
        "propose_contested_verdict": {
            "window": {"proposed": 2, "resolved": 1, "matched": 1, "match_rate": 1.0},
            "all_time": {"proposed": 4, "resolved": 1, "matched": 1, "match_rate": 1.0}},
    },
}


class TestFleet:
    def test_the_digest_renders_a_fleet_table(self):
        d = dict(DIGEST, fleet=FLEET)
        html = _render("renderAutopilotDigest", d)
        assert "Fleet" in html
        assert "Stale-skill re-author" in html and "Contested-verdict proposal" in html
        assert "67%" in html and "71%" in html   # window and all-time approval rates
        assert "100%" in html                    # match rate

    def test_a_null_rate_is_a_dash_never_zero_percent(self):
        html = _render("renderAutopilotDigest", dict(DIGEST, fleet=FLEET))
        assert "—" in html
        # A standalone "0%" (not preceded by another digit or a decimal point)
        # would mean a null rate rendered as zero instead of a dash. Plain
        # substring exclusion of "0%" false-fails on any genuine rate ending
        # in zero (e.g. "70%"), which "100%" alone doesn't cover.
        assert not re.search(r"(?<![\d.])0%", html)

    def test_no_fleet_block_renders_no_table(self):
        assert "Fleet" not in _render("renderAutopilotDigest", DIGEST)

    def test_a_contested_row_shows_the_proposal(self):
        row = {"id": "m1", "contested_with": "m2", "contested_at": "2026-09-01",
               "text_preview": "Deploy with update.sh",
               "proposed_verdict": {"action": "supersede", "winner_id": "m1"},
               "proposed_rationale": "m1 names the current script",
               "proposed_by": "night-shift", "proposed_at": "2026-09-02T03:00:00+00:00"}
        html = _render("apContestedRow", row)
        assert "Night Shift proposes" in html and "keep m1" in html and "supersede m2" in html
        assert "m1 names the current script" in html and "night-shift" in html

    def test_a_coexist_proposal_reads_as_both_true(self):
        row = {"id": "m1", "contested_with": "m2", "text_preview": "A",
               "proposed_verdict": {"action": "coexist", "winner_id": None},
               "proposed_rationale": "", "proposed_by": "night-shift", "proposed_at": ""}
        assert "both true" in _render("apContestedRow", row)

    def test_a_row_without_a_proposal_is_unchanged(self):
        row = {"id": "m1", "contested_with": "m2", "contested_at": "x", "text_preview": "A"}
        assert "proposes" not in _render("apContestedRow", row)

    def test_low_efficacy_section_is_listed(self):
        inbox = _inbox(items={"low_efficacy_skills": {"count": 1, "approximate": False, "items": [
            {"id": "s1", "trigger": "Rotate the key", "skill_efficacy": 0.31, "skill_efficacy_n": 7}]}})
        html = _render("renderAutopilotInbox", inbox)
        assert "Rotate the key" in html and "0.31" in html and "n=7" in html

    def test_every_api_section_key_has_a_dashboard_entry(self):
        """The class of drift low_efficacy_skills had: emitted, documented, and
        never rendered — so the headline total counted rows nobody could see."""
        api = (DASHBOARD.parents[1] / "cortex/app/autopilot/api.py").read_text(encoding="utf-8")
        emitted = set(re.findall(r'"([a-z_]+)": await _section\(', api))
        block = _autopilot_js()
        listed = set(re.findall(r"\{ key: '([a-z_]+)'", block))
        assert emitted <= listed, f"API sections missing from AUTOPILOT_SECTIONS: {sorted(emitted - listed)}"
