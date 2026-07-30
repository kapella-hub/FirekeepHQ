"""Tests for SessionManager.get_shadow_epoch — the reader precompact's
ctx_update(category="scratch", key="shadow_epoch") writes through.
"""

import pytest
from unittest.mock import AsyncMock

from app.config import Settings
from app.session import SessionManager
from app import residency


class TestShadowEpoch:
    @pytest.mark.asyncio
    async def test_shadow_epoch_is_empty_when_never_bumped(self, mock_redis):
        mock_redis.hget = AsyncMock(return_value=None)
        mgr = SessionManager(mock_redis, Settings())
        assert await mgr.get_shadow_epoch("sess-1") == ""

    @pytest.mark.asyncio
    async def test_shadow_epoch_reads_the_scratch_field_precompact_wrote(self, mock_redis):
        """precompact bumps the epoch through the ordinary ctx_update scratch path —
        no new MCP tool, and no new Redis key."""
        mock_redis.hget = AsyncMock(return_value="1700000000000")
        mgr = SessionManager(mock_redis, Settings())
        assert await mgr.get_shadow_epoch("sess-1") == "1700000000000"
        mock_redis.hget.assert_awaited_once_with("nb:session:sess-1:scratch", "shadow_epoch")

    @pytest.mark.asyncio
    async def test_epoch_is_NONE_not_empty_when_the_read_fails(self, mock_redis):
        """AMENDED 2026-07-30 (C2, Critical). An earlier version of this task returned
        "" on a read error and claimed that "mismatches every cursor". That was FALSE:
        "" is a real, matchable state carried by every cursor minted before the first
        compaction, so an errored read matched a STALE post-compaction cursor and served
        a delta to an agent that had just lost its context — a guard that failed OPEN.
        None is unmatchable by construction, so a failure cannot pass for a state."""
        mock_redis.hget = AsyncMock(side_effect=RuntimeError("redis down"))
        mgr = SessionManager(mock_redis, Settings())
        assert await mgr.get_shadow_epoch("sess-1") is None


# --- ctx_get_shadow(since=...) wiring (Task 7) ---------------------------------
#
# HARNESS: there is no `bridge_tools` fixture. Every bridge MCP-tool test patches
# app.mcp_server._get_manager with an AsyncMock manager and awaits the tool
# function directly — see bridge/tests/test_mcp_tools.py:44-51. Copy that shape.
from unittest.mock import patch


def _session_data():
    return {"goal": "g", "status": "active", "plan": "- [ ] one",
            "decisions": [{"timestamp": "2026-07-30T10:00:00.000001+00:00",
                           "content": "chose A"}],
            "progress": [], "files": {}, "scratch": {}, "proactive_memories": []}


def _mgr(epoch=""):
    mgr = AsyncMock()
    mgr.get_active_session_id = AsyncMock(return_value="sess-1")
    mgr.get_session_data = AsyncMock(return_value=_session_data())
    mgr.get_shadow_epoch = AsyncMock(return_value=epoch)
    return mgr


