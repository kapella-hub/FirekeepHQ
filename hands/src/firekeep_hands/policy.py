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

# The kinds whose target is a WINDOW or a control inside one, rather than an
# app named in the payload (`open_app`/`focus_app`) or a URL (`open_url`).
#
# This tuple is what makes `boundary` the catch-all three documents say it is.
# Until it existed the class only fired on the app *crossing* — the two "switch
# app" verbs and a navigation — so a task started with `apps=[]` could click,
# type and invoke its way through whatever window happened to be in front,
# permit-free, as long as the control's own text tripped none of the other five
# classes. The banking window somebody left open is the case that matters.
_WINDOW_SCOPED = ("click", "invoke", "set_value", "type", "key", "scroll")

# The browser is ONE app for this purpose. Which site it may reach is the
# domain allowlist's job (`open_url` below), so a task that declared "browser"
# is not re-prompted on every page it clicks in. `HandsSession` builds its
# browser steps with this app name on both the control and the window.
BROWSER_APP = "browser"

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


def _named(value: str, names) -> bool:
    """Exact, case-insensitive membership.

    Case matters here in practice and not in principle: a human writes
    `apps=["notepad"]` and Windows reports the window as `"Notepad"`, and a
    boundary prompt on every click of a task the human explicitly scoped is
    the fastest way to teach somebody to stop reading the prompts."""
    needle = str(value).lower()
    return any(needle == str(n).lower() for n in names)


def _app_declared(app: str, policy: Policy, task_apps: list[str]) -> bool:
    """An empty app name is not evidence of a crossing.

    A backend that could not name the foreground window returns `""`, and
    treating that as "an app you did not declare" would put a permit in front
    of every step on a machine where window naming is unavailable — a refusal
    the human cannot act on, because there is nothing to add to `apps`."""
    return not app or _named(app, list(task_apps) + list(policy.apps))


def _target_apps(control: Control | None, window: WindowInfo | None) -> list[str]:
    """Which app a window-scoped step lands in, from both things that know.

    Both, not one: `hands_find(app=…)` can hand back a control in an app that
    is not the foreground window, so the control's own app is the more
    accurate answer when it has one — and a control with no app at all leaves
    the window as the only witness. Either being undeclared is a crossing."""
    apps: list[str] = []
    for app in ((control.app if control is not None else ""), (window.app if window else "")):
        app = str(app or "")
        if app and not _named(app, apps):
            apps.append(app)
    return apps


def boundary_apps(
    action: dict,
    control: Control | None,
    window: WindowInfo | None,
    url: str | None,
    policy: Policy,
    task_apps: list[str],
) -> list[str]:
    """Every name this action steps outside of: app names for anything that
    targets a window or a control, the URL's host for `open_url`.

    One function, two callers, on purpose. `_classify_with_reasons` uses it to
    raise the `boundary` class, and `HandsSession` uses it to know what to add
    to the task once a human has approved the crossing. Deriving the second
    from anything else is how a widening and a classification drift apart —
    and a widening that names a different app than the one that was approved
    is a hole, not a bug."""
    kind = action.get("kind")
    if kind == "open_url":
        target = action.get("url") or url
        if not target:
            return []
        host = (urlsplit(str(target)).hostname or "").lower()
        # The allowlist matches a parent domain; a task-scoped widening does
        # not. The human approved `pay.example.com`, not `example.com`.
        if host_allowed(policy, str(target)) or _named(host, task_apps):
            return []
        return [host or str(target)]
    if kind in ("open_app", "focus_app"):
        app = str(action.get("app", ""))
        return [] if _app_declared(app, policy, task_apps) else [app]
    if kind in _WINDOW_SCOPED:
        return [app for app in _target_apps(control, window)
                if not _app_declared(app, policy, task_apps)]
    return []


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

    # `send` reads the clicked control's own name, not the window title: a
    # bare "OK"/"Yes" button doesn't say what it sends, the control that was
    # actually invoked does. `money`/`destroy`/`install` also read the title
    # because a confirmation dialog puts the meaning there ("Confirm
    # payment", "Delete file?") and leaves a generic "OK"/"Yes" on the button.
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

    crossed = boundary_apps(action, control, window, url, policy, task_apps)
    if crossed:
        reasons["boundary"] = crossed[0]

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


def _parse_until(until: str) -> dt.datetime | None:
    """None for anything that isn't a well-formed ISO-8601 UTC timestamp —
    a malformed or empty `until` must not raise inside `decide()`, the
    safety gate every action passes through; callers treat None as expired,
    the same as a timestamp already in the past."""
    try:
        return dt.datetime.strptime(until, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


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
    until = _parse_until(entry.until)
    if until is None or until <= now:
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
