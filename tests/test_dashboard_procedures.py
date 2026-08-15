"""The Procedures panel must not invent numbers it was not given.

Why this file exists
--------------------
`GET /procedures` returns per-step STATS keyed by step id (`app/procedures/api.py`)
and carries neither the step text nor the total step count. The design's coverage
claim -- "4 of 7 steps observable" (spec H2) -- is a statement the rollup alone
cannot make, and the whole point of showing coverage is that a coverage number
the user cannot see is the same silent-cap failure this repo bans elsewhere.

So the panel enriches each row with the authored `step_specs` from
`GET /skills/{id}`, and every case below pins what it does when that enrichment
is NOT available: it says the denominator is unknown rather than rendering
"X of X", and it labels a step by its id rather than inventing text. A panel
that quietly reports full coverage because it could not fetch the total is the
confident-wrong-signal failure `healthVerdict` was written to kill, one tab over.

The rendering is a pure function behind sentinels for the same reason
`healthVerdict` is: the previous generation of this tab decided everything
inline inside a fetch callback, so nothing could assert on it without a browser.
This file executes the SHIPPED source under node, not a copy.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard" / "index.html"
START, END = ">>> proceduresPanel", "<<< proceduresPanel"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _extract() -> str:
    src = DASHBOARD.read_text(encoding="utf-8")
    try:
        body = src.split(START, 1)[1].split(END, 1)[0]
    except IndexError:  # pragma: no cover - only when someone deletes the markers
        pytest.fail(
            f"sentinels {START!r}/{END!r} missing from dashboard/index.html. "
            "They are load-bearing: this test executes the shipped function."
        )
    # Both sentinels live INSIDE /* */ comments (the healthVerdict precedent), so
    # a raw split leaves comment fragments at each end. Cut at the close of the
    # opening comment and the open of the closing one.
    return body.split("*/", 1)[1].rsplit("/*", 1)[0]


def _render(data, skills_by_id=None) -> str:
    js = _extract() + (
        "\nconst out = renderProceduresPanel(%s, %s);\n"
        "process.stdout.write(String(out));\n"
    ) % (json.dumps(data), json.dumps(skills_by_id or {}))
    p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, f"node failed: {p.stderr[:600]}"
    return p.stdout


def _call(fn: str, *args) -> str:
    js = _extract() + (
        "\nprocess.stdout.write(String(%s(%s)));\n"
        % (fn, ", ".join(json.dumps(a) for a in args))
    )
    p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, f"node failed: {p.stderr[:600]}"
    return p.stdout


# ---------------------------------------------------------------- fixtures --
# Shapes copied from app/procedures/api.py's real response, not invented: a row
# is {skill_id, trigger, observable_steps, executions, steps, proposals}, `steps`
# is store.get_step_stats verbatim (seven int keys), `proposals` is
# store.list_proposals verbatim ({id, kind, skill_id, step_id, detail}).

ROW = {
    "skill_id": "sk1",
    "trigger": "Publish a client release",
    "observable_steps": 2,
    "executions": 4,
    "steps": {
        "a": {"observed": 3, "skipped": 1, "executions": 4,
              "observed_scored": 0, "observed_success": 0,
              "skipped_scored": 0, "skipped_success": 0},
        "b": {"observed": 1, "skipped": 3, "executions": 4,
              "observed_scored": 0, "observed_success": 0,
              "skipped_scored": 0, "skipped_success": 0},
    },
    "proposals": [],
}

SKILL = {
    "id": "sk1",
    "trigger": "Publish a client release",
    "step_specs": [
        {"id": "a", "text": "bump the bundled symdex wheel",
         "kind": "file_glob", "pattern": "*.toml", "load_bearing": True},
        {"id": "b", "text": "regenerate the bootstrap checksum",
         "kind": "file_glob", "pattern": "client/bootstrap/*", "load_bearing": False},
        {"id": "c", "text": "ask the release owner to confirm",
         "kind": "unobservable", "pattern": "", "load_bearing": False},
    ],
}


def _body(rows, specs_total=3):
    return {"procedures": rows, "count": len(rows), "specs_total": specs_total}


class TestColdStart:
    """H3: no existing skill has specs. Until some are authored the feature does
    nothing at all, which is correct and must not read as a bug."""

    def test_zero_specs_names_the_tool_that_fixes_it(self):
        html = _render(_body([], specs_total=0))
        assert "skill_add_step_specs" in html, (
            "the cold-start line must name the tool that ends the cold start"
        )

    def test_zero_specs_renders_no_procedure_card(self):
        html = _render(_body([], specs_total=0))
        assert "steps observable" not in html

    def test_a_missing_body_does_not_throw(self):
        assert "skill_add_step_specs" in _render(None)


class TestCoverage:
    """Coverage is REPORTED, never hidden or guessed."""

    def test_coverage_counts_unobservable_steps_in_the_denominator(self):
        html = _render(_body([ROW]), {"sk1": SKILL})
        assert "2 of 3 steps observable" in html, (
            "the denominator is every authored step, observable or not -- that is "
            "the number H2 exists to expose"
        )

    def test_coverage_is_shown_when_nothing_is_observable(self):
        skill = dict(SKILL)
        skill["step_specs"] = [
            {"id": "c", "text": "ask the owner", "kind": "unobservable",
             "pattern": "", "load_bearing": False},
        ]
        row = dict(ROW, observable_steps=0, steps={})
        html = _render(_body([row]), {"sk1": skill})
        assert "0 of 1 steps observable" in html

    def test_an_unavailable_spec_list_says_the_total_is_unknown(self):
        """THE case that matters. Without the skill detail the total is genuinely
        unknown; rendering "2 of 2" would claim full coverage on no evidence."""
        html = _render(_body([ROW]), {})
        assert "2 of 2 steps observable" not in html, (
            "an unknown denominator must never be filled in with the numerator"
        )
        assert "unavailable" in html.lower()
        assert "2 observable step" in html


class TestStepRows:
    def test_step_text_is_rendered_when_the_skill_is_available(self):
        html = _render(_body([ROW]), {"sk1": SKILL})
        assert "bump the bundled symdex wheel" in html
        assert "regenerate the bootstrap checksum" in html

    def test_counts_come_from_the_rollup_stats(self):
        html = _render(_body([ROW]), {"sk1": SKILL})
        assert "observed 3" in html and "skipped 1" in html
        assert "observed 1" in html and "skipped 3" in html

    def test_a_step_with_no_stats_says_so_instead_of_showing_zeros(self):
        """`steps` is written by the nightly pass. Before it has run the dict is
        empty, and "observed 0 / skipped 0" would report a measurement nobody
        made -- absence of evidence rendered as evidence of absence (I2)."""
        row = dict(ROW, steps={}, executions=0)
        html = _render(_body([row]), {"sk1": SKILL})
        assert "observed 0" not in html
        assert "no observations recorded yet" in html

    def test_unobservable_steps_are_labelled_as_such(self):
        html = _render(_body([ROW]), {"sk1": SKILL})
        assert "ask the release owner to confirm" in html
        assert "unobservable" in html

    def test_without_the_skill_the_step_id_is_the_label(self):
        html = _render(_body([ROW]), {})
        assert "bump the bundled symdex wheel" not in html
        assert "observed 3" in html, "the stats still render, keyed by id"

    def test_the_execution_count_is_shown(self):
        html = _render(_body([ROW]), {"sk1": SKILL})
        assert "4 executions" in html


class TestProposals:
    def test_a_proposal_renders_its_detail_and_a_dismiss_button(self):
        row = dict(ROW, proposals=[{
            "id": "p1", "kind": "dead_step", "skill_id": "sk1", "step_id": "b",
            "detail": "skipped in 9 of 10 executions at no measurable cost",
        }])
        html = _render(_body([row]), {"sk1": SKILL})
        assert "skipped in 9 of 10 executions at no measurable cost" in html
        assert "dismissProcedureProposal('p1')" in html

    def test_no_proposals_means_no_dismiss_button(self):
        html = _render(_body([ROW]), {"sk1": SKILL})
        assert "dismissProcedureProposal" not in html


class TestEscaping:
    """The panel builds HTML strings, and one of them is a JS call inside an
    onclick attribute -- two nested contexts, two escapes."""

    def test_hostile_trigger_text_is_escaped(self):
        row = dict(ROW, trigger="<script>alert(1)</script>")
        html = _render(_body([row]), {})
        assert "<script>" not in html

    def test_hostile_step_text_is_escaped(self):
        skill = dict(SKILL)
        skill["step_specs"] = [{"id": "a", "text": "<img src=x onerror=alert(1)>",
                                "kind": "file_glob", "pattern": "*", "load_bearing": False}]
        html = _render(_body([ROW]), {"sk1": skill})
        assert "<img" not in html

    def test_a_quote_in_a_proposal_id_cannot_break_out_of_the_onclick(self):
        """HTML-entity escaping ALONE is not enough here: the parser decodes
        &#39; back to ' BEFORE the JS in the attribute is parsed, so a lone
        entity-escaped quote still closes the string and the rest executes."""
        row = dict(ROW, proposals=[{
            "id": "');alert(1);//", "kind": "dead_step", "skill_id": "sk1",
            "step_id": "b", "detail": "d",
        }])
        html = _render(_body([row]), {"sk1": SKILL})
        assert "alert(1)" in html, "the payload should still be present, inert"
        assert "('&#39;);alert" not in html, (
            "an entity-only escape decodes straight back into an argument break: "
            "the parser hands the JS a bare ' and the rest of the payload executes"
        )
        assert "\\&#39;);alert" in html, (
            "the quote must be JS-escaped BEFORE it is HTML-escaped"
        )

    def test_the_attribute_escaper_is_not_merely_the_html_escaper(self):
        """Proves discrimination: if procAttr ever degrades to procEsc this test
        goes red, which is the only way to notice."""
        assert _call("procEsc", "a'b") != _call("procAttr", "a'b")


class TestWiring:
    """The hazard the plan names: an inline onclick handler that is not
    window.-exported does nothing at click time, with no build- or test-time
    detection anywhere."""

    def test_every_handler_used_from_an_onclick_in_this_panel_is_exported(self):
        block = _extract()
        handlers = set(re.findall(r'onclick=\\?"([A-Za-z_$][\w$]*)\(', block))
        assert handlers, "expected at least one inline onclick handler in the panel"
        src = DASHBOARD.read_text(encoding="utf-8")
        for h in sorted(handlers):
            assert re.search(r"\bwindow\.%s\s*=" % re.escape(h), src), (
                f"{h}() is called from an inline onclick but is never "
                f"window.-exported -- the button will silently do nothing"
            )

    def test_the_panel_is_loaded_when_the_skills_tab_opens(self):
        src = DASHBOARD.read_text(encoding="utf-8")
        m = re.search(r"else if \(name === 'skills'\) \{([^}]*)\}", src)
        assert m, "could not find the skills branch of switchTab"
        assert "loadProcedures()" in m.group(1)

    def test_the_panel_container_exists_in_the_skills_section(self):
        src = DASHBOARD.read_text(encoding="utf-8")
        assert 'id="proceduresPanel"' in src

    def test_a_disabled_deployment_hides_the_panel_rather_than_erroring(self):
        """PROCEDURE_ENABLED=false does not mount the router at all (the /dreams
        + /collectors precedent), so the panel must treat 404 as 'off', not as a
        fault to shout about."""
        src = DASHBOARD.read_text(encoding="utf-8")
        m = re.search(r"function loadProcedures\(\)[\s\S]*?\n\}", src)
        assert m, "loadProcedures not found"
        body = m.group(0)
        assert "404" in body and "display" in body, (
            "loadProcedures must hide the panel on a 404 rather than render an error"
        )

    def test_the_panel_uses_fetchJSON_not_bare_fetch(self):
        """The existing Skills tab uses bare fetch with no timeout and no status
        check; new handlers do not inherit that."""
        src = DASHBOARD.read_text(encoding="utf-8")
        for fn in ("loadProcedures", "dismissProcedureProposal"):
            m = re.search(r"function %s\([\s\S]*?\n\}" % fn, src)
            assert m, f"{fn} not found"
            body = m.group(0)
            assert "fetchJSON(" in body, f"{fn} must use fetchJSON"
            assert not re.search(r"[^J]\bfetch\(", body), f"{fn} must not use bare fetch"

    def test_the_dismiss_handler_has_a_catch(self):
        """Dismiss is admin-gated server-side, so a 403 is a REACHABLE outcome
        here. Without a .catch the button fails silently and the row re-renders
        unchanged -- the exact defect the existing PATCH handlers still have."""
        src = DASHBOARD.read_text(encoding="utf-8")
        m = re.search(r"function dismissProcedureProposal\([\s\S]*?\n\}", src)
        assert m
        assert ".catch(" in m.group(0)


class TestTheClosedTierBStateReachesAHuman:
    """`tier_b: "insufficient outcome signal"` used to exist only as a Celery
    task return value: no Redis record, no field on `GET /procedures`, nothing
    rendered. So a deployment where the gate is shut -- the EXPECTED state on
    today's data (spec F1) -- was byte-identical on screen to one where it is
    open and found nothing, with no last_run to say whether the pass had ever
    executed. That is the indistinguishability `/dreams`' last_run/health fields
    exist to kill, and this panel had neither.
    """

    def test_a_deployment_where_the_pass_never_ran_says_so(self):
        html = _render(_body([ROW]), {"sk1": SKILL})
        assert "has not run yet" in html

    def test_a_closed_gate_says_why_the_verdicts_are_missing(self):
        body = dict(_body([ROW]), run={
            "last_run": "2026-08-06T02:00:00+00:00", "health": "ok",
            "tier_b": "insufficient outcome signal",
        })
        html = _render(body, {"sk1": SKILL})
        assert "2026-08-06T02:00:00+00:00" in html
        assert "withheld" in html
        assert "knowable outcome" in html

    def test_a_degenerate_signal_is_named_as_such_not_as_a_shortage(self):
        """The two closed states have different remedies: one needs more
        sessions, the other needs sessions that actually differ. Reporting the
        second as the first sends an operator to collect more of what cannot
        help."""
        body = dict(_body([ROW]), run={
            "last_run": "2026-08-06T02:00:00+00:00", "health": "ok",
            "tier_b": "uniform outcome signal",
        })
        html = _render(body, {"sk1": SKILL})
        assert "same outcome" in html

    def test_a_failed_pass_is_not_reported_as_never_having_run(self):
        body = dict(_body([ROW]), run={
            "last_run": "2026-08-06T02:00:00+00:00", "health": "error",
            "tier_b": "unknown", "error": "qdrant unreachable",
        })
        html = _render(body, {"sk1": SKILL})
        assert "FAILED" in html and "qdrant unreachable" in html

    def test_dropped_unjoinable_edits_are_surfaced_not_hidden(self):
        """Spec section 4 stage 2, of exactly this drop: 'This is counted and
        surfaced, not hidden.'"""
        html = _render(dict(_body([ROW]), unjoinable_edits=7), {"sk1": SKILL})
        assert "7 recognised edits" in html

    def test_the_run_line_escapes_what_the_server_sent(self):
        body = dict(_body([ROW]), run={
            "last_run": "2026-08-06", "health": "error", "tier_b": "unknown",
            "error": "<img src=x onerror=alert(1)>",
        })
        html = _render(body, {"sk1": SKILL})
        assert "<img" not in html
        assert "&lt;img" in html

    def test_an_open_gate_with_no_decidable_step_says_so(self):
        """PROCEDURE_AGENT_CAP is spent across BOTH buckets while a verdict needs
        PROCEDURE_MIN_EXECUTIONS in EACH, and both default to 5 -- so no step can
        be decided by fewer than two distinct agent identities. Reporting "open"
        while nothing can ever be proposed is the inert-subsystem report."""
        body = dict(_body([ROW]), run={
            "last_run": "2026-08-06T02:00:00+00:00", "health": "ok",
            "tier_b": "open", "verdict_ready_steps": 0,
        })
        html = _render(body, {"sk1": SKILL})
        assert "no step yet has enough scored" in html

    def test_a_declined_stale_sweep_reaches_the_operator(self):
        """A skill scan that succeeds and returns EMPTY — wrong
        QDRANT_COLLECTION, a collection restored empty, a payload index
        mid-rebuild — is not evidence that every procedure was deleted, so the
        pass leaves the derived state standing rather than clearing it. The
        decline has to be visible: `orphans_cleared: 0` is what a healthy pass
        and a declined one both look like, and only an operator can fix the
        cause."""
        body = dict(_body([ROW]), run={
            "last_run": "2026-08-06T02:00:00+00:00", "health": "ok",
            "tier_b": "open", "verdict_ready_steps": 1,
            "orphans_cleared": 0, "orphan_sweep": "declined: vacuous scan",
        })
        html = _render(body, {"sk1": SKILL})
        assert "stale cleanup declined" in html
        assert "vacuous scan" in html

    def test_a_healthy_sweep_says_nothing_about_itself(self):
        body = dict(_body([ROW]), run={
            "last_run": "2026-08-06T02:00:00+00:00", "health": "ok",
            "tier_b": "open", "verdict_ready_steps": 1,
            "orphans_cleared": 2, "orphan_sweep": "ok",
        })
        assert "stale cleanup declined" not in _render(body, {"sk1": SKILL})

    def test_an_open_gate_with_a_decidable_step_reads_plainly(self):
        body = dict(_body([ROW]), run={
            "last_run": "2026-08-06T02:00:00+00:00", "health": "ok",
            "tier_b": "open", "verdict_ready_steps": 2,
        })
        html = _render(body, {"sk1": SKILL})
        assert "efficacy verdicts are being offered" in html