class TestShadowDelta:
    @pytest.mark.asyncio
    async def test_full_restore_returns_a_cursor_and_is_not_a_delta(self):
        from app.mcp_server import ctx_get_shadow
        with patch("app.mcp_server._get_manager", return_value=_mgr()):
            out = await ctx_get_shadow(agent_id="a")
        assert out["delta"] is False
        assert out["shadow_cursor"]
        assert "### Decisions" in out["shadow"]

    @pytest.mark.asyncio
    async def test_a_fresh_cursor_yields_a_delta_that_names_what_it_withheld(self):
        from app.mcp_server import ctx_get_shadow
        with patch("app.mcp_server._get_manager", return_value=_mgr()):
            first = await ctx_get_shadow(agent_id="a")
            second = await ctx_get_shadow(agent_id="a", since=first["shadow_cursor"])
        assert second["delta"] is True
        assert "still exist" in second["note"]
        assert "ctx_get_shadow()" in second["note"]

    @pytest.mark.asyncio
    async def test_every_bad_cursor_yields_a_full_restore(self):
        """The tool-level half of the fail-safe matrix. The pure-function half lives
        in tests/test_residency.py; both must hold."""
        from app.mcp_server import ctx_get_shadow
        from app.shadow import assemble_shadow
        for bad in (None, "", "garbage", "eyJ2IjoxfQ"):
            with patch("app.mcp_server._get_manager", return_value=_mgr()):
                out = await ctx_get_shadow(agent_id="a", since=bad)
            assert out["delta"] is False, f"cursor {bad!r} produced a delta"
            # AMENDED 2026-07-30 (review round 1, Minor): delta=False alone doesn't
            # prove the DOCUMENT is actually complete — assert it's the same
            # reference output a full restore always produces.
            assert out["shadow"] == assemble_shadow(_session_data()), \
                f"cursor {bad!r} produced a non-full document"

    @pytest.mark.asyncio
    async def test_a_cursor_is_refused_after_the_epoch_is_bumped(self):
        """precompact's server-side belt: the agent wrongly passes a stale cursor
        after a compaction, and Bridge answers with everything anyway."""
        from app.mcp_server import ctx_get_shadow
        with patch("app.mcp_server._get_manager", return_value=_mgr(epoch="1000")):
            first = await ctx_get_shadow(agent_id="a")
        with patch("app.mcp_server._get_manager", return_value=_mgr(epoch="9999")):
            out = await ctx_get_shadow(agent_id="a", since=first["shadow_cursor"])
        assert out["delta"] is False

    @pytest.mark.asyncio
    async def test_ctx_resume_session_never_returns_a_delta(self):
        """A resume is by definition a context the agent cannot vouch for. It takes
        no `since` and must always be full — the signature is also load-bearing:
        ctx_resume_session has no such parameter and FastMCP would reject the kwarg."""
        import inspect
        from app.mcp_server import ctx_resume_session
        assert "since" not in inspect.signature(ctx_resume_session).parameters

    @pytest.mark.asyncio
    async def test_ctx_resume_session_mints_a_cursor_but_never_a_delta_key(self):
        """AMENDED 2026-07-30 (review round 1): the signature-only test above cannot
        catch a regression in the resume BODY — deleting the cursor-minting lines
        entirely, or adding an always-false `"delta": False` (the exact anti-pattern
        the brief called out: "an always-false flag invites someone to start passing
        `since`"), both leave the suite green without this test."""
        from app.mcp_server import ctx_resume_session
        mgr = AsyncMock()
        mgr.resume_session = AsyncMock(return_value=None)
        mgr.get_session_data = AsyncMock(return_value=_session_data())
        mgr.get_shadow_epoch = AsyncMock(return_value="")
        with patch("app.mcp_server._get_manager", return_value=mgr):
            out = await ctx_resume_session("sess-1", agent_id="a")
        assert out["shadow_cursor"]
        assert "delta" not in out

    @pytest.mark.asyncio
    async def test_a_delta_document_never_denies_that_withheld_content_exists(self):
        """The C1 regression test, at the layer C1 actually lived in.

        AMENDED 2026-07-30: the original version of this test asserted a blanket
        "none of these four placeholder strings may ever appear in a delta document".
        That is over-broad — C1 is "never say *No files tracked* WHEN FILES WERE
        WITHHELD", not "never say it at all". The shared `_session_data()` fixture
        has empty `files`/`progress` from the start, so those placeholders were
        simply TRUE there (nothing was ever omitted from an already-empty section),
        and asserting their absence was asserting the wrong thing. This local
        fixture instead gives every filterable section (decisions, progress, files,
        plan) a genuinely older entry that the high-water filter actually withholds.

        AMENDED again 2026-07-30 (review round 1): with every section retaining its
        newest entry, `out["decisions"]`/`["progress"]`/`["files"]` were never fully
        empty — only PARTIALLY filtered — so the three per-section denial assertions
        below could not actually fail: `assemble_shadow` only ever prints the "*No
        X*" placeholder for a section that has NOTHING left after filtering (see
        shadow.py's `elif not decisions` / `elif not files` / `elif not progress`
        branches), and a partially-filtered section always has something left. Only
        the plan branch (which zeroes to `""` on an unchanged plan) was actually
        exercising the C1 path. `decisions` here is trimmed to ONE entry older than
        the high-water mark set by `files`' newest entry — so the whole section is
        dropped (`out["decisions"] == []`), which is the shape that actually reaches
        the placeholder branch and makes the assertion load-bearing again.
        """
        from app.mcp_server import ctx_get_shadow

        def _rich_session_data():
            return {
                "goal": "g", "status": "active", "plan": "- [ ] one",
                # Fully withheld: its only entry predates the high-water mark set
                # below by files' newest entry, so the whole section is dropped.
                "decisions": [
                    {"timestamp": "2026-07-30T09:00:00.000001+00:00", "content": "old decision"},
                ],
                # Partially withheld: one entry survives — covers the "some kept,
                # some omitted" shape the fully-withheld decisions case does not.
                "progress": [
                    {"timestamp": "2026-07-30T09:00:00.000001+00:00", "content": "old progress"},
                    {"timestamp": "2026-07-30T10:00:00.000001+00:00", "content": "new progress"},
                ],
                "files": {
                    "old.py": {"summary": "stale", "last_action": "2026-07-30T09:00:00.000001+00:00"},
                    "new.py": {"summary": "fresh", "last_action": "2026-07-30T10:00:00.000001+00:00"},
                },
                "scratch": {}, "proactive_memories": [],
            }

        def _rich_mgr():
            mgr = AsyncMock()
            mgr.get_active_session_id = AsyncMock(return_value="sess-1")
            mgr.get_session_data = AsyncMock(return_value=_rich_session_data())
            mgr.get_shadow_epoch = AsyncMock(return_value="")
            return mgr

        with patch("app.mcp_server._get_manager", return_value=_rich_mgr()):
            first = await ctx_get_shadow(agent_id="a")
            second = await ctx_get_shadow(agent_id="a", since=first["shadow_cursor"])
        doc = second["shadow"]

        # The pure function's own omission report is the ground truth for which
        # sections actually withheld something in THIS fixture — asserting against
        # it (rather than all four unconditionally) is what keeps this test honest
        # about what C1 claims versus what it doesn't.
        rendered, omitted_report = residency.filter_since(
            _rich_session_data(), first["shadow_cursor"], session_id="sess-1", epoch="")
        assert omitted_report["decisions"] and omitted_report["progress"] and omitted_report["files"]
        assert omitted_report["plan"]
        # The load-bearing shape: decisions is FULLY withheld (nothing survives the
        # filter), which is what actually reaches assemble_shadow's placeholder
        # branch. Without this, the denial assertions below could not fail even if
        # `omitted=omitted` were dropped from the ctx_get_shadow implementation.
        assert rendered["decisions"] == []

        for key, denial in (("decisions", "No decisions recorded"),
                            ("progress", "No progress logged"),
                            ("files", "No files tracked")):
            assert omitted_report[key], f"fixture bug: {key} was not actually omitted"
            assert denial not in doc, f"{key}: denied withheld content exists"
        assert "No plan set" not in doc, "plan: denied withheld content exists"
        assert "omitted" in doc.lower(), "document does not disclose that content was withheld"
        assert "ctx_get_shadow()" in doc, "document does not say how to recover the full set"

    @pytest.mark.asyncio
    async def test_a_full_restore_document_is_byte_identical_to_the_pre_change_output(self):
        """The no-regression half: with no cursor, the document must be exactly what
        callers got before this task existed. assemble_shadow(data) with omitted=None
        is the reference."""
        from app.mcp_server import ctx_get_shadow
        from app.shadow import assemble_shadow
        with patch("app.mcp_server._get_manager", return_value=_mgr()):
            out = await ctx_get_shadow(agent_id="a")
        assert out["shadow"] == assemble_shadow(_session_data())

    @pytest.mark.asyncio
    async def test_a_failed_epoch_read_mints_no_cursor(self):
        """A response carrying a cursor could seed a later delta on a session whose
        epoch was never readable — so on a failed epoch read there must be no
        shadow_cursor key at all, not even an empty one."""
        from app.mcp_server import ctx_get_shadow
        with patch("app.mcp_server._get_manager", return_value=_mgr(epoch=None)):
            out = await ctx_get_shadow(agent_id="a")
        assert "shadow_cursor" not in out
        assert out["delta"] is False
