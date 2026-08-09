"""Pure matching. No I/O — these are the functions that must never raise on the
blocking pre-edit path (I6)."""

from app.procedures import match


def _e(skill="s1", step="a", pattern="*.py", order=0, load_bearing=False, text="t"):
    return {"skill_id": skill, "skill_trigger": "trig", "step_id": step,
            "step_text": text, "pattern": pattern, "load_bearing": load_bearing,
            "order": order}


def test_matches_a_glob():
    idx = [_e(pattern="requirements.txt"), _e(step="b", pattern="*.md")]
    got = match.match_target(idx, "requirements.txt")
    assert [g["step_id"] for g in got] == ["a"]


def test_matches_a_path_suffix_so_absolute_targets_work():
    """pre_tool sends whatever path the tool was given — often absolute. A
    pattern authored as a repo-relative glob must still match."""
    idx = [_e(pattern="client/pyproject.toml")]
    got = match.match_target(idx, "E:/Documents/Projects/Firekeep/client/pyproject.toml")
    assert len(got) == 1


def test_backslash_paths_match_forward_slash_patterns():
    idx = [_e(pattern="cortex/app/*.py")]
    assert match.match_target(idx, r"cortex\app\main.py")


def test_a_hostile_pattern_cannot_raise():
    for bad in ["[", "**[", "\\", "a" * 5000, "../../*", "(?i)x"]:
        assert match.match_target([_e(pattern=bad)], "anything.py") == []


def test_a_non_string_pattern_cannot_raise():
    """The one input that PROVES _matches' try/except is load-bearing.

    None of the hostile STRING patterns above actually raise — fnmatch swallows
    an unbalanced bracket and a stray backslash — so without this case the guard
    is untested and a later 'simplification' would delete it with a green suite.
    load_index returns whatever JSON was in Redis, so a null or numeric pattern
    is reachable, and _norm(None) raises AttributeError on the blocking
    pre-edit path (I6).
    """
    for bad in [None, 123, ["*.py"], {"pattern": "*.py"}]:
        assert match.match_target([_e(pattern=bad)], "anything.py") == []


def test_no_match_on_empty_target():
    assert match.match_target([_e()], "") == []


def test_missing_load_bearing_only_looks_earlier():
    idx = [
        _e(step="a", order=0, load_bearing=True),
        _e(step="b", order=1, load_bearing=False),
        _e(step="c", order=2, load_bearing=True),
    ]
    missing = match.missing_load_bearing(idx, "s1", matched_order=1, observed_step_ids=set())
    assert [m["step_id"] for m in missing] == ["a"]


def test_an_observed_step_is_not_missing():
    idx = [_e(step="a", order=0, load_bearing=True), _e(step="b", order=1)]
    assert match.missing_load_bearing(idx, "s1", 1, {"a"}) == []


def test_other_skills_steps_are_never_considered():
    idx = [_e(skill="other", step="x", order=0, load_bearing=True), _e(step="b", order=1)]
    assert match.missing_load_bearing(idx, "s1", 1, set()) == []


def test_advisory_text_without_stats_states_no_numbers():
    txt = match.advisory_text(_e(), {"step_id": "a", "step_text": "regen the lock"}, None)
    assert "regen the lock" in txt
    assert "%" not in txt and " of " not in txt


def test_advisory_text_with_stats_quotes_them():
    stats = {"a": {"observed": 11, "skipped": 4, "executions": 15}}
    txt = match.advisory_text(_e(), {"step_id": "a", "step_text": "regen the lock"}, stats)
    assert "11" in txt and "15" in txt
