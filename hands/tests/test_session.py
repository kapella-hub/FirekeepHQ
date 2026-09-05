"""`HandsSession` — the state machine every MCP tool call goes through.

The doubles here are deliberately *real* where the safety property lives: the
fake broker client wraps an actual `PermitStore`, so "approve, consume, one
use only, expiry" is exercised by the same code the running broker uses
rather than by a stub that returns whatever the test wants. Only the
transport (HTTP), the Keep (cortex/relay) and the platform backend are faked.
"""
from __future__ import annotations

import json
from pathlib import Path

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
from firekeep_hands.config import HandsConfig, Policy, Remembered
from firekeep_hands import session as session_module
from firekeep_hands.evidence import Ledger
from firekeep_hands.keep import KeepDecision
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

    def __init__(self, *, acquired: bool = True, decision: KeepDecision | None = None):
        self._acquired = acquired
        # What relay/app/leases.py sends back on a lost race.
        self.lease_extra = {"held_by": "agent-7", "expires_in": 900}
        # What cortex said about the task. The real KeepLink sets this from
        # the ActionBeforeResponse; None means it never answered.
        self.last_decision = decision or KeepDecision(None)
        self.holds = False
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

    def acquire_lease(self, ttl_minutes: int = 30, reclaim_own: bool = True):
        self.leases.append(ttl_minutes)
        reply = {"acquired": self._acquired, "fencing_token": 7}
        if self._acquired:
            self.holds = True
        else:
            reply.update(self.lease_extra)
        return reply

    def renew_lease(self):
        self.renewed += 1

    def release_lease(self) -> bool:
        """Mirrors the real one: nothing to release unless we hold it, and the
        answer says which of the two happened."""
        if not self.holds:
            return False
        self.holds = False
        self.released = True
        return True


def _page_scene() -> list[dict]:
    """What the DOM probe reports for a small page. `role: "password"` is the
    probe's own spelling for `input[type=password]`, and its value is always
    blank — the probe never reads a password field's contents."""
    return [
        {"ref": "g1-d1", "role": "button", "name": "Save draft", "value": "",
         "rect": [0, 0, 80, 20], "href": ""},
        {"ref": "g1-d2", "role": "password", "name": "Password", "value": "",
         "rect": [0, 30, 200, 24], "href": ""},
        {"ref": "g1-d3", "role": "button", "name": "Place order", "value": "",
         "rect": [0, 60, 120, 30], "href": ""},
        {"ref": "g1-d4", "role": "a", "name": "", "value": "",
         "rect": [0, 100, 60, 16], "href": "/account/delete"},
    ]


class FakeBrowser:
    """Records operations against a fixed page scene. Refs are
    generation-stamped (`g<gen>-d<N>`) and only what is in the scene is live,
    so anything else raises `stale_ref` the way the DOM probe does."""

    def __init__(self, scene: list[dict] | None = None, page: dict | None = None):
        self.calls: list[tuple] = []
        self.scene = _page_scene() if scene is None else list(scene)
        self.page = dict(page or {"url": "https://shop.example/cart", "title": "Cart — Shop"})
        self.loaded = True

    @property
    def live(self) -> set[str]:
        return {c["ref"] for c in self.scene}

    def _tab(self) -> dict:
        return {"id": "t1", "url": self.page["url"], "title": self.page["title"]}

    def open(self) -> dict:
        self.calls.append(("open",))
        return {"tabs": [self._tab()]}

    def tabs(self) -> list[dict]:
        self.calls.append(("tabs",))
        return [self._tab()]

    def navigate(self, url, *, tab=None) -> dict:
        self.calls.append(("navigate", url))
        self.page = {"url": url, "title": "Page"}
        return {"url": url, "title": "Page", "loaded": self.loaded}

    def read(self, *, tab=None, budget=4000) -> dict:
        self.calls.append(("read",))
        return {"url": self.page["url"], "title": self.page["title"], "text": "hello"[:budget]}

    def find(self, query, *, tab=None, limit=10) -> list[dict]:
        self.calls.append(("find", query))
        needle = query.lower()
        return [c for c in self.scene
                if needle in c["name"].lower() or needle in c["href"].lower()][:limit]

    def click(self, ref, *, tab=None) -> None:
        if ref not in self.live:
            raise HandsError("stale_ref", f"{ref} no longer exists (find again)")
        self.calls.append(("click", ref))

    def fill(self, ref, text, *, tab=None) -> None:
        if ref not in self.live:
            raise HandsError("stale_ref", f"{ref} no longer exists (find again)")
        self.calls.append(("fill", ref, text))

    def screenshot(self, *, tab=None, max_width=1280) -> bytes:
        self.calls.append(("screenshot", tab))
        return b"\x89PNG\r\n\x1a\nbrowser"

    def current_url(self, tab=None) -> str:
        self.calls.append(("current_url", tab))
        return self.page["url"]


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
    # The human needs to know who has it and how long to wait, not just "no".
    message = str(ei.value)
    assert "hands is leased by agent-7 until 20" in message
    assert "wait for it to lapse or end that session" in message


