"""A fully in-memory Backend for tests: a fixed "scene" of controls plus a
window, with every mutating call recorded to `.calls` so behaviour built on
top of a Backend — the broker, the MCP tool handlers — can be exercised
deterministically, with no real UI Automation or Accessibility API involved.
"""
from __future__ import annotations

from .base import Control, Observation, Rect, WindowInfo

_DEFAULT_PERMISSIONS = {"accessibility": "ok", "screen": "ok", "input": "ok"}


class FakeBackend:
    name = "fake"

    def __init__(self, controls: list[Control] | None = None,
                 window: WindowInfo | None = None, text: str = "",
                 permissions: dict[str, str] | None = None):
        self.scene: list[Control] = list(controls or [])
        self.window = window
        self.text = text
        self._permissions = dict(permissions) if permissions else dict(_DEFAULT_PERMISSIONS)
        self.calls: list[tuple] = []
        self.values: dict[str, str] = {}
        self._generation = 0
        self._clipboard = ""

    def permissions(self) -> dict[str, str]:
        return dict(self._permissions)

    def active_window(self) -> WindowInfo | None:
        return self.window

    def windows(self) -> list[WindowInfo]:
        return [self.window] if self.window is not None else []

    def observe(self, *, app: str | None, region: Rect | None, max_nodes: int,
                text_budget: int, screenshot: bool, max_width: int) -> Observation:
        self._generation += 1
        controls = list(self.scene)
        truncated = len(controls) > max_nodes
        if truncated:
            controls = controls[:max_nodes]
        return Observation(
            generation=self._generation,
            window=self.window,
            controls=controls,
            text=self.text[:text_budget],
            screenshot_png=None,
            truncated=truncated,
        )

    def find(self, query: str, *, role: str | None, app: str | None,
             limit: int) -> list[Control]:
        needle = query.lower()
        # Case-folded, like the real backends: an app name is something a
        # caller typed, and the platforms report it in the OS's own case.
        app_needle = app.lower() if app is not None else None
        matches = []
        for c in self.scene:
            if role is not None and c.role != role:
                continue
            if app_needle is not None and c.app.lower() != app_needle:
                continue
            if needle in c.name.lower() or needle in c.value.lower():
                matches.append(c)
        return matches[:limit]

    def invoke(self, control: Control) -> None:
        self.calls.append(("invoke", control.ref))

    def set_value(self, control: Control, value: str) -> None:
        self.calls.append(("set_value", control.ref, value))
        self.values[control.ref] = value

    def click(self, point: tuple[int, int], *, button: str = "left",
              double: bool = False) -> None:
        self.calls.append(("click", point, button, double))

    def type_text(self, text: str) -> None:
        self.calls.append(("type", text))

    def key(self, chord: str) -> None:
        self.calls.append(("key", chord))

    def scroll(self, point: tuple[int, int], dy: int) -> None:
        self.calls.append(("scroll", point, dy))

    def focus_app(self, app: str) -> bool:
        self.calls.append(("focus_app", app))
        return True

    def open_app(self, app: str) -> bool:
        self.calls.append(("open_app", app))
        return True

    def clipboard_get(self) -> str:
        return self._clipboard

    def clipboard_set(self, text: str) -> None:
        self.calls.append(("clipboard_set", text))
        self._clipboard = text