# ------------------------------------------- Phase C: enforced runbooks --
# All Phase C wire fields (mode, command_steps, bundle, deviations) are
# ADDITIVE AND OPTIONAL: a round-1 server sends none of them, and every case
# above must keep passing untouched — which is why the fixtures below extend
# ROW rather than fork it.

BUNDLE = {"version": "ab12cd34ef56", "sessions_current": 0, "sessions_stale": 3}


def _dev(i, **over):
    # i orders newest-first, matching the wire: the ledger is LPUSH
    # newest-first and the panel must preserve that order, not re-derive it.
    rec = {
        "at": "2026-08-%02dT00:00:00+00:00" % (20 - i), "kind": "block",
        "skill_id": "sk1", "step_id": "a", "session": "s%d" % i,
        "member": "morgan", "agent": "claude", "command_hash": "h%d" % i,
        "detail": "d%d" % i,
    }
    rec.update(over)
    return rec


class TestModeBadge:
    def test_each_mode_renders_its_badge(self):
        html = _render(_body([dict(ROW, mode="advise")]), {"sk1": SKILL})
        assert ">advise<" in html
        html = _render(_body([dict(ROW, mode="require_ack")]), {"sk1": SKILL})
        assert ">require_ack<" in html and "badge-yellow" in html
        html = _render(_body([dict(ROW, mode="block")]), {"sk1": SKILL})
        assert ">block<" in html and "badge-red" in html

    def test_absent_mode_renders_no_phase_c_markup_at_all(self):
        """THE backward-compat case: a round-1 rollup carries no mode, no
        command_steps, no bundle, no deviations — and the card must be the
        round-1 card, nothing more."""
        html = _render(_body([ROW]), {"sk1": SKILL})
        assert ">advise<" not in html
        assert "badge-red" not in html
        assert "setProcedureMode" not in html
        assert "<select" not in html
        assert "NOT ACTIVELY ENFORCED" not in html
        assert "Deviations" not in html