def test_the_lease_refusal_degrades_when_relay_says_less():
    link = FakeLink(acquired=False)
    link.lease_extra = {}
    s = build_session("Notepad", link=link)
    with pytest.raises(HandsError) as ei:
        s.task_start("x", ["Notepad"])
    assert str(ei.value).startswith("hands is leased by another session — ")


def test_a_keep_block_stops_the_task_and_gives_the_machine_back():
    """The Keep gets a say in whether a task starts. A block that left the
    lease held would lock the machine for half an hour over a task that never
    ran."""
    link = FakeLink(decision=KeepDecision("block", "this looks like the incident from Tuesday"))
    s = build_session("Notepad", link=link)
    with pytest.raises(HandsError) as ei:
        s.task_start("do the thing", ["Notepad"])
    assert ei.value.code == "blocked"
    assert str(ei.value) == "this looks like the incident from Tuesday"
    assert link.released is True
    assert s.task_id is None and s.ledger is None
    assert link.after == []  # cortex refused it; there is no outcome to reconcile


@pytest.mark.parametrize("decision", [
    KeepDecision(None),                       # offline, or no answer at all
    KeepDecision("allow"),
    KeepDecision("rethink", "are you sure?"),  # advisory, not a refusal
])
def test_anything_short_of_an_explicit_block_proceeds(decision):
    link = FakeLink(decision=decision)
    s = build_session("Notepad", link=link)
    assert s.task_start("x", ["Notepad"])["ok"] is True
    assert s.task_id is not None and link.released is False


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
    # Someone reading this is deciding how to set approvals up, which is
    # exactly when the phone opt-in is worth naming.
    assert "phone_approvals true" in st["approvals"]
    assert "docs/guides/hands.md" in st["approvals"]


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
    assert ok["ok"] and ("navigate", "https://evil.example/x") in browser.calls
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
    assert session.browser_op("find", query="Save")["controls"][0]["ref"] == "g1-d1"
    assert session.browser_op("click", ref="g1-d1")["ok"]
    assert session.browser_op("read")["text"] == "hello"
    assert session.browser_op("screenshot")["screenshot_png"].startswith(b"\x89PNG")
    steps = session.ledger.steps()
    assert len(steps) == 5 and {s["route"] for s in steps} == {"browser"}
    assert session.step_index == 5


def test_a_browser_ref_this_session_never_scanned_is_refused_unclassified(session):
    """No descriptor means no classifier, and running it anyway would be the
    one way a web button could reach the page ungated. Refused before the
    browser is touched, so it never becomes a step."""
    session.browser = FakeBrowser()
    session.task_start("x", ["Notepad"])
    r = session.browser_op("click", ref="g0-d1")
    assert r["ok"] is False and r["error"].startswith("stale_ref")
    assert session.ledger.steps() == []
    assert session.browser.calls == []


