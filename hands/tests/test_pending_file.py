"""`pending.json` — the record of what is waiting for a chord."""
import json

from firekeep_hands.broker import pending
from firekeep_hands.broker.permits import PermitStore
from firekeep_hands.broker.server import PermitAnnouncer


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


def _wired(clock=None):
    """A store with the announcer attached, its notifier recorded rather
    than shown."""
    shown = []
    announcer = PermitAnnouncer("ctrl+alt+y", "ctrl+alt+n",
                                notifier=lambda *a: shown.append(a))
    store = PermitStore(ttl_s=60, clock=clock or Clock(), on_change=announcer)
    return store, shown


def _rows():
    return pending.read_pending()["permits"]


def test_a_new_permit_appears_in_the_file(isolated_home):
    store, _shown = _wired()
    store.request(challenge="c", title="Invoke Send in Mail", classes=("send",),
                  task_id="t", step_index=1)
    state = pending.read_pending()
    assert state["chord"] == "ctrl+alt+y" and state["deny_chord"] == "ctrl+alt+n"
    row = state["permits"][0]
    assert row["challenge"] == "c" and row["title"] == "Invoke Send in Mail"
    assert row["classes"] == ["send"] and row["expires_in_s"] == 60.0


def test_a_decided_permit_leaves_the_file(isolated_home):
    store, _shown = _wired()
    store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=0)
    assert len(_rows()) == 1
    store.decide("c", "approve", via="chord")
    assert _rows() == []


def test_a_consumed_permit_leaves_the_file(isolated_home):
    store, _shown = _wired()
    store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=0)
    store.decide("c", "approve", via="chord")
    store.consume("c")
    assert _rows() == []


def test_an_expired_permit_leaves_the_file_on_the_next_look(isolated_home):
    clock = Clock()
    store, _shown = _wired(clock)
    store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=0)
    assert len(_rows()) == 1
    clock.t += 61
    store.get("c")                      # any call sweeps, and the sweep is a change
    assert _rows() == []


def test_the_file_lists_the_oldest_first(isolated_home):
    clock = Clock()
    store, _shown = _wired(clock)
    store.request(challenge="a", title="A", classes=("send",), task_id="t", step_index=0)
    clock.t += 1
    store.request(challenge="b", title="B", classes=("money",), task_id="t", step_index=1)
    assert [row["title"] for row in _rows()] == ["A", "B"]


def test_the_file_is_written_atomically_and_privately(isolated_home):
    import stat
    import sys
    store, _shown = _wired()
    store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=0)
    path = pending.pending_path()
    assert json.loads(path.read_text(encoding="utf-8"))["permits"]
    assert not list(path.parent.glob("pending.json.tmp*"))
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_reading_a_missing_or_corrupt_file_is_nothing_pending(isolated_home):
    assert pending.read_pending() == {"chord": "", "deny_chord": "", "permits": []}
    pending.pending_path().write_text("{not json", encoding="utf-8")
    assert pending.read_pending()["permits"] == []
    pending.pending_path().write_text('["a list"]', encoding="utf-8")
    assert pending.read_pending()["permits"] == []
    pending.pending_path().write_text('{"permits": "not a list"}', encoding="utf-8")
    assert pending.read_pending()["permits"] == []


def test_clear_removes_the_file(isolated_home):
    store, _shown = _wired()
    store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=0)
    assert pending.pending_path().exists()
    pending.clear_pending()
    assert not pending.pending_path().exists()
    pending.clear_pending()             # idempotent


def test_a_write_failure_never_fails_a_permit(isolated_home, monkeypatch):
    def boom(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(pending, "_write_json_atomic", boom)
    store, _shown = _wired()
    permit = store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=0)
    assert permit.state == "pending"
    assert store.decide("c", "approve", via="chord") is not None
    assert store.consume("c") is True


# -- the notification half ---------------------------------------------------


