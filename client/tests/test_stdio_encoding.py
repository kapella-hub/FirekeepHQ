"""Non-ASCII text must survive a stdio hop from ANY host locale.

WHY THIS EXISTS. Every process in the kit that speaks JSON on stdio read
`sys.stdin` and wrote `sys.stdout` at the platform default encoding, which on
Windows is the ANSI code page (cp1252), not UTF-8. Measured against the live
deployment: `relay_register(goal="probe — unicode ✓ test")` issued from the
Windows client stored `"probe â€" unicode âœ" test"` — Redis held
`c3a2 e282ac e2809d` where the em dash's `e2 80 94` belonged — while the
identical call from the Linux VPS round-tripped byte-perfect, which proves the
server was never involved. The corruption is already in the owner's real
presence entry and in the live replay stream, and it reaches every write
surface: presence, tasks, DMs, bulletin, and by extension memories, skills and
corpus text.

The gateway is the sharpest example: it pinned its BACKEND subprocess pipes
correctly (`Popen(..., text=True, encoding="utf-8")`) and left its own stdio on
the locale default — the one hop nobody configured.

These tests run on any host, because they assert on what the code DOES to the
streams (reconfigure to utf-8) and on a real round trip through a stream that
is explicitly constructed as cp1252 — not on the ambient platform default,
which would make the whole file a no-op on Linux CI.
"""

from __future__ import annotations

import io
import json

import pytest

from firekeep_client.stdio import force_utf8_stdio


NON_ASCII = "probe — unicode ✓ test"


class _FakeStream:
    """Records reconfigure() kwargs; nothing else."""

    def __init__(self):
        self.calls = []

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)


def test_force_utf8_stdio_pins_all_three_streams(monkeypatch):
    """stdin, stdout AND stderr — a half-pinned process still corrupts.

    stdin carries hook payloads and JSON-RPC requests, stdout carries replies
    and hook systemMessages, stderr carries block reasons the runtime shows the
    user. All three are text the agent or the server sees.
    """
    streams = {}
    for name in ("stdin", "stdout", "stderr"):
        streams[name] = _FakeStream()
        monkeypatch.setattr("sys." + name, streams[name])

    force_utf8_stdio()

    for name, stream in streams.items():
        assert len(stream.calls) == 1, name
        assert stream.calls[0]["encoding"] == "utf-8", name


def test_force_utf8_stdio_pins_lf_on_stdout(monkeypatch):
    """A JSON-RPC frame is delimited by ONE newline.

    Windows text mode translates `\\n` to `\\r\\n` independently of encoding,
    which corrupts the framing rather than the characters — a separate defect
    from the mojibake, fixed by the same call.
    """
    out = _FakeStream()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stdin", _FakeStream())
    monkeypatch.setattr("sys.stderr", _FakeStream())

    force_utf8_stdio()

    assert out.calls[0]["newline"] == "\n"


def test_force_utf8_stdio_survives_a_stream_that_cannot_be_reconfigured(monkeypatch):
    """An encoding tweak must never be the reason a process refuses to start.

    A detached stream, or a test double substituted for stdout, has no
    `reconfigure` (or raises from it). Both are non-events.
    """

    class _Hostile:
        def reconfigure(self, **kwargs):
            raise ValueError("underlying buffer has been detached")

    monkeypatch.setattr("sys.stdin", _Hostile())
    monkeypatch.setattr("sys.stdout", object())  # no reconfigure attribute at all
    monkeypatch.setattr("sys.stderr", None)

    force_utf8_stdio()  # must not raise


def test_cp1252_stream_round_trips_after_reconfigure():
    """The end-to-end proof, on any host.

    A stream constructed as cp1252 mangles the em dash; the same stream
    reconfigured to utf-8 returns it byte-identical. This is the mechanism
    behind the live corruption, reproduced without needing a Windows runner.
    """
    payload = json.dumps({"goal": NON_ASCII}, ensure_ascii=False)

    raw = io.BytesIO(payload.encode("utf-8"))
    mangled = io.TextIOWrapper(raw, encoding="cp1252", errors="replace")
    assert json.loads(mangled.read())["goal"] != NON_ASCII

    raw.seek(0)
    fixed = io.TextIOWrapper(raw, encoding="cp1252", errors="replace")
    fixed.reconfigure(encoding="utf-8")
    assert json.loads(fixed.read())["goal"] == NON_ASCII


@pytest.mark.parametrize(
    "module_path, func",
    [
        ("firekeep_client.gateway", "run"),
        ("firekeep_client.hooks.__main__", "main"),
        ("firekeep_client.shim", "run"),
        ("firekeep_client.decision.server", "main"),
    ],
)
def test_every_stdio_entry_point_pins_utf8(module_path, func, monkeypatch):
    """Each stdio process must call it, or that process keeps corrupting text.

    Pinning it in one entry point and not the others is exactly the state that
    shipped: the gateway configured its children and not itself.
    """
    import importlib

    # `shim` and `decision.server` import anyio at module scope, and CI runs this
    # suite BEFORE `pip install -e client` (the base suite deliberately proves the
    # kit works on a bare interpreter). Same guard test_migration_entrypoints.py
    # already uses. The release workflow additionally runs this file in the
    # transport step, AFTER the install — so these two cases are skipped here and
    # genuinely executed there, rather than silently never running.
    if module_path in ("firekeep_client.shim", "firekeep_client.decision.server"):
        pytest.importorskip("anyio")

    module = importlib.import_module(module_path)
    called = []
    monkeypatch.setattr(module, "force_utf8_stdio", lambda: called.append(True))

    # Drive each entry point just far enough to reach the call, then bail out
    # via whatever early return it already has.
    if func == "run" and module_path.endswith("gateway"):
        monkeypatch.setattr(module.sys, "stdin", iter(()))
        monkeypatch.setattr(module.Gateway, "close", lambda self: None)
        module.run()
    elif func == "main" and "hooks" in module_path:
        module.main(["__no_such_core__"])
    elif func == "run":
        module.run("__no_such_service__")
    else:
        monkeypatch.setattr(
            module.resolver, "is_bypassed", lambda: True
        )
        monkeypatch.setattr(
            module, "FastMCPUnavailable", RuntimeError, raising=False
        )
        try:
            module.main()
        except BaseException:
            pass  # the handshake needs a real runtime; the call above is the point

    assert called == [True], f"{module_path}.{func} did not pin stdio to UTF-8"