def test_a_ref_the_page_has_moved_on_from_is_a_ledgered_error(session):
    """The other staleness: this session has the descriptor, but the page
    itself has changed under it. That one reaches the browser and comes back
    as a failed step."""
    browser = FakeBrowser()
    session.browser = browser
    session.task_start("x", ["Notepad"])
    session.browser_op("find", query="Save")
    browser.scene = []  # the page moved on between the scan and the click
    r = session.browser_op("click", ref="g1-d1")
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


def test_a_web_button_is_classified_like_a_native_one(session, store):
    """A "Place order" drawn in a page is the same decision as one drawn by
    an application; the surface is not a reason to ask the human less often."""
    browser = FakeBrowser()
    session.browser = browser
    session.task_start("x", ["Notepad"])
    session.browser_op("find", query="order")
    r = session.browser_op("click", ref="g1-d3")
    assert r["ok"] is False and r["needs_permit"]["classes"] == ["money"]
    # Names the SITE, not "in browser": which shop is about to be ordered
    # from is the whole question the human is being asked.
    assert r["needs_permit"]["title"] == 'click "Place order" on shop.example'
    assert ("click", "g1-d3") not in browser.calls  # never dispatched

    ch = r["needs_permit"]["challenge"]
    store.decide(ch, "approve", via="chord")
    ok = session.browser_op("click", ref="g1-d3", permit=ch)
    assert ok["ok"] and ok["classes"] == ["money"]
    # Bracketed by the before/after evidence pair, so not the last call.
    assert ("click", "g1-d3") in browser.calls
    assert session.ledger.steps()[-1]["permit"] == {"challenge": ch, "via": "chord"}


def test_filling_a_password_input_is_a_credential_step(session, store):
    browser = FakeBrowser()
    session.browser = browser
    session.task_start("x", ["Notepad"])
    session.browser_op("find", query="password")
    r = session.browser_op("fill", ref="g1-d2", text="hunter2")
    assert r["ok"] is False and r["needs_permit"]["classes"] == ["credential"]
    assert "hunter2" not in r["needs_permit"]["title"]

    ch = r["needs_permit"]["challenge"]
    store.decide(ch, "approve", via="chord")
    assert session.browser_op("fill", ref="g1-d2", text="hunter2", permit=ch)["ok"]
    assert ("fill", "g1-d2", "hunter2") in browser.calls  # the real text runs
    line = session.ledger.steps()[-1]
    assert line["action"]["text"] == "<redacted:credential>"  # but is never stored


def test_an_ordinary_web_click_needs_no_permit(session):
    browser = FakeBrowser()
    session.browser = browser
    session.task_start("x", ["Notepad"])
    session.browser_op("find", query="Save")
    r = session.browser_op("click", ref="g1-d1")
    assert r["ok"] is True and r["classes"] == []
    assert ("screenshot", None) not in browser.calls  # unprotected: no evidence images
    assert session.ledger.steps()[-1]["before"] is None


def test_a_protected_browser_step_is_photographed_through_the_browser(session, store):
    """The page is what the human approved. A desktop grab would show
    whatever happened to be in front on a machine where the browser is not."""
    browser = FakeBrowser()
    session.browser = browser
    session.task_start("x", ["Notepad"])
    session.browser_op("find", query="order")
    ch = session.browser_op("click", ref="g1-d3")["needs_permit"]["challenge"]
    store.decide(ch, "approve", via="chord")
    assert session.browser_op("click", ref="g1-d3", permit=ch)["ok"]

    line = session.ledger.steps()[-1]
    assert line["before"] and line["after"]
    assert (session.ledger.dir / f"{line['step_index']:03d}-before.png").exists()
    assert browser.calls.count(("screenshot", None)) == 2


def test_the_evidence_pair_is_taken_on_the_tab_the_step_targets(session, store):
    """A protected click on a named tab must be photographed on that tab, not
    on whichever one happens to be current."""
    browser = FakeBrowser()
    session.browser = browser
    session.task_start("x", ["Notepad"])
    session.browser_op("find", query="order", tab="t7")
    ch = session.browser_op("click", ref="g1-d3", tab="t7")["needs_permit"]["challenge"]
    store.decide(ch, "approve", via="chord")
    assert session.browser_op("click", ref="g1-d3", tab="t7", permit=ch)["ok"]
    assert browser.calls.count(("screenshot", "t7")) == 2
    assert ("screenshot", None) not in browser.calls


