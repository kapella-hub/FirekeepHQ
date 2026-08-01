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
    #
    # Each case pairs the hostile override with a UNIQUE fragment of the content it
    # carries. The fragment is what makes the sibling test below discriminating: an
    # absent denial only proves a row was emitted, and a row emitted from a shape the
    # renderer half-recognised (`- [] `, `- [0.00] `, `- **a.py** — `) is an empty row
    # that satisfies "not denied" while having discarded every value. Assert the value
    # is THERE, not merely that nothing claimed it was absent.
    HOSTILE = [
        ({"files": [{"path": "list-shaped-files"}]}, "list-shaped-files"),
        ({"files": "files-not-a-mapping"}, "files-not-a-mapping"),
        # Retained deliberately: the pre-existing `str(info)` branch already preserved
        # this, so it guards no NEW behaviour on the revert path -- but under the
        # presence assertion below it is no longer inert, because it now pins that
        # branch's content preservation against a future change that drops it.
        ({"files": {"a.py": "summary-as-string"}}, "summary-as-string"),
        # The fourth emptying site, alongside the two `_stamped_line` sections and
        # proactive_memories: `info` IS a dict, so the `str(info)` branch above is
        # skipped, and `info.get("summary", "")` then renders `- **a.py** — `.
        ({"files": {"a.py": {"note": "info-carrying-no-summary"}}},
         "info-carrying-no-summary"),
        ({"files": {1: {"summary": "unorderable-file-key"},                # unorderable
                    "a": {"summary": "str-file-key"}}}, "unorderable-file-key"),
        ({"decisions": {"d1": {"content": "dict-shaped-decision"}}},
         "dict-shaped-decision"),
        ({"decisions": ["bare-decision-string"]}, "bare-decision-string"),
        ({"decisions": [{"timestamp": 1700000000, "content": "int-stamped-decision"}]},
         "int-stamped-decision"),
        ({"decisions": [{"timestamp": None, "content": "none-stamped-decision"}]},
         "none-stamped-decision"),
        ({"decisions": 1234567}, "1234567"),
        ({"progress": {"p1": {"content": "dict-shaped-progress"}}},
         "dict-shaped-progress"),
        ({"progress": ["bare-progress-string"]}, "bare-progress-string"),
        ({"progress": [{"timestamp": 1700000000, "content": "int-stamped-progress"}]},
         "int-stamped-progress"),
        ({"scratch": ["scratch-not-a-mapping"]}, "scratch-not-a-mapping"),
        ({"scratch": {1: "unorderable-scratch-value", "b": "c"}},          # unorderable
         "unorderable-scratch-value"),
        ({"plan": {"not": "plan-as-a-dict"}}, "plan-as-a-dict"),
        ({"proactive_memories": {"m1": {"score": 0.5,
                                        "content": "dict-shaped-memory"}}},
         "dict-shaped-memory"),
        ({"proactive_memories": [{"score": "high", "content": "unformattable-score"}]},
         "unformattable-score"),
        ({"proactive_memories": ["bare-memory-string"]}, "bare-memory-string"),
    ]

    @pytest.mark.parametrize("override,fragment", HOSTILE)
    def test_no_session_shape_can_make_the_renderer_raise(self, override, fragment):
        data = {"goal": "g", "status": "active", "plan": "p",
                "decisions": [], "files": {}, "progress": [], "scratch": {},
                "proactive_memories": [], **override}
        result = assemble_shadow(data)
        assert isinstance(result, str)
        assert "## Session: g" in result

    @pytest.mark.parametrize("override,fragment", HOSTILE)
    def test_a_malformed_container_is_shown_rather_than_silently_dropped(
            self, override, fragment):
        """Totality must not be bought with silence. Iterating a dict-shaped section
        would yield its KEYS and discard every value; rendering nothing at all would
        be the affirmative denial ('*No decisions recorded*') this module exists to
        prevent. The unrecognised value is rendered literally instead.

        Both halves are asserted, and the second is the load-bearing one. Asserting
        only that the denial is absent passes on a row like `- [] ` -- emitted, and
        empty. `ctx_get_shadow` is the post-compaction lifeline; a document that
        neither denies the content nor contains it is the same loss as a denial, just
        harder to see."""
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
        assert fragment in result, (
            f"{key}: rendered a row but discarded the content {fragment!r} -- "
            f"got {[ln for ln in result.splitlines() if ln.startswith('- ')]}"
        )

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
