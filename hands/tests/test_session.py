"""`HandsSession` — the state machine every MCP tool call goes through.

The doubles here are deliberately *real* where the safety property lives: the
fake broker client wraps an actual `PermitStore`, so "approve, consume, one
use only, expiry" is exercised by the same code the running broker uses
rather than by a stub that returns whatever the test wants. Only the
transport (HTTP), the Keep (cortex/relay) and the platform backend are faked.
"""
from __future__ import annotations

import pytest

from firekeep_hands.backends.base import (
    Control,
    HandsError,
    Rect,
    UnsupportedBackend,
    WindowInfo,
)
from firekeep_hands.backends.fake import FakeBackend
from firekeep_hands.broker.permits import PermitStore
from firekeep_hands.config import HandsConfig, Policy
from firekeep_hands.session import HandsSession

# -- doubles ---------------------------------------------------------------


class RecordingBackend(FakeBackend):
    """`FakeBackend` plus a record of every `observe()` call and a stand-in
    PNG, so a test can prove a protected step captured before/after images
    and an unprotected one captured none."""

    def __init__(self, *args, screenshot_error: Exception | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.observes: list[dict] = []
        self._screenshot_error = screenshot_error

    def observe(self, **kwargs):
        self.observes.append(dict(kwargs))
        if kwargs.get("screenshot") and self._screenshot_error is not None:
            raise self._screenshot_error
        observation = super().observe(**kwargs)
        if kwargs.get("screenshot"):
            observation.screenshot_png = b"\x89PNG\r\n\x1a\nfake"
        return observation

    def shots(self) -> int:
        return sum(1 for call in self.observes if call.get("screenshot"))


class FakeBrokerClient:
    """`BrokerClient`'s surface over a real `PermitStore` — the HTTP hop is
    the only thing removed."""

    def __init__(self, store: PermitStore, *, listeners: dict[str, str] | None = None,
                 request_state: str | None = None):
        self.store = store
        self.listeners = listeners or {"chord": "active", "phone": "off"}
        self.requested: list[tuple] = []
        self._request_state = request_state  # force "unreachable"/"error" replies

    def _json(self, permit) -> dict:
        return {
            "challenge": permit.challenge,
            "title": permit.title,
            "classes": list(permit.classes),
            "task_id": permit.task_id,
            "step_index": permit.step_index,
            "state": permit.state,
            "via": permit.via,
            "expires_in_s": max(0.0, permit.expires_at - self.store.now()),
        }

    def health(self) -> dict:
        return {"ok": True, "chord": "ctrl+alt+y", "listeners": dict(self.listeners),
                "pending": len(self.store.pending())}

    def request(self, *, challenge, title, classes, task_id, step_index) -> dict:
        self.requested.append((challenge, title, tuple(classes), task_id, step_index))
        if self._request_state is not None:
            return {"challenge": challenge, "state": self._request_state, "via": None}
        permit = self.store.request(challenge=challenge, title=title, classes=classes,
                                    task_id=task_id, step_index=step_index)
        return self._json(permit)

    def get(self, challenge) -> dict | None:
        permit = self.store.get(challenge)
        return self._json(permit) if permit is not None else None

    def wait(self, challenge, timeout_s) -> dict:
        permit = self.store.get(challenge)
        if permit is None:
            return {"challenge": challenge, "state": "unknown", "via": None}
        return self._json(permit)

    def consume(self, challenge) -> bool:
        return self.store.consume(challenge)


class FakeLink:
    """`KeepLink` with the network removed; every call is recorded so the
    lifecycle tests can assert on ordering and counts."""

    offline = True

    def __init__(self, *, acquired: bool = True):
        self._acquired = acquired
        self.before: list[tuple] = []
        self.after: list[tuple] = []
        self.leases: list[int] = []
        self.renewed = 0
        self.released = False

    def action_before(self, *, goal, task_id, apps):
        self.before.append((goal, task_id, tuple(apps)))
        return "A1"

    def action_after(self, action_id, outcome, summary):
        self.after.append((action_id, outcome, summary))

    def acquire_lease(self, ttl_minutes: int = 30):
        self.leases.append(ttl_minutes)
        return {"acquired": self._acquired, "fencing_token": 7}

    def renew_lease(self):
        self.renewed += 1

    def release_lease(self):
        self.released = True


class FakeBrowser:
    """Records operations. Refs are generation-stamped (`g<gen>-d<N>`) and
    only the current generation is live, so a ref from an older scan raises
    `stale_ref` the way the DOM probe does."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.live = {"g1-d1", "g1-d2"}
        self.url = "about:blank"
        self.loaded = True

    def open(self) -> dict:
        self.calls.append(("open",))
        return {"tabs": [{"id": "t1", "url": self.url, "title": "New Tab"}]}

    def tabs(self) -> list[dict]:
        self.calls.append(("tabs",))
        return [{"id": "t1", "url": self.url, "title": "New Tab"}]

    def navigate(self, url, *, tab=None) -> dict:
        self.calls.append(("navigate", url))
        self.url = url
        return {"url": url, "title": "Page", "loaded": self.loaded}

    def read(self, *, tab=None, budget=4000) -> dict:
        self.calls.append(("read",))
        return {"url": self.url, "title": "Page", "text": "hello"[:budget]}

    def find(self, query, *, tab=None, limit=10) -> list[dict]:
        self.calls.append(("find", query))
        return [{"ref": "g1-d1", "role": "button", "name": query}][:limit]

    def click(self, ref, *, tab=None) -> None:
        if ref not in self.live:
            raise HandsError("stale_ref", f"{ref} no longer exists (find again)")
        self.calls.append(("click", ref))

    def fill(self, ref, text, *, tab=None) -> None:
        if ref not in self.live:
            raise HandsError("stale_ref", f"{ref} no longer exists (find again)")
        self.calls.append(("fill", ref, text))

    def screenshot(self, *, tab=None, max_width=1280) -> bytes:
        self.calls.append(("screenshot",))
        return b"\x89PNG\r\n\x1a\nbrowser"


# -- fixtures --------------------------------------------------------------

_TITLES = {"Mail": "Inbox — Mail", "Notepad": "Untitled — Notepad"}


def _scene(app: str) -> list[Control]:
    return [
        Control("c1", "Button", "Save", "", Rect(10, 20, 100, 40), app, ("Invoke",)),
        Control("send", "Button", "Send", "", Rect(120, 20, 60, 40), app, ("Invoke",)),
        Control("delete", "Button", "Delete", "", Rect(190, 20, 60, 40), app, ("Invoke",)),
        Control("edit", "Edit", "Text Editor", "", Rect(0, 80, 400, 300), app, ("Value",)),
    ]


def _window(app: str) -> WindowInfo:
    return WindowInfo(app, _TITLES[app], 4242, Rect(0, 0, 800, 600))


def build_session(app="Notepad", *, broker=None, browser=None, link=None,
                  backend=None, policy=None) -> HandsSession:
    backend = backend or RecordingBackend(controls=_scene(app), window=_window(app))
    return HandsSession(
        backend=backend,
        broker=broker,
        link=link or FakeLink(),
        browser=browser,
        config=HandsConfig(),
        policy=policy or Policy([], [], []),
        session_id="s1",
    )


@pytest.fixture
def store():
    return PermitStore(ttl_s=60)


@pytest.fixture
def broker(store):
    return FakeBrokerClient(store)


@pytest.fixture
def session(request, broker):
    """Windowed on "Notepad" by default; the Mail tests ask for "Mail" with
    `@pytest.mark.parametrize("session", ["Mail"], indirect=True)`."""
    return build_session(getattr(request, "param", "Notepad"), broker=broker)


@pytest.fixture
def session_without_broker(request):
    return build_session(getattr(request, "param", "Mail"), broker=None)


# -- the brief's tests -----------------------------------------------------


def test_unprotected_action_runs_and_is_ledgered(session):
    session.task_start("save the note", ["Notepad"])
    session.observe()
    r = session.act({"kind": "invoke", "ref": "c1"})
    assert r["ok"] and r["route"] == "accessibility"
    assert session.backend.calls[-1] == ("invoke", "c1")
    assert session.ledger.steps()[0]["classes"] == []
    # no screenshots for an unprotected step
    assert session.backend.shots() == 0
    assert session.ledger.steps()[0]["before"] is None


@pytest.mark.parametrize("session", ["Mail"], indirect=True)
def test_protected_action_needs_a_permit_then_runs_once(session, store):
    session.task_start("send the mail", ["Mail"])
    session.observe()
    r = session.act({"kind": "invoke", "ref": "send"})
    assert r["ok"] is False and r["needs_permit"]["classes"] == ["send"]
    ch = r["needs_permit"]["challenge"]
    assert session.act({"kind": "invoke", "ref": "send"}, permit=ch)["ok"] is False
    store.decide(ch, "approve", via="chord")
    assert session.request_permit(ch, wait_s=1)["state"] == "approved"
    session.observe()
    ok = session.act({"kind": "invoke", "ref": "send"}, permit=ch)
    assert ok["ok"] and session.ledger.steps()[-1]["permit"] == {"challenge": ch, "via": "chord"}
    session.observe()
    again = session.act({"kind": "invoke", "ref": "send"}, permit=ch)
    assert again["ok"] is False and "needs_permit" in again


@pytest.mark.parametrize("session", ["Mail"], indirect=True)
def test_permit_is_bound_to_the_exact_action(session, store):
    session.task_start("x", ["Mail"])
    session.observe()
    ch = session.act({"kind": "invoke", "ref": "send"})["needs_permit"]["challenge"]
    store.decide(ch, "approve", via="chord")
    session.observe()
    r = session.act({"kind": "invoke", "ref": "delete"}, permit=ch)
    assert r["ok"] is False and r["needs_permit"]["challenge"] != ch


def test_no_broker_fails_closed_for_protected_only(session_without_broker):
    s = session_without_broker
    s.task_start("x", ["Mail"])
    s.observe()
    assert s.act({"kind": "invoke", "ref": "c1"})["ok"] is True
    s.observe()  # every act invalidates refs (see test_refs_go_stale_after_any_action)
    r = s.act({"kind": "invoke", "ref": "send"})
    assert r["ok"] is False and "broker" in r["error"]


def test_refs_go_stale_after_any_action(session):
    session.task_start("x", ["Notepad"])
    session.observe()
    session.act({"kind": "type", "text": "a"})
    r = session.act({"kind": "invoke", "ref": "c1"})
    assert r["ok"] is False and r["error"].startswith("stale_ref")


def test_budget_and_lifecycle(session):
    session.config.max_steps = 2
    session.task_start("x", ["Notepad"])
    session.act({"kind": "wait", "seconds": 0})
    session.act({"kind": "wait", "seconds": 0})
    assert session.act({"kind": "wait", "seconds": 0})["error"].startswith("budget")
    end = session.task_end("done", "ok")
    assert end["steps"] == 2
    assert session.link.after == [("A1", "done", "ok")]
    assert session.link.released is True


# -- the rulings this task was given ---------------------------------------


@pytest.mark.parametrize("session", ["Mail"], indirect=True)
def test_permit_title_comes_from_the_routed_control_not_the_model(session, broker):
    """The broker displays whatever the server sends it, so the title has to
    be built from what was observed and routed — never from a string the
    model put in the action."""
    session.task_start("x", ["Mail"])
    session.observe()
    session.act({"kind": "invoke", "ref": "send", "title": "totally harmless"})
    challenge, title, classes, task_id, step_index = broker.requested[-1]
    assert title == 'invoke "Send" in Mail'
    assert classes == ("send",) and task_id == session.task_id and step_index == 0
    assert "harmless" not in title


@pytest.mark.parametrize("session", ["Mail"], indirect=True)
def test_a_protected_step_stores_before_and_after_images(session, store):
    session.task_start("x", ["Mail"])
    session.observe()
    ch = session.act({"kind": "invoke", "ref": "send"})["needs_permit"]["challenge"]
    store.decide(ch, "approve", via="chord")
    session.observe()
    assert session.act({"kind": "invoke", "ref": "send"}, permit=ch)["ok"]
    line = session.ledger.steps()[-1]
    assert line["before"] and line["after"]
    assert (session.ledger.dir / "000-before.png").exists()
    assert (session.ledger.dir / "000-after.png").exists()


@pytest.mark.parametrize("session", ["Mail"], indirect=True)
def test_a_failed_capture_never_burns_the_approval(session, store):
    """The permit is consumed before the "before" image is taken. A machine
    that cannot screenshot must still run the step the human approved."""
    session.backend._screenshot_error = HandsError("permission", "mss is unavailable")
    session.task_start("x", ["Mail"])
    session.observe()
    ch = session.act({"kind": "invoke", "ref": "send"})["needs_permit"]["challenge"]
    store.decide(ch, "approve", via="chord")
    session.observe()
    r = session.act({"kind": "invoke", "ref": "send"}, permit=ch)
    assert r["ok"] is True
    line = session.ledger.steps()[-1]
    assert line["before"] is None and line["after"] is None
    assert session.backend.calls[-1] == ("invoke", "send")


@pytest.mark.parametrize("session_without_broker", ["Mail"], indirect=True)
def test_a_broker_that_dies_between_health_and_request_fails_closed(session_without_broker, store):
    s = session_without_broker
    s.broker = FakeBrokerClient(store, request_state="unreachable")
    s.task_start("x", ["Mail"])
    s.observe()
    r = s.act({"kind": "invoke", "ref": "send"})
    assert r["ok"] is False and "broker" in r["error"] and "needs_permit" not in r
    assert s.broker is None  # dropped, so the next protected step re-probes


def test_request_permit_without_a_broker_is_unavailable(session_without_broker):
    r = session_without_broker.request_permit("nope", wait_s=1)
    assert r["state"] == "unavailable" and "broker" in r["error"]


def test_lease_is_renewed_every_ten_steps(session):
    session.task_start("x", ["Notepad"])
    for _ in range(21):
        session.act({"kind": "wait", "seconds": 0})
    assert session.link.renewed == 2
    assert session.link.leases == [30]


def test_task_start_refuses_when_another_session_holds_the_machine():
    s = build_session("Notepad", link=FakeLink(acquired=False))
    with pytest.raises(HandsError) as ei:
        s.task_start("x", ["Notepad"])
    assert ei.value.code == "busy"
    assert s.task_id is None


def test_a_second_task_start_is_refused_while_one_is_open(session):
    session.task_start("first", ["Notepad"])
    with pytest.raises(HandsError) as ei:
        session.task_start("second", ["Notepad"])
    assert ei.value.code == "busy"


def test_act_without_a_task_is_refused(session):
    r = session.act({"kind": "wait", "seconds": 0})
    assert r["ok"] is False and r["error"].startswith("no_task")


def test_find_refs_are_actable(session):
    """`route` only resolves refs that are in the current observation, so a
    ref handed out by `hands_find` has to land there too."""
    session.task_start("x", ["Notepad"])
    found = session.find("save")
    assert [c["ref"] for c in found["controls"]] == ["c1"]
    assert session.act({"kind": "invoke", "ref": "c1"})["ok"] is True


def test_find_on_a_missing_app_is_an_empty_list_not_an_error(session):
    session.task_start("x", ["Notepad"])
    assert session.find("save", app="Nothing")["controls"] == []


def test_observe_and_find_need_an_open_task(session):
    """An observation is a tree — and maybe a picture — of the human's own
    screen, so it belongs inside a declared task with a ledger and a lease.
    Only hands_status answers with no task."""
    assert session.observe()["error"].startswith("no_task")
    assert session.find("save")["error"].startswith("no_task")
    assert session.status()["ok"] is True


def test_observe_detail_levels(session):
    session.task_start("x", ["Notepad"])
    summary = session.observe(detail="summary")
    assert "controls" not in summary and summary["control_count"] == 4
    controls = session.observe(detail="controls")
    assert [c["ref"] for c in controls["controls"]] == ["c1", "send", "delete", "edit"]
    shot = session.observe(detail="screenshot")
    assert shot["screenshot_png"].startswith(b"\x89PNG")
    with pytest.raises(HandsError) as ei:
        session.observe(detail="everything")
    assert ei.value.code == "invalid_action"


def test_status_reports_the_brokers_listeners_verbatim(session):
    st = session.status()
    assert st["broker"]["running"] is True
    assert st["broker"]["listeners"] == {"chord": "active", "phone": "off"}
    assert "phone_approvals true" in st["approvals"]
    assert "docs/guides/hands.md" in st["approvals"]
    assert st["backend"] == "fake" and st["task"] is None


def test_status_without_a_broker_says_protected_steps_are_refused(session_without_broker):
    st = session_without_broker.status()
    assert st["broker"] == {"running": False}
    assert "refused" in st["approvals"]


def test_status_on_an_unsupported_platform_still_answers():
    s = build_session(backend=UnsupportedBackend())
    st = s.status()
    assert st["backend"] == "unsupported"
    assert st["permissions"]["accessibility"] == "missing"


# -- the browser -----------------------------------------------------------


def broker_title(session) -> str:
    """The title the session last showed the human for a permit."""
    return session.broker.requested[-1][1]


def test_browser_navigate_goes_through_the_policy_gate(session, store):
    browser = FakeBrowser()
    session.browser = browser
    session.task_start("x", ["Notepad"])
    r = session.browser_op("navigate", url="https://evil.example/x")
    assert r["ok"] is False and r["needs_permit"]["classes"] == ["boundary"]
    ch = r["needs_permit"]["challenge"]
    assert broker_title(session) == "open evil.example in the browser"
    store.decide(ch, "approve", via="chord")
    ok = session.browser_op("navigate", url="https://evil.example/x", permit=ch)
    assert ok["ok"] and browser.calls[-1] == ("navigate", "https://evil.example/x")
    assert session.ledger.steps()[-1]["route"] == "browser"


def test_browser_navigate_to_an_allowlisted_host_is_not_a_boundary(session):
    session.browser = FakeBrowser()
    session.policy.domains.append("example.com")
    session.task_start("x", ["Notepad"])
    r = session.browser_op("navigate", url="https://docs.example.com/a")
    assert r["ok"] is True and r["classes"] == []


def test_browser_direct_ops_are_ledgered_steps(session):
    browser = FakeBrowser()
    session.browser = browser
    session.task_start("x", ["Notepad"])
    assert session.browser_op("open")["ok"]
    assert session.browser_op("find", query="Send")["controls"][0]["ref"] == "g1-d1"
    assert session.browser_op("click", ref="g1-d1")["ok"]
    assert session.browser_op("read")["text"] == "hello"
    assert session.browser_op("screenshot")["screenshot_png"].startswith(b"\x89PNG")
    steps = session.ledger.steps()
    assert len(steps) == 5 and {s["route"] for s in steps} == {"browser"}
    assert session.step_index == 5


def test_browser_click_on_a_ref_from_an_older_scan_is_an_error_not_a_raise(session):
    session.browser = FakeBrowser()
    session.task_start("x", ["Notepad"])
    r = session.browser_op("click", ref="g0-d1")
    assert r["ok"] is False and r["error"].startswith("stale_ref")
    assert session.ledger.steps()[-1]["outcome"] == "error"


def test_a_navigation_that_never_loaded_is_still_an_ok_step_with_the_flag(session):
    """Task 9's `navigate` no longer raises on a missing load event; it
    reports `loaded: False`. That is a step that happened, so the ledger says
    "ok" and the runtime is told to look before it acts."""
    browser = FakeBrowser()
    browser.loaded = False
    session.browser = browser
    session.policy.domains.append("example.com")
    session.task_start("x", ["Notepad"])
    r = session.browser_op("navigate", url="https://example.com/slow")
    assert r["ok"] is True and r["loaded"] is False
    assert r["url"] == "https://example.com/slow"
    assert session.ledger.steps()[-1]["outcome"] == "ok"


def test_unknown_browser_op_is_refused(session):
    session.browser = FakeBrowser()
    session.task_start("x", ["Notepad"])
    r = session.browser_op("eval", text="alert(1)")
    assert r["ok"] is False and r["error"].startswith("invalid_action")


def test_browser_ops_need_an_open_task(session):
    session.browser = FakeBrowser()
    r = session.browser_op("open")
    assert r["ok"] is False and r["error"].startswith("no_task")


# -- execution routes ------------------------------------------------------


def test_set_value_without_a_value_pattern_clicks_selects_and_types(session):
    session.task_start("x", ["Notepad"])
    session.backend.scene = [
        Control("plain", "Edit", "Notes", "", Rect(0, 0, 100, 20), "Notepad", ()),
    ]
    session.observe()
    r = session.act({"kind": "set_value", "ref": "plain", "value": "hi"})
    assert r["ok"] and r["route"] == "pixel+type"
    assert [c[0] for c in session.backend.calls[-3:]] == ["click", "key", "type"]


def test_scroll_on_the_window_uses_the_window_centre(session):
    session.task_start("x", ["Notepad"])
    session.observe()
    assert session.act({"kind": "scroll", "ref": "window", "dy": -3})["ok"]
    assert session.backend.calls[-1] == ("scroll", (400, 300), -3)


def test_a_backend_failure_is_recorded_not_raised(session):
    session.task_start("x", ["Notepad"])
    session.observe()

    def boom(control):
        raise HandsError("elevated_target", "that window runs elevated")

    session.backend.invoke = boom
    r = session.act({"kind": "invoke", "ref": "c1"})
    assert r["ok"] is False and r["error"].startswith("elevated_target")
    assert session.ledger.steps()[-1]["outcome"] == "error"
    assert session.step_index == 1  # a failed step still costs budget


def test_raw_coordinates_are_refused(session):
    session.task_start("x", ["Notepad"])
    session.observe()
    r = session.act({"kind": "click", "x": 10, "y": 10})
    assert r["ok"] is False and r["error"].startswith("invalid_action")
    assert session.ledger.steps() == []  # never became a step


def test_task_end_closes_the_ledger_and_resets(session):
    session.task_start("x", ["Notepad"])
    directory = session.ledger.dir
    end = session.task_end("done", "all good")
    assert end["steps"] == 0 and session.task_id is None and session.ledger is None
    assert session.status()["last_task"]["outcome"] == "done"
    assert (directory / "task.json").exists()
    with pytest.raises(HandsError) as ei:
        session.task_end("done")
    assert ei.value.code == "no_task"