def test_the_site_named_in_a_permit_is_the_one_the_page_is_on(session):
    """A find is often the only browser call before a click, and the DOM probe
    reports no URL — so the session asks the browser where it is."""
    browser = FakeBrowser(page={"url": "https://bank.example/transfer", "title": "Transfer"})
    session.browser = browser
    session.task_start("x", ["Notepad"])
    session.browser_op("find", query="order")
    assert ("current_url", None) in browser.calls
    r = session.browser_op("click", ref="g1-d3")
    assert r["needs_permit"]["title"] == 'click "Place order" on bank.example'


def test_a_protected_navigation_is_photographed_too(session, store):
    browser = FakeBrowser()
    session.browser = browser
    session.task_start("x", ["Notepad"])
    r = session.browser_op("navigate", url="https://evil.example/x")
    ch = r["needs_permit"]["challenge"]
    store.decide(ch, "approve", via="chord")
    assert session.browser_op("navigate", url="https://evil.example/x", permit=ch)["ok"]
    line = session.ledger.steps()[-1]
    assert line["before"] and line["after"]
    assert browser.calls.count(("screenshot", None)) == 2


def test_an_unlabelled_link_is_judged_by_where_it_goes(session):
    session.browser = FakeBrowser()
    session.task_start("x", ["Notepad"])
    session.browser_op("find", query="/account")
    r = session.browser_op("click", ref="g1-d4")
    assert r["ok"] is False and r["needs_permit"]["classes"] == ["destroy"]


def test_a_navigation_drops_the_page_scan(session):
    """A new document means new refs. The probe's generation counter resets
    on navigation, so a ref from the old page must not be classified against
    the old page's description of it."""
    session.browser = FakeBrowser()
    session.policy.domains.append("shop.example")
    session.task_start("x", ["Notepad"])
    session.browser_op("find", query="Save")
    assert session.browser_op("navigate", url="https://shop.example/other")["ok"]
    r = session.browser_op("click", ref="g1-d1")
    assert r["ok"] is False and r["error"].startswith("stale_ref")


def test_the_page_title_is_what_the_classifier_reads_not_the_url(session):
    """A URL is a path, not a description. Reading documentation about
    `remove` must not put every click behind an approval."""
    browser = FakeBrowser(
        scene=[{"ref": "g1-d1", "role": "button", "name": "Run it", "value": "",
                "rect": [0, 0, 60, 20], "href": ""}],
        page={"url": "https://docs.example/library/os.html#os.remove",
              "title": "os — operating system interfaces"},
    )
    session.browser = browser
    session.task_start("x", ["Notepad"])
    session.browser_op("read")
    session.browser_op("find", query="Run")
    assert session.browser_op("click", ref="g1-d1")["ok"] is True

    browser.page = {"url": "https://shop.example/x", "title": "Confirm payment"}
    session.browser_op("read")
    session.browser_op("find", query="Run")
    r = session.browser_op("click", ref="g1-d1")
    assert r["ok"] is False and r["needs_permit"]["classes"] == ["money"]


def test_unknown_browser_op_is_refused(session):
    session.browser = FakeBrowser()
    session.task_start("x", ["Notepad"])
    r = session.browser_op("eval", text="alert(1)")
    assert r["ok"] is False and r["error"].startswith("invalid_action")


def test_browser_ops_need_an_open_task(session):
    session.browser = FakeBrowser()
    r = session.browser_op("open")
    assert r["ok"] is False and r["error"].startswith("no_task")


# -- the focus hint, and what the ledger keeps -----------------------------


def _password_scene(app: str) -> list[Control]:
    return [
        Control("pw", "PasswordBox", "Password", "", Rect(0, 0, 200, 24), app, ("Invoke", "Value")),
        Control("note", "Edit", "Notes", "", Rect(0, 40, 200, 24), app, ("Value",)),
    ]


