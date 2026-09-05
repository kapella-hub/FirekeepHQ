"""Protected-class classification and the permit/allow decision.

Hands can drive the whole desktop, so before any action reaches a backend it
passes through here: `classify` names the ways an action could hurt (send
something irreversible, spend money, destroy data, touch a credential,
install/elevate, or cross outside the task's declared apps/domains), and
`decide` turns that into a verdict a human doesn't have to re-litigate every
time — once they've approved a specific control or domain, `remember` lets
that approval stand for a while via a `Remembered` entry in `Policy`.

The regexes are deliberately narrow (word-boundary, on the control's own
name/value) rather than scanning arbitrary action payloads: a `key` action's
`chord` field or a `type` action's typed `text` can contain any string a
model or a user produces, and matching those against words like "delete"
would classify by what was typed, not by what the action actually targets.
Each class instead names precisely which field decides it, per the table in
the design doc this module implements.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .backends.base import Control, WindowInfo
from .config import Policy, Remembered

CLASSES = ("send", "money", "destroy", "credential", "install", "boundary")

_SEND_RE = re.compile(r"\b(send|post|publish|submit|reply|tweet|share)\b", re.IGNORECASE)
_MONEY_RE = re.compile(
    r"\b(pay|buy|purchase|checkout|transfer|donate|place order|order now|confirm payment|subscribe)\b",
    re.IGNORECASE,
)
_DESTROY_RE = re.compile(
    r"\b(delete|remove|erase|format|uninstall|empty (the )?(trash|recycle bin)|discard|shred|factory reset|permanently)\b",
    re.IGNORECASE,
)
_CREDENTIAL_RE = re.compile(
    r"\b(password|passcode|passphrase|otp|2fa|verification code|secret|api key|token)\b",
    re.IGNORECASE,
)
_INSTALL_RE = re.compile(
    r"\b(install|run as administrator|allow access|grant|enable extension|add extension|trust this)\b",
    re.IGNORECASE,
)
# A pasted secret looks like an opaque token, not a sentence — matched on
# shape rather than a word list since a generated API key never repeats.
_SECRET_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{32,}$")

_SEND_CHORDS = {"ctrl+enter", "cmd+enter", "cmd+shift+d"}
_DESTROY_CHORDS = {"delete", "shift+delete", "cmd+backspace", "cmd+delete"}
_DESTROY_APPS = {"explorer", "finder"}
_CREDENTIAL_ROLES = {"PasswordBox", "AXSecureTextField"}
_INSTALL_EXTENSIONS = (".msi", ".exe", ".pkg", ".dmg", ".app")


@dataclass(frozen=True)
class Decision:
    verdict: str  # "allow" | "permit"
    classes: tuple[str, ...]
    reason: str


def _control_text(control: Control | None) -> str:
    if control is None:
        return ""
    return f"{control.name} {control.value}"


def _classify_with_reasons(
    action: dict,
    control: Control | None,
    window: WindowInfo | None,
    url: str | None,
    policy: Policy,
    task_apps: list[str],
) -> dict[str, str]:
    """The class -> matched-text map `classify` and `decide` both need;
    `decide` additionally uses the matched text to build its `reason`."""
    kind = action.get("kind")
    reasons: dict[str, str] = {}
    ctrl_text = _control_text(control)
    title = window.title if window else ""
    name_and_title = " ".join(t for t in (ctrl_text, title) if t)

    if kind in ("invoke", "click"):
        m = _SEND_RE.search(ctrl_text)
        if m:
            reasons["send"] = m.group(0)
    if kind == "key":
        chord = str(action.get("chord", "")).lower()
        if chord in _SEND_CHORDS:
            reasons["send"] = chord

    m = _MONEY_RE.search(name_and_title)
    if m:
        reasons["money"] = m.group(0)

    m = _DESTROY_RE.search(name_and_title)
    if m:
        reasons["destroy"] = m.group(0)
    elif kind == "key":
        chord = str(action.get("chord", "")).lower()
        app = (window.app if window else "").lower()
        if chord in _DESTROY_CHORDS and app in _DESTROY_APPS:
            reasons["destroy"] = chord

    if kind in ("type", "set_value"):
        role = control.role if control else None
        if role in _CREDENTIAL_ROLES:
            reasons["credential"] = f"role={role}"
        else:
            m = _CREDENTIAL_RE.search(ctrl_text)
            if m:
                reasons["credential"] = m.group(0)
    elif kind == "clipboard_set":
        text = action.get("text", "")
        if _SECRET_TOKEN_RE.match(text):
            reasons["credential"] = "clipboard token"

    m = _INSTALL_RE.search(name_and_title)
    if m:
        reasons["install"] = m.group(0)
    elif kind == "open_app":
        app = action.get("app", "")
        if app.lower().endswith(_INSTALL_EXTENSIONS) and app not in policy.apps:
            reasons["install"] = app

    if kind == "open_url":
        target = action.get("url") or url
        if target and not host_allowed(policy, target):
            reasons["boundary"] = target
    elif kind in ("open_app", "focus_app"):
        app = action.get("app", "")
        if app not in task_apps and app not in policy.apps:
            reasons["boundary"] = app

    return reasons


def classify(
    action: dict,
    control: Control | None,
    window: WindowInfo | None,
    url: str | None,
    policy: Policy,
    task_apps: list[str],
) -> tuple[str, ...]:
    reasons = _classify_with_reasons(action, control, window, url, policy, task_apps)
    return tuple(c for c in CLASSES if c in reasons)


def _parse_until(until: str) -> dt.datetime:
    return dt.datetime.strptime(until, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def _is_remembered(
    entry: Remembered,
    cls: str,
    control: Control | None,
    window: WindowInfo | None,
    url: str | None,
    now: dt.datetime,
) -> bool:
    if entry.cls != cls:
        return False
    app = window.app if window else None
    if entry.app != "*" and entry.app != app:
        return False
    if _parse_until(entry.until) <= now:
        return False
    match = entry.match.lower()
    haystacks = [h.lower() for h in (control.name if control else None, url) if h]
    return any(match in h for h in haystacks)


def decide(
    action: dict,
    control: Control | None,
    window: WindowInfo | None,
    url: str | None,
    policy: Policy,
    task_apps: list[str],
    now: dt.datetime | None = None,
) -> Decision:
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    reasons = _classify_with_reasons(action, control, window, url, policy, task_apps)
    remaining = tuple(
        c
        for c in CLASSES
        if c in reasons
        and not any(_is_remembered(r, c, control, window, url, now) for r in policy.remembered)
    )
    if remaining:
        cls = remaining[0]
        return Decision("permit", remaining, f"{cls}: {reasons[cls]}")
    return Decision("allow", remaining, "")


def remember(policy: Policy, cls: str, app: str, match: str, days: int = 30, now: dt.datetime | None = None) -> None:
    """Record a human's approval so `decide` stops asking for it — until it
    expires, at which point the same action classifies as `permit` again."""
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    until = (now + dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    policy.remembered.append(Remembered(cls, app, match, until))


def host_allowed(policy: Policy, url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    for d in policy.domains:
        d = d.lower()
        if host == d or host.endswith("." + d):
            return True
    return False
