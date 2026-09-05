"""Unit tests for `Browser`, driven entirely through a `FakeTransport` that
records every `(method, params, session)` and returns canned CDP-shaped
replies. None of this touches a real browser — that is the live check run by
hand on a machine with Chrome/Edge installed (see the task report) — so these
tests exist to prove `Browser` builds the right protocol calls in the right
order, and in particular that a click or fill never acts on a caller-supplied
point: only on the rect the (fake) DOM probe just reported.
"""
from __future__ import annotations

import base64
import io
import json

import pytest
from PIL import Image

from firekeep_hands.backends.base import HandsError
from firekeep_hands.browser import Browser


def _one_pixel_png_b64() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), color=(200, 0, 0)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class FakeTransport:
    """Stands in for `CdpTransport`. `Runtime.evaluate` inspects the
    `const __hands = {...};` prefix `Browser` prepends to the DOM probe
    source, so it can answer `find`/`focus` calls without ever running the
    real probe JS (that is `test_dom_probe.py`'s job)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str | None]] = []
        self.closed = False
        self.attach_session = "S1"

        self.target_infos = [
            {"targetId": "T1", "type": "page", "url": "about:blank", "title": "New Tab"},
        ]
        self.tab_info = {"targetId": "T1", "url": "https://example.com", "title": "Example Domain"}
        self.read_text = "hello world"
        self.find_controls = [
            {"ref": "d1", "role": "button", "name": "Sign in", "value": "",
             "rect": [10, 20, 100, 40], "href": ""},
        ]
        # ref -> rect, for the fake "focus" op that click()/fill() call to
        # get a fresh rect / prove the ref still resolves.
        self.known_refs = {"d1": [10, 20, 100, 40], "d2": [5, 5, 200, 30]}
        self.wait_event_result: dict = {}

    def send(self, method: str, params: dict | None = None, *, session: str | None = None,
              timeout: float = 10.0) -> dict:
        params = params or {}
        self.calls.append((method, dict(params), session))
        if method == "Target.getTargets":
            return {"targetInfos": self.target_infos}
        if method == "Target.getTargetInfo":
            return {"targetInfo": self.tab_info}
        if method == "Page.navigate":
            return {"frameId": "f1"}
        if method == "Runtime.evaluate":
            expr = params.get("expression", "")
            marker = "window.__hands = "
            if marker in expr:
                payload = json.loads(expr.split(marker, 1)[1].split(";\n", 1)[0])
                return {"result": {"value": self._probe_response(payload)}}
            return {"result": {"value": self.read_text}}
        if method == "Page.captureScreenshot":
            return {"data": _one_pixel_png_b64()}
        if method in ("Input.dispatchMouseEvent", "Input.insertText", "Page.enable"):
            return {}
        raise AssertionError(f"FakeTransport got an unexpected method: {method}")

    def _probe_response(self, payload: dict) -> dict:
        op = payload.get("op")
        if op in ("find", "scan"):
            return {"controls": self.find_controls, "truncated": False}
        if op == "focus":
            rect = self.known_refs.get(payload.get("ref"))
            return {"ok": rect is not None, "rect": rect}
        raise AssertionError(f"FakeTransport got an unexpected probe op: {op!r}")

    def wait_event(self, name: str, *, session: str | None, timeout: float) -> dict | None:
        self.calls.append(("wait_event:" + name, {"timeout": timeout}, session))
        return self.wait_event_result

    def attach(self, target_id: str) -> str:
        self.calls.append(("attach", {"targetId": target_id}, None))
        return self.attach_session

    def close(self) -> None:
        self.closed = True

    # -- assertion helpers --------------------------------------------------

    def calls_named(self, method: str) -> list[tuple[dict, str | None]]:
        return [(params, session) for called, params, session in self.calls if called == method]


@pytest.fixture
def fake() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def browser(fake: FakeTransport) -> Browser:
    return Browser(fake)


def test_open_launches_nothing_when_a_transport_was_given_and_attaches_first_tab(
    browser: Browser, fake: FakeTransport
) -> None:
    result = browser.open()

    assert result == {"tabs": [{"id": "T1", "url": "about:blank", "title": "New Tab"}]}
    assert fake.calls_named("attach") == [({"targetId": "T1"}, None)]
    # Without this, Page.loadEventFired never arrives and every navigate()
    # burns its full wait_event timeout for nothing (see browser.py).
    assert fake.calls_named("Page.enable") == [({}, "S1")]


def test_tabs_lists_page_targets_only(browser: Browser, fake: FakeTransport) -> None:
    fake.target_infos.append(
        {"targetId": "W1", "type": "worker", "url": "blob:", "title": ""}
    )

    assert browser.tabs() == [{"id": "T1", "url": "about:blank", "title": "New Tab"}]


def test_navigate_sends_page_navigate_then_waits_for_load(
    browser: Browser, fake: FakeTransport
) -> None:
    result = browser.navigate("https://example.com")

    navigate_calls = fake.calls_named("Page.navigate")
    assert navigate_calls == [({"url": "https://example.com"}, "S1")]
    wait_calls = fake.calls_named("wait_event:Page.loadEventFired")
    assert len(wait_calls) == 1
    assert wait_calls[0][1] == "S1"
    # Page.navigate must be sent BEFORE the wait, not after.
    assert [c[0] for c in fake.calls].index("Page.navigate") < \
        [c[0] for c in fake.calls].index("wait_event:Page.loadEventFired")
    assert result == {"url": "https://example.com", "title": "Example Domain"}


def test_read_returns_url_title_and_trimmed_text(browser: Browser, fake: FakeTransport) -> None:
    fake.read_text = "hello world"

    result = browser.read(budget=5)

    assert result == {"url": "https://example.com", "title": "Example Domain", "text": "hello"}


def test_find_returns_probe_controls(browser: Browser, fake: FakeTransport) -> None:
    matches = browser.find("sign in")

    assert matches == fake.find_controls
    evaluate_calls = fake.calls_named("Runtime.evaluate")
    assert len(evaluate_calls) == 1
    assert evaluate_calls[0][1] == "S1"


def test_find_respects_limit(browser: Browser, fake: FakeTransport) -> None:
    fake.find_controls = [
        {"ref": f"d{i}", "role": "button", "name": f"item {i}", "value": "",
         "rect": [0, 0, 10, 10], "href": ""}
        for i in range(5)
    ]

    assert len(browser.find("item", limit=2)) == 2


def test_click_dispatches_mouse_events_at_the_probes_rect_centre(
    browser: Browser, fake: FakeTransport
) -> None:
    browser.click("d1")

    pressed = fake.calls_named("Input.dispatchMouseEvent")
    kinds = {params["type"]: params for params, _session in pressed}
    assert "mousePressed" in kinds and "mouseReleased" in kinds
    for params in (kinds["mousePressed"], kinds["mouseReleased"]):
        assert params["x"] == 60  # 10 + 100/2
        assert params["y"] == 40  # 20 + 40/2
        assert params["button"] == "left"
    assert all(session == "S1" for _p, session in pressed)


def test_click_ignores_any_caller_supplied_point(browser: Browser, fake: FakeTransport) -> None:
    """`click` takes only a ref — there is no coordinate parameter to trust
    in the first place, and this asserts the dispatched point always comes
    from the probe's rect for that ref, not from anything else in scope."""
    fake.known_refs["d1"] = [100, 200, 20, 20]  # moved since the last scan

    browser.click("d1")

    pressed = fake.calls_named("Input.dispatchMouseEvent")
    kinds = {params["type"]: params for params, _session in pressed}
    assert kinds["mousePressed"]["x"] == 110
    assert kinds["mousePressed"]["y"] == 210