def test_typing_after_clicking_a_password_box_is_a_credential_step(session, store):
    """`type` carries no ref, so without the focus hint the classifier has
    nothing to look at and a typed password is unprotected."""
    session.backend.scene = _password_scene("Notepad")
    session.task_start("x", ["Notepad"])
    session.observe()
    assert session.act({"kind": "invoke", "ref": "pw"})["ok"]
    r = session.act({"kind": "type", "text": "hunter2"})
    assert r["ok"] is False and r["needs_permit"]["classes"] == ["credential"]


def test_the_focus_hint_is_dropped_when_focus_leaves_for_another_app(session):
    session.backend.scene = _password_scene("Notepad")
    session.task_start("x", ["Notepad"])
    session.observe()
    session.act({"kind": "invoke", "ref": "pw"})
    session.act({"kind": "focus_app", "app": "Notepad"})
    assert session.act({"kind": "type", "text": "hunter2"})["ok"] is True


def test_typing_into_an_ordinary_field_is_not_a_credential_step(session):
    session.backend.scene = _password_scene("Notepad")
    session.task_start("x", ["Notepad"])
    session.observe()
    session.act({"kind": "invoke", "ref": "note"})
    assert session.act({"kind": "type", "text": "shopping list"})["ok"] is True


def test_a_typed_secret_never_reaches_the_ledger(session, store):
    session.backend.scene = _password_scene("Notepad")
    session.task_start("x", ["Notepad"])
    session.observe()
    session.act({"kind": "invoke", "ref": "pw"})
    ch = session.act({"kind": "type", "text": "hunter2"})["needs_permit"]["challenge"]
    store.decide(ch, "approve", via="chord")
    assert session.act({"kind": "type", "text": "hunter2"}, permit=ch)["ok"]

    assert session.backend.calls[-1] == ("type", "hunter2")  # the real text runs
    line = session.ledger.steps()[-1]
    assert line["action"]["text"] == "<redacted:credential>"
    assert "hunter2" not in (session.ledger.dir / "steps.jsonl").read_text(encoding="utf-8")


def test_a_remembered_allowance_stops_the_asking_but_not_the_redaction(session):
    """`decide` drops a remembered class, so the credential class is gone by
    the time the ledger line is written. The role is still a password box and
    the text is still a secret."""
    session.backend.scene = _password_scene("Notepad")
    session.policy.remembered.append(
        Remembered("credential", "Notepad", "password", "2099-01-01T00:00:00Z"))
    session.task_start("x", ["Notepad"])
    session.observe()
    r = session.act({"kind": "set_value", "ref": "pw", "value": "hunter2"})
    assert r["ok"] is True and r["classes"] == []
    assert session.backend.values["pw"] == "hunter2"
    assert session.ledger.steps()[-1]["action"]["value"] == "<redacted:credential>"


def test_an_ordinary_typed_string_is_kept_verbatim(session):
    session.task_start("x", ["Notepad"])
    session.act({"kind": "clipboard_set", "text": "just a note"})
    assert session.ledger.steps()[-1]["action"]["text"] == "just a note"


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


def test_an_exception_the_backend_never_declared_is_still_a_recorded_step(session, store):
    """`uiautomation` raises `comtypes.COMError` straight out of a pattern
    call. Letting one past the recorder would leave a consumed permit, an
    action that may well have run, no ledger line, an uncounted step and refs
    that were never invalidated."""
    session.backend.scene = _password_scene("Mail")
    session.task_start("x", ["Mail"])
    session.observe()

    def boom(control):
        raise RuntimeError("COMError -2147220991")

    session.backend.invoke = boom
    r = session.act({"kind": "invoke", "ref": "pw"})
    assert r["ok"] is False and r["error"].startswith("backend: ")
    assert session.ledger.steps()[-1]["outcome"] == "error"
    assert session.step_index == 1
    assert session.last_obs is None


def test_a_ledger_that_cannot_be_written_still_counts_the_step(session):
    session.task_start("x", ["Notepad"])

    def boom(**_kwargs):
        raise OSError("no space left on device")

    session.ledger.record = boom
    r = session.act({"kind": "wait", "seconds": 0})
    assert r["ok"] is True and "not recorded" in r["evidence_error"]
    assert session.step_index == 1