class TestNotActivelyEnforced:
    """The chip renders exactly when mode !== advise AND command_steps > 0 AND
    bundle.sessions_current === 0 — configuration without enforcement. Any
    missing field means the claim cannot be made, so it is not."""

    def test_the_derivation_holds(self):
        body = dict(_body([dict(ROW, mode="block", command_steps=2)]), bundle=BUNDLE)
        assert "NOT ACTIVELY ENFORCED" in _render(body, {"sk1": SKILL})

    def test_require_ack_also_qualifies(self):
        body = dict(_body([dict(ROW, mode="require_ack", command_steps=1)]), bundle=BUNDLE)
        assert "NOT ACTIVELY ENFORCED" in _render(body, {"sk1": SKILL})

    def test_advise_mode_is_not_enforcement_so_no_warning(self):
        body = dict(_body([dict(ROW, mode="advise", command_steps=2)]), bundle=BUNDLE)
        assert "NOT ACTIVELY ENFORCED" not in _render(body, {"sk1": SKILL})

    def test_a_current_session_means_it_is_enforced(self):
        body = dict(_body([dict(ROW, mode="block", command_steps=2)]),
                    bundle=dict(BUNDLE, sessions_current=2))
        assert "NOT ACTIVELY ENFORCED" not in _render(body, {"sk1": SKILL})

    def test_no_command_steps_means_nothing_to_enforce(self):
        body = dict(_body([dict(ROW, mode="block", command_steps=0)]), bundle=BUNDLE)
        assert "NOT ACTIVELY ENFORCED" not in _render(body, {"sk1": SKILL})

    def test_an_absent_bundle_withholds_the_claim(self):
        """A round-1 server sends no bundle; 'not actively enforced' would then
        be an invented fact about session state nobody reported."""
        body = _body([dict(ROW, mode="block", command_steps=2)])
        assert "NOT ACTIVELY ENFORCED" not in _render(body, {"sk1": SKILL})


