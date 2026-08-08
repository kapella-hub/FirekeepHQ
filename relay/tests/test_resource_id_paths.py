"""`resource_id` must accept the paths the pre-edit hook actually sends.

WHY THIS EXISTS. `_normalize_resource_id` validated against `_validate_name`'s
charset — `[a-zA-Z0-9._-]` — which is right for a channel or an agent id and
wrong for a FILE PATH. Two consequences, both proven live:

  * every Windows absolute path was rejected, because of the drive-letter
    colon: `relay_lease(resource_id="E:\\Documents\\...\\main.py")` returned
    `{"error": "Invalid resource_id: ...", "status": "unavailable"}`;
  * so was any path containing a space, on every OS.

The consumer is what makes that fatal rather than merely inconvenient.
`client/firekeep_client/hooks/pre_tool.py` computes its resource_id as
`os.path.normpath(file_path).replace("\\\\", "/")`, i.e. exactly the string
above, and then tests `lease.get("held")`. `"held"` is absent from an error
dict, so the test was falsy, the edit was allowed, no warning printed and no
failure logged — indistinguishable from "nobody holds this file". The lease
coordination gate therefore could not fire at all on Windows.

These tests pin the charset at the boundary the hook uses, not at
`_validate_name`, which must stay narrow for channel/agent names.
"""

from __future__ import annotations

import pytest

from app.mcp_server import _normalize_resource_id, _validate_name


class TestResourceIdAcceptsRealPaths:
    def test_windows_absolute_path(self):
        """The exact string pre_tool._resource_id produces on Windows."""
        assert _normalize_resource_id(
            "E:\\Documents\\Projects\\Firekeep\\cortex\\app\\main.py"
        ) == "E:.Documents.Projects.Firekeep.cortex.app.main.py"

    def test_path_containing_a_space(self):
        """Not Windows-specific — 'docs/my notes.md' failed on every OS."""
        assert _normalize_resource_id("docs/my notes.md") == "docs.my notes.md"

    def test_posix_absolute_path_still_works(self):
        """No regression on the shape that always worked."""
        assert _normalize_resource_id("/srv/app/main.py") == "srv.app.main.py"


class TestResourceIdStillRefusesTheDangerousShapes:
    def test_path_traversal_still_refused(self):
        with pytest.raises(ValueError, match="path traversal"):
            _normalize_resource_id("../../etc/passwd")

    def test_empty_still_refused(self):
        with pytest.raises(ValueError, match="must not be empty"):
            _normalize_resource_id("   ")

    def test_over_length_still_refused(self):
        with pytest.raises(ValueError, match="Invalid resource_id"):
            _normalize_resource_id("a" * 201)

    @pytest.mark.parametrize("bad", ["a\nb", "a\x00b", "a{b}", "a*b", "a?b"])
    def test_control_and_glob_characters_still_refused(self, bad):
        """Widening the charset must not become 'anything goes'."""
        with pytest.raises(ValueError, match="Invalid resource_id"):
            _normalize_resource_id(bad)


class TestNameValidationUnchanged:
    def test_channel_and_agent_names_stay_narrow(self):
        """The widened charset is scoped to resource_id ONLY.

        A channel or agent id has no reason to contain a colon or a space, and
        widening `_validate_name` would have loosened every one of its ~15
        other call sites for a defect that belongs to one of them.
        """
        with pytest.raises(ValueError, match="Invalid channel"):
            _validate_name("has spaces", "channel")
        with pytest.raises(ValueError, match="Invalid agent_id"):
            _validate_name("agent:1", "agent_id")