def test_each_permit_is_announced_once(isolated_home):
    store, shown = _wired()
    store.request(challenge="c", title="Send it", classes=("send",), task_id="t", step_index=0)
    store.get("c"); store.pending(); store.get("c")
    assert len(shown) == 1
    assert shown[0] == ("Send it", ("send",), "ctrl+alt+y", "ctrl+alt+n")


def test_resolving_a_permit_raises_no_new_toast(isolated_home):
    store, shown = _wired()
    store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=0)
    store.decide("c", "approve", via="chord")
    store.consume("c")
    assert len(shown) == 1


def test_a_second_permit_is_announced_too(isolated_home):
    store, shown = _wired()
    store.request(challenge="a", title="A", classes=("send",), task_id="t", step_index=0)
    store.request(challenge="b", title="B", classes=("money",), task_id="t", step_index=1)
    assert [args[0] for args in shown] == ["A", "B"]


def test_a_notifier_that_raises_never_fails_a_permit(isolated_home):
    def boom(*a):
        raise RuntimeError("no notification daemon")

    announcer = PermitAnnouncer("ctrl+alt+y", "ctrl+alt+n", notifier=boom)
    store = PermitStore(ttl_s=60, on_change=announcer)
    permit = store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=0)
    assert permit.state == "pending"
    assert store.decide("c", "approve", via="chord") is not None
    assert store.consume("c") is True


def test_a_store_with_no_watcher_behaves_exactly_as_before(isolated_home):
    """The hook is optional; nothing else in the broker changed shape."""
    store = PermitStore(ttl_s=60)
    store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=0)
    assert store.decide("c", "approve", via="chord") is not None
    assert store.consume("c") is True
    assert not pending.pending_path().exists()


def test_fifty_threads_requesting_at_once_leave_a_readable_file(isolated_home, caplog):
    """The announcer runs outside the store lock by design, so real threads
    write this file concurrently. Before the temp name carried a thread id
    they collided on one `<name>.tmp-<pid>` path and most writes were lost —
    swallowed into a debug line, leaving a stale file that told the human
    the wrong thing about what a chord would approve."""
    import logging
    import threading

    shown = []
    lock = threading.Lock()

    def record(*args):
        with lock:
            shown.append(args[0])

    announcer = PermitAnnouncer("ctrl+alt+y", "ctrl+alt+n", notifier=record)
    store = PermitStore(ttl_s=600, on_change=announcer)   # the real wall clock

    count = 50
    start = threading.Barrier(count)
    errors = []

    def request(index):
        try:
            start.wait(timeout=10)
            store.request(challenge=f"c{index:02d}", title=f"step {index}",
                          classes=("send",), task_id="t", step_index=index)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    with caplog.at_level(logging.DEBUG, logger="firekeep_hands.broker.pending"):
        threads = [threading.Thread(target=request, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    # nothing was swallowed
    assert [r.getMessage() for r in caplog.records if "could not write" in r.getMessage()] == []
    # the file parses and holds every permit
    state = pending.read_pending()
    assert {row["challenge"] for row in state["permits"]} == {f"c{i:02d}" for i in range(count)}
    assert len(store.pending()) == count
    # and each permit was announced exactly once
    assert sorted(shown) == sorted(f"step {i}" for i in range(count))
    # no temp files left behind
    assert list(pending.pending_path().parent.glob("pending.json.tmp*")) == []


def test_the_watcher_is_never_called_while_the_store_lock_is_held(isolated_home):
    """It writes a file and can spawn a process. Holding the lock across
    that would stall every HTTP handler and the chord listener, and would
    deadlock outright if a watcher ever called back into the store."""
    seen = []

    def reentrant(store):
        # would deadlock on a non-reentrant lock, and blocks other threads
        # on a reentrant one; both are why the callback fires after release
        seen.append(len(store.pending()))

    store = PermitStore(ttl_s=60, on_change=reentrant)
    store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=0)
    assert seen == [1]