class TestDeviationLedger:
    def test_rows_render_newest_first_in_wire_order(self):
        body = dict(_body([ROW]), deviations=[_dev(0), _dev(1)])
        html = _render(body, {"sk1": SKILL})
        assert html.index("d0") < html.index("d1")
        assert "2026-08-20T00:00:00+00:00" in html, (
            "the timestamp renders RAW: no formatter is reachable from the "
            "pure block, and an invented 'ago' is a number nobody measured"
        )

    def test_kind_and_step_id_are_shown(self):
        body = dict(_body([ROW]), deviations=[
            _dev(0), _dev(1, kind="ack", step_id="b", detail="hotfix, dry run skipped")])
        html = _render(body, {"sk1": SKILL})
        assert 'badge-red">block<' in html
        assert "hotfix, dry run skipped" in html

    def test_display_caps_at_five_with_an_honest_remainder(self):
        body = dict(_body([ROW]), deviations=[_dev(i) for i in range(7)])
        html = _render(body, {"sk1": SKILL})
        for i in range(5):
            assert "d%d" % i in html
        assert "d5" not in html and "d6" not in html
        assert "+2 more" in html, "the remainder is the real count, not a guess"

    def test_five_or_fewer_claims_no_remainder(self):
        body = dict(_body([ROW]), deviations=[_dev(i) for i in range(5)])
        assert " more<" not in _render(body, {"sk1": SKILL})

    def test_absent_deviations_render_no_deviation_markup(self):
        assert "Deviations" not in _render(_body([ROW]), {"sk1": SKILL})

    def test_an_empty_ledger_renders_no_deviation_markup(self):
        body = dict(_body([ROW]), deviations=[])
        assert "Deviations" not in _render(body, {"sk1": SKILL})

    def test_another_skills_deviations_do_not_render_on_this_row(self):
        body = dict(_body([ROW]), deviations=[_dev(0, skill_id="other")])
        html = _render(body, {"sk1": SKILL})
        assert "Deviations" not in html and "d0" not in html

    def test_hostile_detail_is_escaped(self):
        body = dict(_body([ROW]),
                    deviations=[_dev(0, detail="<img src=x onerror=alert(1)>")])
        html = _render(body, {"sk1": SKILL})
        assert "<img" not in html
        assert "&lt;img" in html


