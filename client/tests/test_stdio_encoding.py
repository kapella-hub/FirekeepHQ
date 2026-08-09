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
import os
import sys

import pytest

from firekeep_client.stdio import force_utf8_stdio, pin_import_paths


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


@pytest.mark.parametrize(
    "module_path, func",
    [
        ("firekeep_client.gateway", "run"),
        ("firekeep_client.hooks.__main__", "main"),
        ("firekeep_client.shim", "run"),
        ("firekeep_client.decision.server", "main"),
    ],
)
def test_every_stdio_entry_point_pins_import_paths(module_path, func, monkeypatch):
    """Each stdio process must freeze module resolution at startup, or an update
    flip corrupts it mid-flight.

    The side-by-side layout (client 0.1.35) launches every kit process through
    the ``~/.firekeep/current`` alias, and Python does not canonicalize it: every
    ``sys.path`` entry stays on the alias path. A long-running process (gateway,
    shim, decision server) that performs a lazy import AFTER `firekeep update`
    flips the alias would resolve it through the NEW venv — mixing modules from
    two client versions inside one process, the kind of skew that surfaces as an
    unreproducible crash hours after the update that caused it. Pinning in one
    entry point and not the others is exactly the half-fixed state
    force_utf8_stdio shipped in once; this test makes the coverage symmetric.
    """
    import importlib

    # Same collection guard as the utf-8 test above: shim/decision import anyio
    # at module scope, and the base CI suite runs on a bare interpreter. The
    # release workflow re-runs this file after the real install, where these two
    # cases genuinely execute.
    if module_path in ("firekeep_client.shim", "firekeep_client.decision.server"):
        pytest.importorskip("anyio")

    module = importlib.import_module(module_path)
    called = []
    monkeypatch.setattr(module, "pin_import_paths", lambda: called.append(True))
    # Keep the sibling startup call from reconfiguring pytest's own captured
    # streams — this test asserts only the import-path pin.
    monkeypatch.setattr(module, "force_utf8_stdio", lambda: None)

    # Drive each entry point just far enough to reach the call, then bail out
    # via whatever early return it already has (mirrors the utf-8 test).
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

    assert called == [True], f"{module_path}.{func} did not pin its import paths"


def test_pin_import_paths_resolves_aliased_entries_and_survives_hostile_ones(
    tmp_path, monkeypatch
):
    """pin_import_paths must realpath alias entries and never raise on junk.

    The first half is the actual mechanism: an entry reached through the
    `current` link must become the REAL versioned dir, so imports performed
    after an update's flip still load the version this process started under.
    The second half is the same contract force_utf8_stdio carries: sys.path on a
    real machine contains hostile entries — ``''`` (script-dir placeholder,
    which realpath would silently rewrite to the CWD, changing import
    semantics) and paths that no longer exist — and startup hygiene must never
    be the reason a process refuses to start.
    """
    real = tmp_path / "venvs" / "9.9.9"
    real.mkdir(parents=True)
    link = tmp_path / "current"
    if os.name == "nt":
        # A junction, not a symlink: os.symlink needs a privilege/dev-mode grant
        # on Windows, junctions do not — and a junction is exactly what the
        # installer creates for `current`, so it is also the truer fixture.
        import _winapi
        _winapi.CreateJunction(str(real), str(link))
    else:
        os.symlink(real, link)

    missing = str(tmp_path / "does-not-exist")
    monkeypatch.setattr(sys, "path", [str(link), "", missing])

    pin_import_paths()  # must not raise, hostile entries included

    assert sys.path[0] == os.path.realpath(str(real)), (
        "the alias entry must be pinned to the real versioned dir"
    )
    assert sys.path[0] != str(link)
    assert sys.path[1] == "", (
        "'' means 'script dir / CWD at import time' by CPython convention; "
        "realpath-ing it would freeze it to the current CWD and change semantics"
    )
    assert sys.path[2] == os.path.realpath(missing), (
        "a nonexistent entry is normalized, never dropped and never a crash"
    )


def test_pin_import_paths_repins_already_imported_packages(tmp_path, monkeypatch):
    """Submodule imports do NOT consult sys.path — they consult the parent
    package's ``__path__``, computed at first import from the alias. Pinning
    sys.path alone leaves the most common lazy-import shape (``from
    firekeep_client.join import join``, anyio's backend selection) resolving
    through the flipped alias, which is the exact cross-version mixing the pin
    exists to stop. Found by the 0.1.35 pre-release review; the first draft
    only realpath'd sys.path."""
    import types

    real = tmp_path / "venvs" / "9.9.9" / "pkg"
    real.mkdir(parents=True)
    link = tmp_path / "current"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(tmp_path / "venvs" / "9.9.9"), str(link))
    else:
        os.symlink(tmp_path / "venvs" / "9.9.9", link)

    fake_pkg = types.ModuleType("fk_test_aliased_pkg")
    fake_pkg.__path__ = [str(link / "pkg")]
    hostile = types.ModuleType("fk_test_namespace_pkg")
    hostile.__path__ = ("not", "a", "list")  # _NamespacePath-shaped: must be left alone
    monkeypatch.setitem(sys.modules, "fk_test_aliased_pkg", fake_pkg)
    monkeypatch.setitem(sys.modules, "fk_test_namespace_pkg", hostile)

    pin_import_paths()

    assert fake_pkg.__path__ == [os.path.realpath(str(real))], (
        "an already-imported package's __path__ must be pinned to the real dir"
    )
    assert hostile.__path__ == ("not", "a", "list"), (
        "non-list __path__ (namespace packages) must be left untouched"
    )


def test_sidecar_entry_point_pins_import_paths(monkeypatch):
    """The sidecar is the LONGEST-lived kit process — a persistent daemon that
    near-certainly straddles an update's alias flip — and it was the one
    console-script entry point the first draft left unpinned (found by the
    0.1.35 pre-release review: the parametrized test above enumerates only the
    four stdio servers, so the omission stayed green)."""
    from firekeep_client import sidecar as sidecar_mod
    from firekeep_client import stdio as stdio_mod

    called = []
    monkeypatch.setattr(stdio_mod, "pin_import_paths", lambda: called.append(True))
    # Keep the daemon from actually starting: acquiring the singleton fails fast.
    monkeypatch.setattr(
        sidecar_mod.Sidecar, "_acquire_singleton", lambda self: False, raising=False
    )
    sidecar_mod.main([])
    assert called == [True], "firekeep-sidecar did not pin import paths at startup"
