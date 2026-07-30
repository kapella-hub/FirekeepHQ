"""GET /sessions/{id} returns `shadow` as a MARKDOWN STRING, not a dict.

Both existing test_skill_scorer.py / test_skill_synthesizer.py fixtures mock
`shadow` as a dict shaped `{"scratch": {...}, "decision": [...]}` — the WRONG
container type (Bridge actually returns `assemble_shadow(data)`, a Markdown
str) and the WRONG section key (`decisions`, plural, not `decision`) with the
WRONG entry key (`content`, not `value`). That triple mismatch is why
`_score_resolution_language`'s `except Exception: return 0.0` never surfaced
in CI: calling `.get()` on a str raises AttributeError, which the bare except
swallows, and every mock happened to hide it.

These fixtures are hand-written to match `bridge/app/shadow.py::assemble_shadow`'s
real Markdown output (`## Session: ...`, `### Decisions`, `- [HH:MM] ...`,
`### Scratchpad`, `- key: value`) rather than a dict, so they cannot re-encode
the same mistake.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.skills.scorer import _score_resolution_language

RESOLUTION_SHADOW_MARKDOWN = (
    "## Session: fix the collector\n"
    "**Status**: completed | **Started**: 2026-07-29T10:00:00 | **Updated**: 2026-07-29T11:00:00\n"
    "\n"
    "### Plan\n"
    "*No plan set*\n"
    "\n"
    "### Decisions\n"
    "- [10:15] root cause was a stale lock; the fix was to release it on timeout\n"
    "\n"
    "### Files Known\n"
    "*No files tracked*\n"
    "\n"
    "### Progress\n"
    "*No progress logged*\n"
    "\n"
    "### Scratchpad\n"
    "- outcome: finally resolved after restarting the worker\n"
)

NO_RESOLUTION_SHADOW_MARKDOWN = (
    "## Session: plan some work\n"
    "**Status**: active | **Started**: 2026-07-29T10:00:00 | **Updated**: 2026-07-29T10:05:00\n"
    "\n"
    "### Plan\n"
    "- investigate the collector\n"
    "\n"
    "### Decisions\n"
    "*No decisions recorded*\n"
    "\n"
    "### Files Known\n"
    "*No files tracked*\n"
    "\n"
    "### Progress\n"
    "*No progress logged*\n"
    "\n"
    "### Scratchpad\n"
    "*Empty*\n"
)


def _mock_http_get(json_body):
    """Build the mocked httpx.AsyncClient the scorer uses to return `json_body`
    from GET /sessions/{id}, mirroring the existing test_skill_scorer.py style."""
    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.get = AsyncMock(
        return_value=MagicMock(status_code=200, json=MagicMock(return_value=json_body))
    )
    return mock_http


@pytest.mark.asyncio
async def test_resolution_score_reads_a_markdown_shadow():
    """shadow is a str; calling .get() on it raises AttributeError into a bare
    except and silently zeroes the 0.35-weighted resolution signal."""
    mock_http = _mock_http_get({"shadow": RESOLUTION_SHADOW_MARKDOWN})
    with patch("app.skills.scorer.httpx.AsyncClient", return_value=mock_http):
        score = await _score_resolution_language("sess-1", "http://bridge:8070")
    assert score > 0.0


@pytest.mark.asyncio
async def test_resolution_score_is_zero_for_a_shadow_with_no_resolution_language():
    mock_http = _mock_http_get({"shadow": NO_RESOLUTION_SHADOW_MARKDOWN})
    with patch("app.skills.scorer.httpx.AsyncClient", return_value=mock_http):
        score = await _score_resolution_language("sess-1", "http://bridge:8070")
    assert score == 0.0


@pytest.mark.asyncio
async def test_resolution_score_still_accepts_a_dict_shadow_with_correct_keys():
    """Future-proofing branch: if `shadow` ever becomes a dict again, the real
    key names are `decisions`/`progress` (plural) with entries shaped
    {timestamp, content} -- not `decision` with {value}.

    The resolution phrase lives ONLY in the `decisions` entry (scratch has none),
    so this fails under the old `shadow.get("decision", [])` key -- unlike a
    fixture that also seeds scratch with a resolution phrase, which would pass
    even with the wrong decisions key and mask this exact defect.
    """
    mock_http = _mock_http_get({
        "shadow": {
            "scratch": {"outcome": "worker restarted"},
            "decisions": [{"timestamp": "2026-07-29T10:15:00", "content": "root cause found, fixed by restarting"}],
            "progress": [],
        }
    })
    with patch("app.skills.scorer.httpx.AsyncClient", return_value=mock_http):
        score = await _score_resolution_language("sess-1", "http://bridge:8070")
    assert score > 0.0