class TestModeControl:
    def test_the_control_renders_when_the_rollup_carries_a_mode(self):
        html = _render(_body([dict(ROW, mode="require_ack")]), {"sk1": SKILL})
        assert "setProcedureMode('sk1'" in html
        assert '<option value="require_ack" selected>' in html
        assert '<option value="block">' in html

    def test_no_control_without_a_mode(self):
        """A round-1 server has no mode route for the Set button to PUT to, so
        rendering the control there would be a button that can only 404."""
        assert "setProcedureMode" not in _render(_body([ROW]), {"sk1": SKILL})

    def test_a_quote_in_a_skill_id_cannot_break_out_of_the_onclick(self):
        row = dict(ROW, skill_id="');alert(1);//", mode="advise")
        html = _render(_body([row]), {})
        assert "('&#39;);alert" not in html, (
            "an entity-only escape decodes straight back into an argument break"
        )
        assert "\\&#39;);alert" in html, (
            "the quote must be JS-escaped BEFORE it is HTML-escaped"
        )


class TestPhaseCWiring:
    def test_set_mode_confirms_then_puts_then_reloads(self):
        src = DASHBOARD.read_text(encoding="utf-8")
        m = re.search(r"function setProcedureMode\([\s\S]*?\n\}", src)
        assert m, "setProcedureMode not found"
        body = m.group(0)
        assert "confirm(" in body and body.index("confirm(") < body.index("fetchJSON("), (
            "a mode change reaches every session's hook — it must be confirmed "
            "before anything is sent"
        )
        assert "'PUT'" in body
        assert "encodeURIComponent(skillId)" in body and "/mode'" in body
        assert "fetchJSON(" in body
        assert not re.search(r"[^J]\bfetch\(", body), "setProcedureMode must not use bare fetch"
        assert ".catch(" in body, "the mode route is admin-gated; 403 is reachable"
        assert "loadProcedures()" in body

    def test_load_procedures_reads_the_deviation_ledger_tolerantly(self):
        src = DASHBOARD.read_text(encoding="utf-8")
        m = re.search(r"function loadProcedures\(\)[\s\S]*?\n\}", src)
        assert m, "loadProcedures not found"
        body = m.group(0)
        assert "/procedures/deviations?limit=50" in body
        # rollup catch + per-skill catch + deviations catch: the deviations
        # route does not exist on a round-1 server, and its 404 must degrade
        # the ledger alone, never the panel.
        assert body.count(".catch(") >= 3
