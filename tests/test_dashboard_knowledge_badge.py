"""The Knowledge tab must not render an intention as a result.

Why this file exists
--------------------
`GET /knowledge/sources` reported `classified / skills_queued=N` for a source
whose every skill draft had died, because the fan-out Celery tasks reported
their outcome to nobody. The server side now derives two verdicts on top of the
stored status -- `drafts_failed` (a draft REPORTED failure) and `drafts_missing`
(nothing drafted, nothing landed, and the record has not moved for well past the
worst-case drafting time) -- and this is the surface a human actually reads them
on. Measured live: "Runbook: Restart stuck Celery worker" sat at queued 1 /
drafted 0 from 2026-07-12, and the row rendered a cheerful "Drafting... (0/1)"
for 25 days.

A badge that says "Drafting..." about work that stopped a month ago is the same
failure `test_dashboard_health.py` was written for: not "no signal" but
"confident wrong signal", on a screen a buyer looks at.

THE CASE THAT MATTERS is `test_drafts_missing_does_not_render_as_progress`. It
is the bug restated. `TestTheOldCodeWouldFailThese` guards against this file
becoming decoration, by running the PRE-CHANGE branch set against the same
inputs and asserting it gets them wrong -- so the suite proves it can
discriminate rather than merely being green.

SCOPE, stated rather than implied: the shipped `esc()` builds a DOM node
(`document.createElement`), which does not exist under bare node, so the harness
substitutes a faithful HTML-escaper. These tests therefore verify the
status->badge DECISION and that untrusted values are passed THROUGH `esc` at
all. They do not verify `esc` itself.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard" / "index.html"
START, END = ">>> knowledgeStatusBadge", "<<< knowledgeStatusBadge"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

# Stand-ins for two helpers the shipped function closes over. `esc` mirrors what
# textContent -> innerHTML does to the characters that matter for markup;
# `relTime` is rendered inert so a stale-processing string stays assertable.
_PRELUDE = """
function esc(s) {
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function relTime(ts) { return 'REL(' + String(ts) + ')'; }
"""


def _extract() -> str:
    src = DASHBOARD.read_text(encoding="utf-8")
    try:
        body = src.split(START, 1)[1].split(END, 1)[0]
    except IndexError:  # pragma: no cover - only when someone deletes the markers
        pytest.fail(
            f"sentinels {START!r}/{END!r} missing from dashboard/index.html. "
            "They are load-bearing: this test executes the shipped function."
        )
    # Both sentinels sit INSIDE /* */ comments, so a raw split leaves a dangling
    # `*/` at the top and a bare `/*` at the bottom -- either is a syntax error
    # before a single assertion can run. Cut at the comment boundaries.
    return body.split("*/", 1)[1].rsplit("/*", 1)[0]


def _badge(source: dict, *, js_body: str | None = None) -> str:
    js = _PRELUDE + (js_body if js_body is not None else _extract()) + (
        "\nprocess.stdout.write(knowledgeStatusBadge(%s));\n" % json.dumps(source)
    )
    p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, f"node failed: {p.stderr[:400]}"
    return p.stdout


# The live shape, verbatim: classified, one draft queued, none drafted, none
# landed in Qdrant, no skills_failed key at all (the record predates per-draft
# counting, which is exactly why `drafts_failed` can never describe it).
LIVE_STUCK = {
    "status": "drafts_missing", "disposition": "procedural",
    "skills_queued": 1, "draft_skill_count": 0, "updated_at": "2026-07-12T10:00:00+00:00",
}


class TestTheBug:
    def test_drafts_missing_does_not_render_as_progress(self):
        """THE regression. For 25 days this row said 'Drafting...'."""
        out = _badge(LIVE_STUCK)
        assert "Drafting" not in out, "work that stopped a month ago is not progress"
        assert "badge" in out and "drafts_missing" != out.strip()

    def test_drafts_missing_says_what_happened_and_what_to_do(self):
        out = _badge(LIVE_STUCK)
        assert "never landed" in out
        assert "re-ingest" in out.lower(), "a dead-end state must name its remedy"

    def test_drafts_missing_is_not_dressed_as_an_observed_failure(self):
        """Nothing reported failure. Claiming it did is the same overreach in
        the other direction, and red is how this dashboard says 'it failed'."""
        out = _badge(LIVE_STUCK)
        assert "badge-red" not in out
        assert "badge-yellow" in out

    def test_an_unknown_future_status_still_renders_something(self):
        """The fallthrough is the only thing standing between a new server-side
        status and a blank cell."""
        assert _badge({"status": "some_new_verdict"}).strip() == "some_new_verdict"


class TestTheOtherHalfOfTheTrap:
    """A change that turns healthy rows into warnings is the same defect
    reversed. These are the states that must NOT be alarmed."""

    def test_a_genuinely_in_flight_ingest_still_reads_as_progress(self):
        out = _badge({
            "status": "classified", "disposition": "procedural",
            "skills_queued": 3, "draft_skill_count": 1,
        })
        assert "Drafting" in out and "1/3" in out
        assert "never landed" not in out

    def test_a_fully_drafted_source_is_green(self):
        out = _badge({
            "status": "classified", "disposition": "procedural",
            "skills_queued": 2, "draft_skill_count": 2,
        })
        assert "badge-green" in out and "2 draft(s)" in out

    def test_a_reference_document_is_not_treated_as_missing_drafts(self):
        """It queued nothing because it HAS no procedures. Nothing is wrong."""
        out = _badge({
            "status": "classified", "disposition": "reference",
            "skills_queued": 0, "draft_skill_count": 0,
        })
        assert "Reference" in out and "never landed" not in out

    def test_corpus_only_stays_neutral_not_an_error(self):
        """No generation model deployed is a deployment shape, not a fault."""
        out = _badge({"status": "corpus_only"})
        assert "badge-blue" in out and "badge-red" not in out

    def test_observed_failure_is_still_reported_as_failure(self):
        out = _badge({
            "status": "drafts_failed", "skills_queued": 1,
            "draft_skill_count": 0, "skills_failed": 1,
        })
        assert "badge-red" in out and "failed" in out

    def test_unknown_renders_nothing_rather_than_a_placeholder_badge(self):
        assert _badge({"status": "unknown"}) == ""


class TestUntrustedValuesReachEsc:
    """`last_draft_error` is an LLM/exception string and lands in markup."""

    def test_a_draft_error_containing_markup_is_escaped(self):
        out = _badge({
            "status": "drafts_failed", "skills_queued": 1, "draft_skill_count": 0,
            "skills_failed": 1, "last_draft_error": "<img src=x onerror=alert(1)>",
        })
        assert "<img" not in out
        assert "&lt;img" in out


class TestTheOldCodeWouldFailThese:
    """Proof this file can discriminate.

    Before the change there was no `drafts_missing` branch, so the status fell
    through to `return esc(st)` and the cell showed the bare machine string. If
    these ever pass against the pre-change function, the tests above are
    decoration.
    """

    def _pre_change(self) -> str:
        """The shipped function with only the drafts_missing branch removed."""
        src = _extract()
        start = src.index("    if (st === 'drafts_missing') {")
        end = src.index("    if (st === 'classified') {")
        cut = src[:start] + src[end:]
        assert "drafts_missing" not in cut, "failed to remove the branch under test"
        return cut

    def test_the_old_code_rendered_the_raw_status_string(self):
        out = _badge(LIVE_STUCK, js_body=self._pre_change())
        assert out.strip() == "drafts_missing", (
            "expected the pre-change fallthrough; if this changed, update the "
            "discriminator rather than deleting it"
        )

    def test_the_old_code_offered_no_remedy(self):
        out = _badge(LIVE_STUCK, js_body=self._pre_change())
        assert "re-ingest" not in out.lower() and "never landed" not in out
