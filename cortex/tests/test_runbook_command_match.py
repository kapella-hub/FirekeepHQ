"""Enforced runbooks round 2 — pure command matching.

Same discipline as test_procedures_match: no I/O, and the functions here run
on the blocking pre-tool path, so "cannot raise" is a contract, not a hope.
"""
from __future__ import annotations

from app.procedures import match


def _c(skill="s1", step="a", pattern="git push*", order=0, load_bearing=False):
    return {"skill_id": skill, "skill_trigger": "trig", "step_id": step,
            "step_text": step, "kind": "command", "pattern": pattern,
            "load_bearing": load_bearing, "order": order, "workspace_id": ""}


def _f(skill="s1", step="a", pattern="*.py", order=0):
    # A round-1-shaped file entry: NO kind field at all.
    return {"skill_id": skill, "skill_trigger": "trig", "step_id": step,
            "step_text": step, "pattern": pattern, "load_bearing": False,
            "order": order}


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def test_whitespace_is_normalized_before_matching():
    """Newlines, tabs and runs of spaces collapse to single spaces, so the
    pattern matches however the agent wrapped the invocation."""
    idx = [_c(pattern="git push origin *")]
    assert match.match_command(idx, "git   push\n\torigin   main")
    assert match.match_command(idx, "  git push origin main  ")


def test_normalize_command_is_idempotent_and_bounded():
    n = match.normalize_command("git\tpush   origin\nmain")
    assert n == "git push origin main"
    assert match.normalize_command(n) == n
    long = "echo " + "a" * 100_000
    assert len(match.normalize_command(long)) <= match._MAX_COMMAND_CHARS


def test_a_non_string_command_is_refused_not_reprd():
    """str(None) is the four-character command "None" — a `N*` pattern would
    match it. Non-strings normalize to empty and match nothing."""
    for bad in (None, 123, ["git", "push"], {"cmd": "x"}):
        assert match.normalize_command(bad) == ""
        assert match.match_command([_c(pattern="*")], bad) == []


def test_matching_is_case_sensitive_on_every_platform():
    """fnmatchcase, not fnmatch: command text has no case-folding convention,
    and fnmatch folds on Windows because it normalises as a path."""
    idx = [_c(pattern="git push*")]
    assert match.match_command(idx, "git push origin")
    assert match.match_command(idx, "GIT PUSH origin") == []


# ---------------------------------------------------------------------------
# Kind separation — the two matchers never cross
# ---------------------------------------------------------------------------

def test_file_matcher_ignores_command_entries():
    idx = [_c(pattern="*.py"), _f(step="b", pattern="*.py")]
    got = match.match_target(idx, "anything.py")
    assert [g["step_id"] for g in got] == ["b"]


def test_command_matcher_ignores_file_and_kindless_entries():
    """A round-1 entry carries no `kind`; absent means file_glob, and it must
    never match a command even when the glob text would."""
    idx = [_f(pattern="git push*"),
           {**_f(step="b", pattern="git push*"), "kind": "file_glob"},
           _c(step="c", pattern="git push*")]
    got = match.match_command(idx, "git push origin main")
    assert [g["step_id"] for g in got] == ["c"]


# ---------------------------------------------------------------------------
# Cannot raise — hostile patterns and hostile entries
# ---------------------------------------------------------------------------

def test_hostile_patterns_cannot_raise():
    for bad in ["[", "**[", "\\", "a" * 5000, "../../*", "(?i)x"]:
        assert match.match_command([_c(pattern=bad)], "git push") == []


def test_non_string_patterns_cannot_raise():
    """load_index returns whatever JSON was in Redis, so a null or numeric
    pattern is reachable on the blocking pre-tool path."""
    for bad in [None, 123, ["git push*"], {"pattern": "*"}]:
        assert match.match_command([_c(pattern=bad)], "git push") == []


def test_a_hostile_entry_shape_cannot_raise():
    for bad_entry in [None, 42, "entry", [], {"kind": "command"}]:
        try:
            match.match_command([bad_entry], "git push")
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"raised on {bad_entry!r}: {exc}") from exc


def test_an_overlong_pattern_is_refused_not_evaluated():
    """MAX_PATTERN_CHARS bounds the write path; the matcher refuses anything
    longer outright rather than spending regex time on it."""
    long_pattern = ("*a" * 1000)
    assert match.match_command([_c(pattern=long_pattern)], "a" * 400) == []


def test_an_adversarially_long_command_neither_raises_nor_hangs():
    idx = [_c(pattern="echo *"), _c(step="b", pattern="*[" )]
    got = match.match_command(idx, "echo " + "x " * 50_000)
    assert [g["step_id"] for g in got] == ["a"]


def test_empty_inputs_match_nothing():
    assert match.match_command([_c()], "") == []
    assert match.match_command([_c(pattern="")], "git push") == []
    assert match.match_command([], "git push") == []


# ---------------------------------------------------------------------------
# missing_load_bearing spans both kinds of one skill
# ---------------------------------------------------------------------------

def test_a_command_step_can_gate_a_file_step_and_vice_versa():
    idx = [
        _c(step="backup", pattern="bash backup.sh*", order=0, load_bearing=True),
        {**_f(step="conf", pattern="deploy.toml"), "order": 1,
         "load_bearing": True},
        _c(step="update", pattern="bash update.sh*", order=2),
    ]
    missing = match.missing_load_bearing(idx, "s1", 2, set())
    assert [m["step_id"] for m in missing] == ["backup", "conf"]
    assert match.missing_load_bearing(idx, "s1", 2, {"backup", "conf"}) == []
