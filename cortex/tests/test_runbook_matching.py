"""Enforced Runbooks Phase A — the server matcher's own guarantees.

The parity file pins what client and server must agree on; this file pins what
only the server has to survive: hostile inputs on the blocking pre-tool path
(cannot raise, cannot buy unbounded regex time), the command/file split inside
one mixed index, the StepSpec model rules for kind="command", and the index
build that stamps `kind` so nothing downstream depends on a default.
"""
from __future__ import annotations

import time

import pytest
import fakeredis.aioredis as fr
from pydantic import ValidationError

from app.procedures import store
from app.procedures import match
from app.procedures.models import (
    MAX_PATTERN_CHARS,
    StepSpec,
    merge_step_specs,
)


def _cmd(pattern, skill="s1", step="a", order=0, lb=False):
    return {"skill_id": skill, "skill_trigger": "t", "step_id": step,
            "step_text": step, "kind": "command", "pattern": pattern,
            "load_bearing": lb, "order": order}


class TestHostileInputsCannotRaiseOrHang:
    def test_hostile_patterns_never_raise(self):
        for bad in ["[", "**[", "\\", "a" * 5000, "../../*", "(?i)x",
                    None, 123, ["*.py"], {"pattern": "*.py"}]:
            assert match.match_command([_cmd(bad)], "anything at all") == []

    def test_a_non_dict_entry_never_raises(self):
        assert match.match_command(["junk", 42, None], "git push") == []

    def test_a_non_string_command_never_raises(self):
        for bad in [None, 123, ["git", "push"], {"cmd": "git push"}]:
            # str() coercion is the pinned behaviour; no raise either way.
            match.match_command([_cmd("git*")], bad)

    def test_an_overlong_pattern_is_refused_not_matched(self):
        """MAX_PATTERN_CHARS bounds the write path; the matcher additionally
        refuses long patterns at read time because the index is just JSON in
        Redis and anything can have been written into it."""
        assert match.match_command([_cmd("a" * 501 + "*")], "a" * 600) == []

    def test_a_multimegabyte_command_is_bounded(self):
        """`Action.target` has no max_length, and a translated glob with many
        stars can backtrack. The normalized command is capped at 4096 chars —
        the match must return promptly, not raise, and still work on the
        prefix it kept."""
        huge = "x " * 2_000_000  # ~4M chars of alternating token/space
        start = time.monotonic()
        got = match.match_command([_cmd("x x x*")], huge)
        elapsed = time.monotonic() - start
        assert got  # the pattern matches within the kept prefix
        assert elapsed < 5.0
        assert len(match.normalize_command(huge)) == 4096

    def test_normalize_command_never_raises(self):
        class Evil:
            def __str__(self):
                raise RuntimeError("boom")

        assert match.normalize_command(Evil()) == ""


class TestCommandAndFileEntriesStaySeparate:
    def test_match_target_skips_command_entries(self):
        """A command pattern like `git push*` must never be tried as a file
        glob — `*` crosses nothing in a command but everything in fnmatch, and
        a file edit named `git push --force` is not a thing."""
        idx = [_cmd("deploy *"),
               {"skill_id": "s1", "step_id": "f", "step_text": "f",
                "kind": "file_glob", "pattern": "deploy *",
                "load_bearing": False, "order": 1}]
        got = match.match_target(idx, "deploy prod")
        assert [e["step_id"] for e in got] == ["f"]

    def test_match_command_skips_file_entries(self):
        idx = [{"skill_id": "s1", "step_id": "f", "step_text": "f",
                "kind": "file_glob", "pattern": "*", "load_bearing": False,
                "order": 0},
               _cmd("git push*", step="c", order=1)]
        got = match.match_command(idx, "git push --tags")
        assert [e["step_id"] for e in got] == ["c"]

    def test_missing_load_bearing_spans_both_kinds(self):
        """A load-bearing COMMAND step gates a later file step and vice versa:
        'earlier step' is defined over the whole spec list, not per kind."""
        idx = [_cmd("bash backup.sh*", step="backup", order=0, lb=True),
               {"skill_id": "s1", "step_id": "conf", "step_text": "conf",
                "kind": "file_glob", "pattern": "*.conf",
                "load_bearing": True, "order": 1},
               _cmd("bash deploy.sh*", step="deploy", order=2)]
        missing = match.missing_load_bearing(idx, "s1", 2, set())
        assert [m["step_id"] for m in missing] == ["backup", "conf"]


class TestStepSpecCommandKind:
    def test_command_requires_a_non_empty_pattern(self):
        with pytest.raises(ValidationError):
            StepSpec(text="push it", kind="command", pattern="")
        with pytest.raises(ValidationError):
            StepSpec(text="push it", kind="command", pattern="   ")

    def test_command_keeps_its_pattern(self):
        s = StepSpec(text="push it", kind="command", pattern="git push*")
        assert s.pattern == "git push*"

    def test_unobservable_still_clears_a_pattern(self):
        s = StepSpec(text="ask a human", kind="unobservable", pattern="x")
        assert s.pattern == ""

    def test_command_pattern_shares_the_same_bound_as_file_glob(self):
        with pytest.raises(ValidationError):
            StepSpec(text="t", kind="command",
                     pattern="a" * (MAX_PATTERN_CHARS + 1))
        ok = StepSpec(text="t", kind="command",
                      pattern="a" * MAX_PATTERN_CHARS)
        assert len(ok.pattern) == MAX_PATTERN_CHARS

    def test_merge_step_specs_carries_command_ids_by_text(self):
        """merge_step_specs is untouched by round 2 — a command step's id must
        survive a wording-preserving rewrite exactly like a file step's."""
        old = [{"id": "keep", "text": "push the tag", "kind": "command",
                "pattern": "git push*", "load_bearing": True}]
        new = [StepSpec(text="push the tag", kind="command",
                        pattern="git push --tags*")]
        merged = merge_step_specs(new, old)
        assert merged[0]["id"] == "keep"


class TestIndexBuildStampsCommandEntries:
    @pytest.mark.asyncio
    async def test_command_specs_are_indexed_with_their_kind(self):
        r = fr.FakeRedis(decode_responses=True)

        class _Settings:
            PROCEDURE_MAX_SPECS = 50

        skills = [{
            "skill_id": "rb1", "trigger": "vps deploy", "content": "",
            "workspace_id": "workspace-local",
            "step_specs": [
                {"id": "backup", "text": "backup", "kind": "command",
                 "pattern": "bash backup.sh*", "load_bearing": True},
                {"id": "ask", "text": "ask", "kind": "unobservable",
                 "pattern": "", "load_bearing": False},
                {"id": "conf", "text": "conf", "kind": "file_glob",
                 "pattern": "*.conf", "load_bearing": False},
            ],
        }]
        n = await store.write_index(r, skills, _Settings())
        assert n == 2  # command + file_glob; unobservable stays unindexed
        idx = await store.load_index(r)
        by_id = {e["step_id"]: e for e in idx}
        assert by_id["backup"]["kind"] == "command"
        assert by_id["conf"]["kind"] == "file_glob"
        # Order is the FULL spec-list position: the unobservable step still
        # occupies its slot.
        assert by_id["backup"]["order"] == 0
        assert by_id["conf"]["order"] == 2
        # Coverage counts command steps as observable now.
        cov = await store.load_coverage(r)
        assert cov["rb1"]["observable"] == 2
        assert cov["rb1"]["spec_count"] == 3