def test_a_browser_failure_the_transport_never_declared_is_recorded(session):
    browser = FakeBrowser()
    session.browser = browser
    session.task_start("x", ["Notepad"])

    def boom(**_kwargs):
        raise RuntimeError("the websocket closed")

    browser.tabs = boom
    r = session.browser_op("tabs")
    assert r["ok"] is False and r["error"].startswith("backend: ")
    assert session.ledger.steps()[-1]["outcome"] == "error"
    assert session.step_index == 1


@pytest.mark.parametrize("action", [
    {"kind": "scroll", "ref": "window", "dy": "3"},
    {"kind": "wait", "seconds": "3"},
    {"kind": "type", "text": 7},
    {"kind": "key", "chord": ["ctrl", "enter"]},
    {"kind": "invoke", "ref": 1},
    {"kind": "open_url", "url": None},
    {"kind": "scroll", "ref": "window", "dy": True},
])
def test_a_field_of_the_wrong_type_never_reaches_the_backend(session, action):
    session.task_start("x", ["Notepad"])
    session.observe()
    r = session.act(action)
    assert r["ok"] is False and r["error"].startswith("invalid_action")
    assert session.backend.calls == []
    assert session.ledger.steps() == []


@pytest.mark.parametrize("action", [
    ["kind", "wait"],                       # a list, not an object
    "wait",
    42,
    None,
    {"kind": {"nested": "object"}},         # unhashable: broke the lookup
    {"kind": 7},
    {"kind": None},
    {},
])
def test_a_malformed_action_envelope_is_refused_not_crashed(session, action):
    """`hands_act` declares `action` as a bare object, so a client can send
    anything. Every one of these used to surface as `backend: …`, which tells
    a model its machine broke rather than that it sent nonsense."""
    session.task_start("x", ["Notepad"])
    r = session.act(action)
    assert r["ok"] is False and r["error"].startswith("invalid_action")
    assert session.backend.calls == [] and session.ledger.steps() == []


def test_the_keep_block_path_resets_even_when_the_cleanup_faults():
    """A raising `ledger.close` must not leave the session believing a task
    is open moments after the lease behind it was handed back."""
    link = FakeLink(decision=KeepDecision("block", "no"))
    s = build_session("Notepad", link=link)
    original = Ledger.close

    def boom(self, *_args, **_kwargs):
        raise OSError("read-only file system")

    Ledger.close = boom
    try:
        with pytest.raises(HandsError) as ei:
            s.task_start("x", ["Notepad"])
    finally:
        Ledger.close = original
    assert ei.value.code == "blocked"
    assert s.task_id is None and s.ledger is None
    assert link.released is True


def test_task_end_releases_even_when_telling_the_keep_fails(session):
    """Reconciling with the Keep is bookkeeping. Bookkeeping must never be
    what decides whether the human gets their machine back."""
    session.task_start("x", ["Notepad"])

    def boom(*_args, **_kwargs):
        raise RuntimeError("cortex exploded")

    session.link.action_after = boom
    with pytest.raises(RuntimeError):
        session.task_end("done", "ok")
    assert session.link.released is True
    assert session.task_id is None


def test_perception_budgets_are_clamped_not_merely_defaulted(session):
    session.task_start("x", ["Notepad"])
    session.observe(max_nodes=1_000_000)
    assert session.backend.observes[-1]["max_nodes"] == session.config.max_nodes
    session.find("save", limit=1_000_000)
    session.observe(max_nodes=2)
    assert session.backend.observes[-1]["max_nodes"] == 2
    with pytest.raises(HandsError) as ei:
        session.observe(max_nodes="lots")
    assert ei.value.code == "invalid_action"


