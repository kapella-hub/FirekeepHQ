"""firekeep_client.hooks — stdlib-only ported hook cores."""
from __future__ import annotations

import functools
from typing import Any, Callable, TypeVar

from firekeep_client import hooklog

F = TypeVar("F", bound=Callable[..., Any])


def never_raise(default: Any) -> Callable[[F], F]:
    """Decorator for a hook core's run(): catch ANY exception that escapes it,
    hooklog the failure, and return `default` instead of propagating.

    Design §6.3 "availability over enforcement": every core calls
    resolver.load_config()/agent_id() UNGUARDED at the top
    of run() -- a missing or malformed ~/.firekeep config raises ConfigError,
    which must degrade the hook (return the safe default), not crash the
    caller's process. pre_tool/post_tool's safe default is 0 (allow --
    matching their existing server-unreachable behavior); session_start/
    stop/prompt's is {} (no systemMessage).

    The hook name used for hooklog.log_failure is derived from the wrapped
    function's module (e.g. "firekeep_client.hooks.pre_tool" -> "pre_tool"),
    matching the `_HOOK` constant each core already defines -- no need to
    pass it explicitly.

    `default` is captured once at decoration time. For dict/list defaults a
    fresh copy is returned on every crash so a caller mutating one crash's
    return value can never leak into the next (session_start/stop/prompt all
    default to `{}`).
    """
    def _decorator(fn: F) -> F:
        hook_name = fn.__module__.rsplit(".", 1)[-1]

        @functools.wraps(fn)
        def _wrapped(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 — last-resort guard, by design
                # A migration conflict must reach the dispatcher, which has the
                # only universal hook output channel (systemMessage). Ordinary
                # config failures keep the availability-first fallback below.
                from firekeep_client import resolver
                if isinstance(e, resolver.ConfigMigrationConflict):
                    raise
                hooklog.log_failure(hook_name, f"run() crashed: {e!r}", exc=e)
                if isinstance(default, dict):
                    return dict(default)
                if isinstance(default, list):
                    return list(default)
                return default
        return _wrapped
    return _decorator
