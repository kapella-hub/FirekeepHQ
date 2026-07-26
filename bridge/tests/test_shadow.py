"""Tests for shadow context assembly."""

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
