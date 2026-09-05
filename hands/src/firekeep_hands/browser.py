"""The Hands-managed browser: one Chrome or Edge instance, launched lazily
against Hands' own profile (`paths.chrome_profile_dir()` — no logins until a
human signs in through Hands) and driven entirely over `CdpTransport`.

Boundary permits (which domains a task may visit) are the SESSION's decision,
enforced by `policy.py` before an action reaches here — `Browser` itself just
navigates, reads and clicks when asked, with no allow-list of its own.

Nothing here trusts a coordinate the caller hands in: `click`/`fill` take a
`ref` minted by a prior `find`, and resolve it to a CURRENT rect through the
DOM probe (`_dom_probe.js`) at the moment of the call — never a point cached
from when the ref was minted, and never a point synthesized by this module.
A page that scrolled or reflowed in between gets clicked in the right place,
or the probe reports the ref gone (`stale_ref`) and nothing is dispatched.
"""
from __future__ import annotations

import base64
import io
import json
from importlib import resources

from . import paths
from ._cdp import CdpTransport
from .backends.base import HandsError

# The probe's own default is 200; Browser asks for more because `find`'s
# whole DOM scan happens once per call and a shallow cap would silently miss
# a control that is visually below the fold but still interactive.
_PROBE_MAX_NODES = 500


def _load_probe_source() -> str:
    return (resources.files("firekeep_hands") / "_dom_probe.js").read_text(encoding="utf-8")


