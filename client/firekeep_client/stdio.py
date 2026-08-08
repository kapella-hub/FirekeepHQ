"""Pin this process's stdio to UTF-8.

WHY THIS EXISTS. On Windows, CPython picks the ANSI code page for `sys.stdin`
and `sys.stdout` (cp1252 in Western locales) unless something says otherwise.
Every JSON-RPC frame the kit exchanges over stdio is UTF-8 by specification, so
a byte sequence like the em dash's `e2 80 94` is decoded as three cp1252
characters, `â€"`, and re-encoded as their UTF-8 forms — `c3a2 e282ac e2809d` —
which is what lands in the store.

This is not hypothetical and it is not cosmetic. Measured against the live
deployment: `relay_register(goal="probe — unicode ✓ test")` issued from the
Windows client came back as `"probe â€" unicode âœ" test"`, and Redis held
exactly those bytes; the identical call issued from the Linux VPS round-tripped
perfectly, so the server is not involved. The owner's REAL presence entry
already reads `"... Firekeep â€" due-diligence ..."`, and the same corruption
appears in the live replay stream and in every task, DM and bulletin written
from that machine. Anything an agent writes through the client — memories,
skills, corpus text — is exposed to it.

The gateway already pinned its BACKEND subprocess pipes correctly
(`subprocess.Popen(..., text=True, encoding="utf-8")`); it was its own stdio,
the hop nobody configured, that was left on the locale default.

Applied at every process entry point that reads or writes JSON on stdio. It is
a no-op on a UTF-8 platform, and it fails silently rather than taking down a
process over an encoding tweak — a stdio stream that cannot be reconfigured
(already detached, or replaced by a test double) is not a reason to refuse to
start.
"""

from __future__ import annotations

import sys


def force_utf8_stdio() -> None:
    """Reconfigure stdin/stdout/stderr to UTF-8 with LF line endings.

    ``newline="\\n"`` on the write side matters independently of the encoding:
    Windows text mode translates ``\\n`` to ``\\r\\n``, and a JSON-RPC frame is
    delimited by a single newline. ``errors="replace"`` on the read side keeps
    a genuinely malformed byte from raising inside a stdio pump that has no
    way to report it.
    """
    for name, kwargs in (
        ("stdin", {"encoding": "utf-8", "errors": "replace"}),
        ("stdout", {"encoding": "utf-8", "errors": "replace", "newline": "\n"}),
        ("stderr", {"encoding": "utf-8", "errors": "replace"}),
    ):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(**kwargs)
        except Exception:  # noqa: BLE001 — see module docstring
            pass
