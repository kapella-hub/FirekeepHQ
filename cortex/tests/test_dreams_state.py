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


def test_reset_progress_clears_every_per_run_counter():
    """Naming the whole set explicitly, because a counter that is bumped but
    never listed in reset_progress silently becomes an all-time total. That is
    what would make `insights_written` — the field GET /dreams uses to tell a
    productive run from a barren one — describe the deployment's history rather
    than the run in front of the operator."""
    s = _state()
    per_run = ("new_memories", "clusters_done", "profiles_done",
               "insights_written", "errors")
    for name in per_run:
        s.bump_counter(name, 3)
    s.reset_progress()
    assert {name: s.get_counter(name) for name in per_run} == dict.fromkeys(per_run, 0)


def test_done_set_on_fresh_redis_is_empty_set_not_error():
    assert _state().done_set("profile") == set()


def test_done_set_returns_all_marked_keys_in_one_read():
    s = _state()
    s.mark_unit_done("profile", "m1::ws1::default::")
    s.mark_unit_done("profile", "m2::ws1::default::")
    assert s.done_set("profile") == {"m1::ws1::default::", "m2::ws1::default::"}
    assert s.done_set("cluster") == set()


# --- the consolidated ledger (final-review I2+I3) ---------------------------

def test_consolidated_set_on_fresh_redis_is_empty_set_not_error():
    assert _state().consolidated_set() == set()


def test_mark_consolidated_roundtrips_and_accumulates():
    s = _state()
    s.mark_consolidated(["a", "b"])
    s.mark_consolidated(["b", "c"])
    assert s.consolidated_set() == {"a", "b", "c"}


def test_mark_consolidated_with_no_ids_is_a_no_op():
    """A cluster the LLM produced zero insights for calls through with an
    empty list; redis-py's SADD raises on zero members, and a tick must not
    die because a synthesis came back empty."""
    s = _state()
    s.mark_consolidated([])
    assert s.consolidated_set() == set()


def test_reset_progress_leaves_the_consolidated_ledger_intact():
    """THE regression guard for the starvation fix (final-review I2+I3).

    reset_progress clears per-run progress at the end of every run. If the
    consolidated ledger were ever added to either of its lists, every run
    would rediscover the same first-N clusters in sorted-bucket order and
    re-synthesize them forever while later partitions were never reached —
    which is precisely the bug the ledger exists to fix. Per-run state must
    go; the store-level fact must not.
    """
    s = _state()
    s.mark_unit_done("cluster", "ck1")
    s.bump_counter("clusters_done", 3)
    s.mark_consolidated(["m1", "m2", "m3"])

    s.reset_progress()

    assert s.done_set("cluster") == set(), "per-run done-set must be cleared"
    assert s.get_counter("clusters_done") == 0, "per-run counter must be cleared"
    assert s.consolidated_set() == {"m1", "m2", "m3"}, \
        "the consolidated ledger is NOT per-run progress and must survive"
