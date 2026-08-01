"""Tests for shadow context assembly."""

import pytest

from app.shadow import assemble_shadow


class TestAssembleShadow:
    def test_full_shadow(self):
        data = {
            "goal": "implement feature X",
            "status": "active",
            "created_at": "2026-03-14T02:00:00Z",
            "updated_at": "2026-03-14T02:15:00Z",
            "plan": "- [x] Step 1\n- [ ] Step 2",
            "decisions": [
                {"timestamp": "2026-03-14T02:05:00Z", "content": "chose approach A"},
            ],
            "files": {
                "app/main.py": {"summary": "added endpoint", "last_action": "2026-03-14T02:10:00Z"},
            },
            "progress": [
                {"timestamp": "2026-03-14T02:06:00Z", "content": "step 1 done"},
            ],
            "scratch": {"key1": "value1"},
        }
        result = assemble_shadow(data)
        assert "## Session: implement feature X" in result
        assert "### Plan" in result
        assert "- [x] Step 1" in result
        assert "### Decisions" in result
        assert "chose approach A" in result
        assert "### Files Known" in result
        assert "**app/main.py**" in result
        assert "### Progress" in result
        assert "step 1 done" in result
        assert "### Scratchpad" in result
        assert "key1: value1" in result

    def test_empty_components(self):
        data = {
            "goal": "test",
            "status": "active",
            "created_at": "2026-03-14T00:00:00Z",
            "updated_at": "2026-03-14T00:00:00Z",
            "plan": "",
            "decisions": [],
            "files": {},
            "progress": [],
            "scratch": {},
        }
        result = assemble_shadow(data)
        assert "## Session: test" in result
        assert "No plan set" in result

    def test_section_order(self):
        data = {
            "goal": "test", "status": "active",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            "plan": "step", "decisions": [{"timestamp": "t", "content": "d"}],
            "files": {"f": {"summary": "s", "last_action": "t"}},
            "progress": [{"timestamp": "t", "content": "p"}],
            "scratch": {"k": "v"},
        }
        result = assemble_shadow(data)
        plan_pos = result.index("### Plan")
        dec_pos = result.index("### Decisions")
        files_pos = result.index("### Files Known")
        prog_pos = result.index("### Progress")
        scratch_pos = result.index("### Scratchpad")
        assert plan_pos < dec_pos < files_pos < prog_pos < scratch_pos


class TestAssembleShadowIsTotal:
    """assemble_shadow is the FLOOR of the post-compaction lifeline (M1).

    `ctx_get_shadow` answers every doubtful path with `assemble_shadow(data)` — the
    full, unfiltered document. A fallback that can itself raise is not a floor, so
    this renderer must be total: no session shape may turn it into an exception.
    """

    # Every container and every entry position, each given a shape the well-formed
    # renderer would have crashed on: `.items()` on a list, `.get()` on a string,
    # `len()` on an int, `format(..., '.2f')` on a string, `join()` on a dict.
    HOSTILE = [
        {"files": [{"path": "a.py"}]},
        {"files": "not-a-mapping"},
        {"files": {"a.py": "summary-as-string"}},
        {"files": {1: {"summary": "s"}, "a": {"summary": "s"}}},   # unorderable keys
        {"decisions": {"d1": {"content": "x"}}},
        {"decisions": ["bare string"]},
        {"decisions": [{"timestamp": 1700000000, "content": "x"}]},
        {"decisions": [{"timestamp": None, "content": "x"}]},
        {"decisions": 7},
        {"progress": {"p1": {"content": "x"}}},
        {"progress": ["bare string"]},
        {"progress": [{"timestamp": 1700000000, "content": "x"}]},
        {"scratch": ["not-a-mapping"]},
        {"scratch": {1: "a", "b": "c"}},                            # unorderable keys
        {"plan": {"not": "a string"}},
        {"proactive_memories": {"m1": {"score": 0.5}}},
        {"proactive_memories": [{"score": "high", "content": "x"}]},
        {"proactive_memories": ["bare string"]},
    ]

    @pytest.mark.parametrize("override", HOSTILE)
    def test_no_session_shape_can_make_the_renderer_raise(self, override):
        data = {"goal": "g", "status": "active", "plan": "p",
                "decisions": [], "files": {}, "progress": [], "scratch": {},
                "proactive_memories": [], **override}
        result = assemble_shadow(data)
        assert isinstance(result, str)
        assert "## Session: g" in result

    @pytest.mark.parametrize("override", HOSTILE)
    def test_a_malformed_container_is_shown_rather_than_silently_dropped(self, override):
        """Totality must not be bought with silence. Iterating a dict-shaped section
        would yield its KEYS and discard every value; rendering nothing at all would
        be the affirmative denial ('*No decisions recorded*') this module exists to
        prevent. The unrecognised value is rendered literally instead."""
        (key, value), = override.items()
        data = {"goal": "g", "status": "active", "plan": "p",
                "decisions": [], "files": {}, "progress": [], "scratch": {},
                "proactive_memories": [], **override}
        result = assemble_shadow(data)
        denial = {"decisions": "*No decisions recorded*", "files": "*No files tracked*",
                  "progress": "*No progress logged*", "scratch": "*Empty*",
                  "plan": "*No plan set*"}.get(key)
        if denial:
            assert denial not in result, f"{key}: denied that its content exists"

    def test_well_formed_data_renders_byte_identically(self):
        """The no-regression half: defensiveness may not change one byte of the
        document a healthy session produces."""
        data = {
            "goal": "implement feature X", "status": "active",
            "created_at": "2026-03-14T02:00:00Z", "updated_at": "2026-03-14T02:15:00Z",
            "plan": "- [x] Step 1\n- [ ] Step 2",
            "decisions": [{"timestamp": "2026-03-14T02:05:00Z", "content": "chose A"}],
            "files": {"z.py": {"summary": "second", "last_action": "t"},
                      "a.py": {"summary": "first", "last_action": "t"}},
            "progress": [{"timestamp": "2026-03-14T02:06:00Z", "content": "step 1 done"}],
            "scratch": {"z": "last", "a": "first"},
            "proactive_memories": [{"score": 0.5, "content": "recalled"}],
        }
        assert assemble_shadow(data) == "\n".join([
            "## Session: implement feature X",
            "**Status**: active | **Started**: 2026-03-14T02:00:00Z | "
            "**Updated**: 2026-03-14T02:15:00Z",
            "",
            "### Plan",
            "- [x] Step 1\n- [ ] Step 2",
            "",
            "### Decisions",
            "- [02:05] chose A",
            "",
            "### Files Known",
            "- **a.py** — first",
            "- **z.py** — second",
            "",
            "### Progress",
            "- [02:06] step 1 done",
            "",
            "### Scratchpad",
            "- a: first",
            "- z: last",
            "",
            "### Relevant Past Experience",
            "- [0.50] recalled",
        ])
