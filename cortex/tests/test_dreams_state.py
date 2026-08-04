import fakeredis

from app.dreams.state import DreamState


def _state():
    return DreamState(fakeredis.FakeStrictRedis(decode_responses=True))


def test_get_run_on_fresh_redis_is_empty_dict_not_error():
    assert _state().get_run() == {}


def test_record_run_roundtrips_and_merges():
    s = _state()
    s.record_run(health="ok", clusters_done=3)
    s.record_run(clusters_done=4)
    run = s.get_run()
    assert run["health"] == "ok"
    assert run["clusters_done"] == "4"
    assert run["last_run"]


def test_unit_dedupe_is_per_kind():
    s = _state()
    s.mark_unit_done("cluster", "abc")
    assert s.is_unit_done("cluster", "abc")
    assert not s.is_unit_done("profile", "abc")


def test_counter_bump_and_reset():
    s = _state()
    assert s.bump_counter("new_memories", 5) == 5
    assert s.bump_counter("new_memories") == 6
    assert s.get_counter("new_memories") == 6
    s.reset_progress()
    assert s.get_counter("new_memories") == 0
    assert not s.is_unit_done("cluster", "abc")


def test_counter_on_missing_key_is_zero():
    assert _state().get_counter("never_set") == 0