def test_click_unknown_ref_raises_stale_ref(browser: Browser) -> None:
    with pytest.raises(HandsError) as excinfo:
        browser.click("d999")
    assert excinfo.value.code == "stale_ref"


def test_fill_focuses_then_inserts_text(browser: Browser, fake: FakeTransport) -> None:
    browser.fill("d2", "hello")

    evaluate_calls = fake.calls_named("Runtime.evaluate")
    assert len(evaluate_calls) == 1
    insert_calls = fake.calls_named("Input.insertText")
    assert insert_calls == [({"text": "hello"}, "S1")]
    # focus must happen before the text is inserted
    order = [c[0] for c in fake.calls]
    assert order.index("Runtime.evaluate") < order.index("Input.insertText")


def test_fill_unknown_ref_raises_stale_ref(browser: Browser) -> None:
    with pytest.raises(HandsError) as excinfo:
        browser.fill("d999", "x")
    assert excinfo.value.code == "stale_ref"


def test_screenshot_returns_png_bytes(browser: Browser) -> None:
    png = browser.screenshot(max_width=1280)

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    image = Image.open(io.BytesIO(png))
    assert image.format == "PNG"
    assert image.width <= 1280


def test_screenshot_downscales_to_max_width(browser: Browser, fake: FakeTransport) -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (2000, 1000), color=(0, 0, 0)).save(buffer, format="PNG")
    original_b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
    original_send = fake.send

    def send_with_big_screenshot(method, params=None, *, session=None, timeout=10.0):
        if method == "Page.captureScreenshot":
            fake.calls.append((method, dict(params or {}), session))
            return {"data": original_b64}
        return original_send(method, params, session=session, timeout=timeout)

    fake.send = send_with_big_screenshot  # type: ignore[method-assign]

    png = browser.screenshot(max_width=400)
    image = Image.open(io.BytesIO(png))
    assert image.width == 400
    assert image.height == 200  # aspect ratio preserved


def test_current_url(browser: Browser) -> None:
    assert browser.current_url() == "https://example.com"


def test_close_closes_the_transport(browser: Browser, fake: FakeTransport) -> None:
    browser.close()
    assert fake.closed is True


def test_ensure_transport_launches_when_none_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Browser(transport=None)` must launch on first use, with the
    configured `kind` and the dedicated Hands Chrome profile directory —
    never a bare default profile that could carry other logins."""
    from firekeep_hands import paths

    launched: dict = {}

    class _StubTransport(FakeTransport):
        pass

    def fake_launch(cls, kind, profile_dir):
        launched["kind"] = kind
        launched["profile_dir"] = profile_dir
        return _StubTransport()

    monkeypatch.setattr("firekeep_hands.browser.CdpTransport.launch", classmethod(fake_launch))

    browser = Browser(kind="chrome")
    browser.open()

    assert launched["kind"] == "chrome"
    assert launched["profile_dir"] == paths.chrome_profile_dir()