def test_task_end_releases_the_machine_even_when_the_ledger_will_not_close(session):
    """A read-only disk must not strand the lease for its full half hour and
    lock the human out of their own desktop."""
    session.task_start("x", ["Notepad"])
    session.act({"kind": "wait", "seconds": 0})

    def boom(_outcome, _summary):
        raise OSError("read-only file system")

    session.ledger.close = boom
    end = session.task_end("done", "ok")
    assert end["ok"] is True and end["steps"] == 1
    assert "did not close" in end["evidence_error"]
    assert session.link.released is True and session.link.after == [("A1", "done", "ok")]
    assert session.task_id is None


def test_a_model_supplied_app_name_is_never_shown_as_an_observed_control(session, broker):
    """`invoke "Send" in Mail` means Hands saw that control. A string the
    runtime chose gets its own shape so the human can tell them apart."""
    session.task_start("x", ["Notepad"])
    session.act({"kind": "open_app", "app": 'Notepad" in Mail'})
    title = broker.requested[-1][1]
    assert title == 'open the app the runtime asked for: "Notepad" in Mail"'
    assert not title.startswith("invoke ")


def test_a_browser_payload_cannot_overwrite_the_step_result(session):
    browser = FakeBrowser()
    session.browser = browser
    session.task_start("x", ["Notepad"])
    browser.read = lambda **_kw: {"url": "u", "title": "t", "text": "hi",
                                  "ok": False, "error": "injected", "step_index": 99}
    r = session.browser_op("read")
    assert r["ok"] is True and r["error"] is None and r["step_index"] == 0
    assert r["text"] == "hi"


def test_raw_coordinates_are_refused(session):
    session.task_start("x", ["Notepad"])
    session.observe()
    r = session.act({"kind": "click", "x": 10, "y": 10})
    assert r["ok"] is False and r["error"].startswith("invalid_action")
    assert session.ledger.steps() == []  # never became a step


def test_abandon_closes_an_open_task_and_gives_the_machine_back(session):
    """What the server runs on its way out. A run that ends any way other
    than hands_task_end used to leave the lease held for its full TTL, and
    the next run on this machine was refused by its own dead predecessor."""
    session.task_start("x", ["Notepad"])
    session.act({"kind": "wait", "seconds": 0})
    closed = session.abandon()
    assert closed["outcome"] == "abandoned" and closed["steps"] == 1
    assert session.link.released is True
    assert session.link.after == [("A1", "abandoned", "server shut down with the task open")]
    assert session.task_id is None
    assert json.loads((Path(closed["evidence"]) / "task.json").read_text(
        encoding="utf-8"))["outcome"] == "abandoned"


def test_task_end_says_whether_the_lease_actually_came_back(session):
    """"not held" is the normal answer on a machine with no Keep, and a
    caller reporting a shutdown must not claim a release it never made."""
    session.task_start("x", ["Notepad"])
    assert session.task_end("done", "ok")["lease"] == "released"

    session.task_start("y", ["Notepad"])
    session.link.holds = False  # relay never really gave it to us
    assert session.task_end("done", "ok")["lease"] == "not held"


def test_a_failed_abandon_names_the_task_it_failed_for(session, monkeypatch):
    """`task_end` clears `task_id` in its own finally, so reading it in the
    handler logged "abandon failed for None"."""
    session.task_start("x", ["Notepad"])
    task_id = session.task_id
    logged: list[str] = []
    monkeypatch.setattr(session_module.hooklog, "log_failure",
                        lambda hook, message, exc=None: logged.append(message))

    def boom(*_args, **_kwargs):
        raise RuntimeError("cortex exploded")

    session.link.action_after = boom
    assert session.abandon() is None
    assert any(task_id in message for message in logged)
    assert not any("for None" in message for message in logged)


def test_abandon_with_no_task_open_does_nothing(session):
    assert session.abandon() is None
    assert session.link.released is False


def test_abandon_still_releases_the_lease_when_the_close_path_blows_up(session):
    """`task_end` gives up on the Keep before it reaches `release_lease`, so
    the teardown has to finish the job itself. The lease is the whole point
    of this method."""
    session.task_start("x", ["Notepad"])

    def boom(*_args, **_kwargs):
        raise RuntimeError("cortex exploded")

    session.link.action_after = boom
    assert session.abandon() is None
    assert session.link.released is True
    assert session.task_id is None


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
