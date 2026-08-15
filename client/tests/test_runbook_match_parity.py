"""Enforced Runbooks Phase B — the PINNED normalization + matching rules.

Spec (2026-08-15, "Wire contract"): a command-kind StepSpec `pattern` is a
bounded glob "matched against the whitespace-normalized command string".

THIS FILE IS THE CLIENT'S HALF OF A TWO-SIDED PIN. The client decides WHETHER
to escalate with `runbooks.match_entries`; the server decides WHAT the verdict
is with its own matcher (cortex/app/procedures/match.py, Phase A). If the two
normalize differently, a command silently skips its runbook — so the exact
rules are frozen here, as literal input/output pairs, for the server suite to
mirror:

  1. Whitespace normalization = `" ".join(command.split())`: every run of
     whitespace (space, tab, newline, CR, FF, VT) collapses to ONE space, and
     the ends are stripped. Applied to BOTH the command and the pattern.
  2. Glob = fnmatch semantics (`*`, `?`, `[seq]`), matched CASE-SENSITIVELY on
     every platform (`fnmatch.fnmatchcase`; plain `fnmatch.fnmatch` would go
     case-insensitive on Windows while the Linux server stays sensitive).
  3. Empty command or empty pattern never matches. A malformed entry is
     skipped, never raised on — this runs on the blocking pre-tool path.
"""
from __future__ import annotations

import pytest

from firekeep_client.hooks import runbooks


class TestNormalizeCommand:
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
        assert runbooks.normalize_command(raw) == expected

    def test_never_raises_on_non_string(self):
        assert runbooks.normalize_command(None) == ""  # REFUSED, server parity:
        # str(None) is the matchable four-character command "None" (review
        # 2026-08-15 — the client must not escalate what the server refuses)
        assert runbooks.normalize_command(12) == ""
        long = "x" * 5000
        assert runbooks.normalize_command(long) == "x" * 4096  # server bound


def _entries(*patterns, mode="advise"):
    return [{"skill_id": "deploy-vps", "step_id": f"s{i}", "pattern": p,
             "mode": mode} for i, p in enumerate(patterns)]


class TestMatchEntries:
    def test_glob_star_matches_suffix(self):
        assert runbooks.match_entries(_entries("git push*"),
                                      "git push --force-with-lease")

    def test_star_matches_empty(self):
        assert runbooks.match_entries(_entries("docker compose up -d*"),
                                      "docker compose up -d")

    def test_command_whitespace_is_normalized_before_matching(self):
        assert runbooks.match_entries(_entries("git push --force"),
                                      "git    push \t --force")

    def test_pattern_whitespace_is_normalized_too(self):
        # An authored pattern with a double space must not silently never-match.
        assert runbooks.match_entries(_entries("git  push*"), "git push --tags")

    def test_case_sensitive_on_every_platform(self):
        """THE Windows landmine: fnmatch.fnmatch would normcase both sides on
        win32 and make this match, while the Linux server would not — the
        client must use fnmatchcase so both sides answer identically."""
        assert runbooks.match_entries(_entries("git push*"), "GIT PUSH --force") == []
        assert runbooks.match_entries(_entries("Git Push*"), "git push") == []

    def test_question_mark_and_char_class(self):
        assert runbooks.match_entries(_entries("git push -?"), "git push -f")
        assert runbooks.match_entries(_entries("rm -r[fF] build"), "rm -rf build")
        assert runbooks.match_entries(_entries("rm -r[fF] build"), "rm -rx build") == []

    def test_no_match_returns_empty(self):
        assert runbooks.match_entries(_entries("git push*"), "ls -la") == []

    def test_empty_command_never_matches(self):
        assert runbooks.match_entries(_entries("*"), "") == []
        assert runbooks.match_entries(_entries("*"), "   \n ") == []

    def test_empty_pattern_never_matches(self):
        assert runbooks.match_entries(_entries(""), "git push") == []

    def test_multiple_entries_all_matches_returned(self):
        entries = _entries("git push*", "git *", "docker*")
        got = runbooks.match_entries(entries, "git push origin main")
        assert [e["pattern"] for e in got] == ["git push*", "git *"]

    def test_malformed_entries_are_skipped_not_raised(self):
        entries = [
            "not a dict",
            42,
            None,
            {"pattern": None},
            {"pattern": 123},
            {"no_pattern_key": True},
            {"pattern": "["},          # unclosed class: fnmatch treats literally
            {"pattern": "git push*", "skill_id": "ok"},
        ]
        got = runbooks.match_entries(entries, "git push")
        assert [e.get("skill_id") for e in got] == ["ok"]

    def test_non_list_entries_is_no_match(self):
        assert runbooks.match_entries(None, "git push") == []
        assert runbooks.match_entries("junk", "git push") == []

    def test_non_command_kind_entries_are_skipped(self):
        entries = [{"pattern": "*", "kind": "file"},
                   {"pattern": "git push*", "kind": "command", "skill_id": "ok"}]
        got = runbooks.match_entries(entries, "git push")
        assert [e.get("skill_id") for e in got] == ["ok"]
