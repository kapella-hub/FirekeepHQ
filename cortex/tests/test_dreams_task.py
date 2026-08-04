from datetime import datetime, timedelta, timezone

from app.dreams import task as dt

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _gate(**kw):
    args = dict(
        enabled=True,
        now=NOW,
        last_write_at=NOW - timedelta(minutes=45),
        idle_minutes=30,
        new_memories=100,
        min_new=25,
    )
    args.update(kw)
    return dt.should_run(**args)


def test_gate_opens_when_idle_and_work_exists():
    ok, reason = _gate()
    assert ok, reason


def test_gate_closed_when_disabled():
    ok, reason = _gate(enabled=False)
    assert not ok and "disabled" in reason


def test_gate_closed_while_recently_active():
    ok, reason = _gate(last_write_at=NOW - timedelta(minutes=2))
    assert not ok and "idle" in reason


def test_gate_closed_without_enough_new_memories():
    ok, reason = _gate(new_memories=3)
    assert not ok and "new" in reason


def test_gate_opens_when_never_written_before():
    ok, reason = _gate(last_write_at=None)
    assert ok, reason


def test_task_is_registered_on_beat_with_matching_name():
    from app.workers.sleep_cycle import celery_app

    name = "app.dreams.task.run_dream_tick"
    assert name in celery_app.tasks
    entry = celery_app.conf.beat_schedule["dream-tick"]
    assert entry["task"] == name


def test_disabled_task_returns_status_without_building_clients(monkeypatch):
    monkeypatch.setattr(dt, "_build_clients", lambda: (_ for _ in ()).throw(
        AssertionError("must not build clients when disabled")))
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DREAM_ENABLED", "false")
    out = dt.run_dream_tick()
    assert out["status"] == "disabled"
