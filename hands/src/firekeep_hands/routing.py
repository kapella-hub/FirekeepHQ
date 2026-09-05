"""Turns a runtime's action request into a concrete, backend-executable
route — deterministically, before policy or the broker ever sees it.

A model never gets to say "click at (x, y)": every pointer action names an
observed `Control` by `ref`, resolved against the `Observation` that produced
it, so an action can only ever land on something the runtime was actually
shown. `route` rejects raw coordinates outright and resolves everything else
against that observation, picking accessibility APIs over pixel simulation
whenever a control's UI Automation / AX patterns make that possible (more
reliable, and it leaves no synthetic mouse trail) and falling back to pixel
input otherwise.
"""
from __future__ import annotations

from dataclasses import dataclass

from .backends.base import Control, HandsError, Observation

_ACCESSIBILITY_INVOKE_PATTERNS = {"Invoke", "AXPress"}
_ACCESSIBILITY_VALUE_PATTERNS = {"Value", "AXValue"}
_FORBIDDEN_KEYS = ("x", "y", "point", "coordinates")

# The longest string a `type` action may carry.
#
# Typed text is delivered one character at a time and paced (the receiving
# control needs the gap to read each one correctly), so a long string is a
# long *window* during which the keystrokes keep landing wherever the
# foreground happens to be — and the foreground is not something Hands
# controls. A few hundred characters is a form field; four thousand is a
# document, which belongs in `set_value` on the field itself, where the text
# arrives at one named control in one step and cannot be sprayed anywhere
# else. The cap lives here rather than in a backend so both platforms and
# the session agree on it.
MAX_TYPE_CHARS = 500

# kind -> required payload keys. `scroll`'s "ref" is either a control ref or
# the literal "window"; both cases are still keyed by "ref" here.
_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "invoke": ("ref",),
    "set_value": ("ref", "value"),
    "click": ("ref",),
    "type": ("text",),
    "key": ("chord",),
    "scroll": ("ref", "dy"),
    "focus_app": ("app",),
    "open_app": ("app",),
    "open_url": ("url",),
    "clipboard_set": ("text",),
    "wait": ("seconds",),
}

# What each field must BE, not just that it is present. A JSON-RPC client can
# put any type in any field — `hands_act`'s schema declares `action` as a bare
# object and does not describe its inside — and every one of these values is
# either passed to an OS call or compared numerically further down. Without
# this, `{"kind": "wait", "seconds": "3"}` raised a bare TypeError from the
# `> 10` comparison below, and `{"kind": "scroll", "dy": "3"}` carried a
# string all the way into the backend.
_NUMBER = object()
_FIELD_TYPES: dict[str, object] = {
    "ref": str,
    "value": str,
    "text": str,
    "chord": str,
    "app": str,
    "url": str,
    "button": str,
    "double": bool,
    "dy": _NUMBER,
    "seconds": _NUMBER,
}


def _check_types(kind: str, action: dict) -> None:
    for key, expected in _FIELD_TYPES.items():
        if key not in action:
            continue
        value = action[key]
        if expected is _NUMBER:
            # `bool` is a subclass of `int`, and `True` is not a scroll
            # distance — reject it explicitly rather than scroll by 1.
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            wanted = "a number"
        else:
            ok = isinstance(value, expected)
            wanted = f"a {expected.__name__}"
        if not ok:
            raise HandsError(
                "invalid_action",
                f"{kind}'s {key!r} must be {wanted}, not {type(value).__name__}",
            )


@dataclass(frozen=True)
class Routed:
    kind: str
    route: str
    control: Control | None
    point: tuple[int, int] | None
    payload: dict


def _resolve_control(ref: str, observation: Observation | None) -> Control:
    if observation is not None:
        for control in observation.controls:
            if control.ref == ref:
                return control
    raise HandsError("stale_ref", f"ref {ref!r} is not in the current observation")


def route(action: dict, observation: Observation | None) -> Routed:
    # The envelope before its contents. `hands_act` declares `action` as a
    # bare object and a JSON-RPC client can send anything at all: a list made
    # the `in` scan below raise TypeError, and a dict `kind` made the lookup
    # raise "unhashable type". Both reached the caller as `backend: …`, which
    # tells a model its machine broke rather than that it sent nonsense.
    if not isinstance(action, dict):
        raise HandsError(
            "invalid_action", f"an action must be an object, not {type(action).__name__}")

    if any(k in action for k in _FORBIDDEN_KEYS):
        raise HandsError("invalid_action", "raw coordinates are not an accepted action shape")

    kind = action.get("kind")
    if not isinstance(kind, str):
        raise HandsError(
            "invalid_action", f"an action's 'kind' must be a string, not {type(kind).__name__}")
    required = _REQUIRED_KEYS.get(kind)
    if required is None:
        raise HandsError("invalid_action", f"unknown action kind {kind!r}")

    missing = [k for k in required if k not in action]
    if missing:
        raise HandsError("invalid_action", f"{kind} is missing required key(s): {missing}")

    _check_types(kind, action)

    if kind == "wait" and action["seconds"] > 10:
        raise HandsError("invalid_action", "wait seconds must be <= 10")

    if kind == "invoke":
        control = _resolve_control(action["ref"], observation)
        if _ACCESSIBILITY_INVOKE_PATTERNS.intersection(control.patterns):
            return Routed(kind, "accessibility", control, None, {})
        return Routed(kind, "pixel", control, control.rect.center(), {})

    if kind == "set_value":
        control = _resolve_control(action["ref"], observation)
        payload = {"value": action["value"]}
        if _ACCESSIBILITY_VALUE_PATTERNS.intersection(control.patterns):
            return Routed(kind, "accessibility", control, None, payload)
        return Routed(kind, "pixel+type", control, control.rect.center(), payload)

    if kind == "click":
        control = _resolve_control(action["ref"], observation)
        payload = {k: action[k] for k in ("button", "double") if k in action}
        return Routed(kind, "pixel", control, control.rect.center(), payload)

    if kind == "type":
        text = action["text"]
        if len(text) > MAX_TYPE_CHARS:
            raise HandsError(
                "invalid_action",
                f"text longer than {MAX_TYPE_CHARS} characters — "
                "use set_value on the field",
            )
        return Routed(kind, "input", None, None, {"text": text})

    if kind == "key":
        return Routed(kind, "shortcut", None, None, {"chord": action["chord"]})

    if kind == "scroll":
        ref = action["ref"]
        if ref == "window":
            return Routed(kind, "pixel", None, None, {"dy": action["dy"]})
        control = _resolve_control(ref, observation)
        return Routed(kind, "pixel", control, control.rect.center(), {"dy": action["dy"]})

    if kind in ("focus_app", "open_app"):
        return Routed(kind, "os", None, None, {"app": action["app"]})

    if kind == "open_url":
        return Routed(kind, "browser", None, None, {"url": action["url"]})

    if kind == "clipboard_set":
        return Routed(kind, "os", None, None, {"text": action["text"]})

    return Routed(kind, "none", None, None, {"seconds": action["seconds"]})  # kind == "wait"
