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
    if any(k in action for k in _FORBIDDEN_KEYS):
        raise HandsError("invalid_action", "raw coordinates are not an accepted action shape")

    kind = action.get("kind")
    required = _REQUIRED_KEYS.get(kind)
    if required is None:
        raise HandsError("invalid_action", f"unknown action kind {kind!r}")

    missing = [k for k in required if k not in action]
    if missing:
        raise HandsError("invalid_action", f"{kind} is missing required key(s): {missing}")

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
        return Routed(kind, "input", None, None, {"text": action["text"]})

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
