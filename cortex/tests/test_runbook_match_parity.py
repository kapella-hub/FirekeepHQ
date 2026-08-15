"""Enforced Runbooks Phase A — the server's half of the TWO-SIDED matching pin.

The client decides WHETHER to escalate (client/firekeep_client/hooks/runbooks
.py, pinned in client/tests/test_runbook_match_parity.py); the server decides
WHAT the verdict is (app/procedures/match.py). If the two normalize
differently, a command silently skips its runbook — so the client suite froze
the rules as literal input/output pairs, and THIS FILE MIRRORS THOSE EXACT
PAIRS so both suites pin the same truth:

  1. Whitespace normalization = `" ".join(command.split())`: every run of
     whitespace (space, tab, newline, CR, FF, VT) collapses to ONE space, and
     the ends are stripped. Applied to BOTH the command and the pattern.
  2. Glob = fnmatch semantics (`*`, `?`, `[seq]`), matched CASE-SENSITIVELY on
     every platform (`fnmatch.fnmatchcase`; plain `fnmatch.fnmatch` would go
     case-insensitive on Windows while the Linux server stays sensitive).
  3. Empty command or empty pattern never matches. A malformed entry is
     skipped, never raised on — this runs on the blocking pre-tool path.

DOCUMENTED DIVERGENCES, all deliberate and all out-of-wire: (1) entries with
NO `kind` are commands to the client (its bundle holds only command entries)
but file_glob to the server (its index mixes round-1 file entries that predate
the field) — the server always STAMPS `kind: "command"` on the entries it
indexes and serves, so no wire entry ever relies on the default; pinned at the
bottom of this file. (2) non-string commands — see
test_never_raises_on_non_string.

The server additionally bounds the command it will look at (4096 chars after
normalization, match._MAX_COMMAND_CHARS) because `Action.target` has no
max_length and a hostile multi-megabyte command must not buy regex time on the
blocking path. Commands longer than that are out of parity by construction —
disclosed, not hidden.
"""
from __future__ import annotations

import pytest

from app.procedures import match


class TestNormalizeCommandPinnedPairs:
    @pytest.mark.parametrize("raw,expected", [
        ("git push", "git push"),
        ("  git   push \t --force\n", "git push --force"),
        ("git\npush", "git push"),
        ("git\r\npush\t\t--tags", "git push --tags"),
        ("\tdocker  compose\v up\f -d ", "docker compose up -d"),
        ("", ""),
        ("   \n\t  ", ""),
    ])
    def test_pinned_normalization_pairs(self, raw, expected):
        assert match.normalize_command(raw) == expected

    def test_never_raises_on_non_string(self):
        """SECOND documented divergence, and it is out-of-wire by construction:
        the client str()-coerces a non-string (its pin: None -> "None"), the
        server refuses it outright ("" -> no match) because `str(None)` is the
        four-character command "None" and an `N*` pattern would match it. A
        non-string can never reach the server matcher through the product path
        — `Action.target` is a pydantic `str` — so the two sides can only
        disagree on input the wire cannot carry, and both fail OPEN on it."""
        assert match.normalize_command(None) == ""
        assert match.normalize_command(42) == ""


def _entries(*patterns):
    """Command-kind index entries, one per pattern — the server's shape for
    what the client calls a bundle entry."""
    return [{"skill_id": "deploy-vps", "step_id": f"s{i}", "kind": "command",
             "pattern": p, "load_bearing": False, "order": i}
            for i, p in enumerate(patterns)]


class TestMatchCommandPinnedPairs:
    def test_glob_star_matches_suffix(self):
        assert match.match_command(_entries("git push*"),
                                   "git push --force-with-lease")

    def test_star_matches_empty(self):
        assert match.match_command(_entries("docker compose up -d*"),
                                   "docker compose up -d")

    def test_command_whitespace_is_normalized_before_matching(self):
        assert match.match_command(_entries("git push --force"),
                                   "git    push \t --force")

    def test_pattern_whitespace_is_normalized_too(self):
        # An authored pattern with a double space must not silently never-match.
        assert match.match_command(_entries("git  push*"), "git push --tags")

    def test_case_sensitive_on_every_platform(self):
        """THE Windows landmine: fnmatch.fnmatch would normcase both sides on
        win32 and make this match, while the Linux server would not — both
        sides must use fnmatchcase so they answer identically."""
        assert match.match_command(_entries("git push*"), "GIT PUSH --force") == []
        assert match.match_command(_entries("Git Push*"), "git push") == []

    def test_question_mark_and_char_class(self):
        assert match.match_command(_entries("git push -?"), "git push -f")
        assert match.match_command(_entries("rm -r[fF] build"), "rm -rf build")
        assert match.match_command(_entries("rm -r[fF] build"), "rm -rx build") == []

    def test_no_match_returns_empty(self):
        assert match.match_command(_entries("git push*"), "ls -la") == []

    def test_empty_command_never_matches(self):
        assert match.match_command(_entries("*"), "") == []
        assert match.match_command(_entries("*"), "   \n ") == []

    def test_empty_pattern_never_matches(self):
        assert match.match_command(_entries(""), "git push") == []

    def test_multiple_entries_all_matches_returned(self):
        entries = _entries("git push*", "git *", "docker*")
        got = match.match_command(entries, "git push origin main")
        assert [e["pattern"] for e in got] == ["git push*", "git *"]

    def test_malformed_entries_are_skipped_not_raised(self):
        entries = [
            "not a dict",
            42,
            None,
            {"kind": "command", "pattern": None},
            {"kind": "command", "pattern": 123},
            {"kind": "command", "no_pattern_key": True},
            {"kind": "command", "pattern": "["},  # unclosed class: literal
            {"kind": "command", "pattern": "git push*", "skill_id": "ok"},
        ]
        got = match.match_command(entries, "git push")
        assert [e.get("skill_id") for e in got] == ["ok"]

    def test_non_command_kind_entries_are_skipped(self):
        entries = [{"pattern": "*", "kind": "file_glob"},
                   {"pattern": "git push*", "kind": "command",
                    "skill_id": "ok"}]
        got = match.match_command(entries, "git push")
        assert [e.get("skill_id") for e in got] == ["ok"]


class TestTheDocumentedKindDefaultDivergence:
    def test_a_kindless_entry_is_file_glob_to_the_server(self):
        """Round-1 index entries carry no `kind` and were always file globs;
        the server must not start reading them as commands. The wire never
        depends on this default — index_entries and the bundle both stamp
        `kind` explicitly (pinned in test_runbook_matching / bundle tests)."""
        kindless = [{"skill_id": "s1", "step_id": "a", "pattern": "git push*",
                     "load_bearing": False, "order": 0}]
        assert match.match_command(kindless, "git push --force") == []
        # ...and a kindless entry with a path glob still matches as the file
        # entry it always was.
        kindless_path = [{"skill_id": "s1", "step_id": "a", "pattern": "*.lock",
                          "load_bearing": False, "order": 0}]
        assert match.match_target(kindless_path, "poetry.lock")
