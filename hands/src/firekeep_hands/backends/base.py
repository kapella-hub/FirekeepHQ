"""The Backend protocol every platform module implements, and the small
platform-neutral value types (Rect, Control, WindowInfo, Observation) that
cross it.

Nothing here touches an OS API — pure dataclasses and a `typing.Protocol` —
so this module imports and runs on any platform, including Linux CI where
no real backend can. `UnsupportedBackend` (used by `backends.load_backend()`
outside Windows/macOS) also lives here rather than in its own module: it has
no OS code of its own, just HandsError raises, so it needs no lazy import.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Protocol


class HandsError(Exception):
    """A Backend operation failed. `code` is one of "stale_ref", "not_found",
    "unsupported", "elevated_target", "permission", "backend", "invalid_action"
    — a closed set callers (and the approval broker) can branch on without
    parsing text.

    `HandsSession` raises the same exception for three failures that are not a
    backend's: "no_task" (a step was attempted before `hands_task_start`),
    "budget" (the task has spent its step allowance) and "busy" (a task is
    already open, or another session holds this machine's lease). They are
    listed here because the set is only closed if it is written down in one
    place."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    def center(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)


@dataclass(frozen=True)
class Control:
    ref: str
    role: str
    name: str
    value: str
    rect: Rect
    app: str
    patterns: tuple[str, ...]
    enabled: bool = True


@dataclass(frozen=True)
class WindowInfo:
    app: str
    title: str
    pid: int
    rect: Rect
    elevated: bool = False


@dataclass
class Observation:
    generation: int
    window: WindowInfo | None
    controls: list[Control]
    text: str
    screenshot_png: bytes | None
    truncated: bool


class Backend(Protocol):
    name: str

    def permissions(self) -> dict[str, str]:
        """accessibility|screen|input -> ok|missing|unknown."""
        ...

    def active_window(self) -> WindowInfo | None: ...

    def windows(self) -> list[WindowInfo]: ...

    def observe(self, *, app: str | None, region: Rect | None, max_nodes: int,
                text_budget: int, screenshot: bool, max_width: int) -> Observation: ...

    def find(self, query: str, *, role: str | None, app: str | None,
             limit: int) -> list[Control]: ...

    def invoke(self, control: Control) -> None: ...

    def set_value(self, control: Control, value: str) -> None: ...

    def click(self, point: tuple[int, int], *, button: str = "left",
              double: bool = False) -> None: ...

    def type_text(self, text: str) -> None: ...

    def key(self, chord: str) -> None: ...

    def scroll(self, point: tuple[int, int], dy: int) -> None: ...

    def focus_app(self, app: str) -> bool: ...

    def open_app(self, app: str) -> bool:
        """True means the launch was *requested*, not that the app is running
        or that a window exists — Windows' `start` and macOS' `open` both
        return as soon as they have handed the request on. A caller that
        needs the window has to poll `windows()` for it."""
        ...

    def clipboard_get(self) -> str: ...

    def clipboard_set(self, text: str) -> None: ...


class UnsupportedBackend:
    """The Backend used on any platform Hands does not support. Every action
    raises rather than silently no-op'ing, so a caller finds out up front
    that Hands cannot run here, instead of failing action by action;
    `permissions()` reports every capability missing for the same reason."""

    name = "unsupported"

    def _unsupported(self) -> None:
        raise HandsError(
            "unsupported",
            "Hands supports Windows and macOS; this is " + sys.platform,
        )

    def permissions(self) -> dict[str, str]:
        return {"accessibility": "missing", "screen": "missing", "input": "missing"}

    def active_window(self) -> WindowInfo | None:
        self._unsupported()

    def windows(self) -> list[WindowInfo]:
        self._unsupported()

    def observe(self, *, app: str | None, region: Rect | None, max_nodes: int,
                text_budget: int, screenshot: bool, max_width: int) -> Observation:
        self._unsupported()

    def find(self, query: str, *, role: str | None, app: str | None,
             limit: int) -> list[Control]:
        self._unsupported()

    def invoke(self, control: Control) -> None:
        self._unsupported()

    def set_value(self, control: Control, value: str) -> None:
        self._unsupported()

    def click(self, point: tuple[int, int], *, button: str = "left",
              double: bool = False) -> None:
        self._unsupported()

    def type_text(self, text: str) -> None:
        self._unsupported()

    def key(self, chord: str) -> None:
        self._unsupported()

    def scroll(self, point: tuple[int, int], dy: int) -> None:
        self._unsupported()

    def focus_app(self, app: str) -> bool:
        self._unsupported()

    def open_app(self, app: str) -> bool:
        self._unsupported()

    def clipboard_get(self) -> str:
        self._unsupported()

    def clipboard_set(self, text: str) -> None:
        self._unsupported()