class Browser:
    def __init__(self, transport: "CdpTransport | None" = None, *, kind: str = "auto"):
        self._transport = transport
        self._kind = kind
        self._sessions: dict[str, str] = {}
        self._current_tab: str | None = None

    # -- lifecycle -----------------------------------------------------

    def open(self) -> dict:
        """Launches the browser if this `Browser` was not handed a transport
        already, and attaches to the first tab so a caller who only ever
        calls `open()` still finds out here whether the browser actually
        answers, rather than on the first real action."""
        self._ensure_transport()
        tabs = self.tabs()
        if tabs:
            self._current_tab = tabs[0]["id"]
            self._resolve(self._current_tab)
        return {"tabs": tabs}

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
        self._transport = None
        self._sessions.clear()
        self._current_tab = None

    def tabs(self) -> list[dict]:
        return [
            {"id": target.get("targetId", ""), "url": target.get("url", ""),
             "title": target.get("title", "")}
            for target in self._page_targets()
        ]

    # -- navigation and reading ------------------------------------------

    def navigate(self, url: str, *, tab: str | None = None) -> dict:
        target_id, session = self._resolve(tab)
        self._ensure_transport().send("Page.navigate", {"url": url}, session=session)
        self._ensure_transport().wait_event(
            "Page.loadEventFired", session=session, timeout=10.0)
        return self._tab_info(target_id)

    def read(self, *, tab: str | None = None, budget: int = 4000) -> dict:
        target_id, session = self._resolve(tab)
        result = self._ensure_transport().send(
            "Runtime.evaluate",
            {"expression": "document.body ? document.body.innerText : ''",
             "returnByValue": True},
            session=session,
        )
        text = result.get("result", {}).get("value") or ""
        info = self._tab_info(target_id)
        return {"url": info["url"], "title": info["title"], "text": str(text)[:budget]}

    def find(self, query: str, *, tab: str | None = None, limit: int = 10) -> list[dict]:
        data = self._run_probe(tab, "find", query=query, limit=limit)
        return list(data.get("controls", []))[:limit]

    # -- acting on a control -----------------------------------------------

    def click(self, ref: str, *, tab: str | None = None) -> None:
        rect = self._locate(ref, tab)
        _target_id, session = self._resolve(tab)
        x, y, w, h = rect
        cx, cy = x + w / 2, y + h / 2
        transport = self._ensure_transport()
        transport.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": cx, "y": cy}, session=session,
        )
        for event_type in ("mousePressed", "mouseReleased"):
            transport.send(
                "Input.dispatchMouseEvent",
                {"type": event_type, "x": cx, "y": cy, "button": "left", "clickCount": 1},
                session=session,
            )

    def fill(self, ref: str, text: str, *, tab: str | None = None) -> None:
        self._locate(ref, tab)  # focuses the element; raises stale_ref if gone
        _target_id, session = self._resolve(tab)
        self._ensure_transport().send("Input.insertText", {"text": text}, session=session)

    def screenshot(self, *, tab: str | None = None, max_width: int = 1280) -> bytes:
        _target_id, session = self._resolve(tab)
        result = self._ensure_transport().send(
            "Page.captureScreenshot", {"format": "png"}, session=session)
        raw = base64.b64decode(result["data"])
        return self._downscale(raw, max_width)

    def current_url(self, tab: str | None = None) -> str:
        target_id, _session = self._resolve(tab)
        return self._tab_info(target_id)["url"]

    # -- internals: the DOM probe -------------------------------------------

    def _locate(self, ref: str, tab: str | None) -> list[int]:
        """The current `[x, y, w, h]` rect for `ref`, via the probe's
        "focus" op. Used by both `click` (which only needs the rect) and
        `fill` (which also wants the DOM focus `Input.insertText` requires) —
        see `_dom_probe.js` for why one op serves both."""
        data = self._run_probe(tab, "focus", ref=ref)
        rect = data.get("rect")
        if not data.get("ok") or not rect:
            raise HandsError("stale_ref", f"{ref} no longer exists (find again)")
        return rect

    def _run_probe(self, tab: str | None, op: str, **extra) -> dict:
        _target_id, session = self._resolve(tab)
        payload = {"op": op, "max_nodes": _PROBE_MAX_NODES}
        payload.update(extra)
        # `window.__hands = ...`, not `const __hands = ...`: every
        # `Runtime.evaluate` call on a session runs in that page's SAME
        # global lexical scope (like pasting into DevTools console
        # repeatedly), so a second `const` declaration of the same name
        # throws "Identifier has already been declared" — found live, on
        # the second `find()` call against one page (see the task report).
        # A plain assignment is idempotent across any number of calls.
        expression = f"window.__hands = {json.dumps(payload)};\n{_load_probe_source()}"
        result = self._ensure_transport().send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
            session=session,
        )
        value = result.get("result", {}).get("value")
        if not isinstance(value, dict):
            raise HandsError("backend", "the DOM probe returned no usable data")
        return value

    # -- internals: transport / tab / session bookkeeping -------------------

    def _ensure_transport(self) -> "CdpTransport":
        if self._transport is None:
            self._transport = CdpTransport.launch(self._kind, paths.chrome_profile_dir())
        return self._transport

    def _page_targets(self) -> list[dict]:
        result = self._ensure_transport().send("Target.getTargets")
        return [t for t in result.get("targetInfos", []) if t.get("type") == "page"]

    def _target_id(self, tab: str | None) -> str:
        if tab is not None:
            return tab
        if self._current_tab is not None:
            return self._current_tab
        targets = self._page_targets()
        if not targets:
            raise HandsError("not_found", "no open tab")
        self._current_tab = targets[0]["targetId"]
        return self._current_tab

    def _resolve(self, tab: str | None) -> tuple[str, str]:
        """`(target_id, session_id)` for `tab` (or the current tab), attaching
        lazily on first use of a target rather than up front for every tab.

        `Page.enable` rides along with a fresh attach: CDP does not deliver
        Page-domain events — `navigate`'s `Page.loadEventFired` included —
        to a session that never enabled the domain, and discovering that by
        having `navigate` silently eat a full 10s timeout on every call was
        this task's one hardware surprise (see the live-check transcript in
        the task report)."""
        target_id = self._target_id(tab)
        session = self._sessions.get(target_id)
        if session is None:
            transport = self._ensure_transport()
            session = transport.attach(target_id)
            transport.send("Page.enable", session=session)
            self._sessions[target_id] = session
        return target_id, session

    def _tab_info(self, target_id: str) -> dict:
        result = self._ensure_transport().send(
            "Target.getTargetInfo", {"targetId": target_id})
        info = result.get("targetInfo", {})
        return {"url": info.get("url", ""), "title": info.get("title", "")}

    @staticmethod
    def _downscale(raw: bytes, max_width: int) -> bytes:
        from PIL import Image

        image = Image.open(io.BytesIO(raw))
        image.load()
        if max_width > 0:
            # A huge height bound is deliberate: `thumbnail` preserves aspect
            # ratio and must constrain width only.
            image.thumbnail((max_width, 10 ** 6))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
