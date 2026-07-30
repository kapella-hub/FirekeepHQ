"""Task 21 CRITICAL fix: `firekeep_client/hooks/__main__.py` — the shared runner that makes
rendered hook commands actually execute `run()`.

Before this module existed, `base.hook_command()` rendered
`{python} -m firekeep_client.hooks.<core>`, which only IMPORTS the core module (no
stdin-reading `__main__` block) and exits 0 without ever calling `run()` — every
rendered hook was silently dead. The subprocess tests below pin the fix at the
PROCESS level (the layer the bug actually lived at): they invoke the real
`python -m firekeep_client.hooks <core>` command line, feed it stdin, and assert on
real stdout/stderr/exit-code, because an in-process call to `main()` cannot
prove the rendered command line is alive end-to-end.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

CLIENT_DIR = Path(__file__).resolve().parents[2]  # client/tests/hooks/<file> -> client/


def _write_subprocess_config(tmp_path: Path) -> dict:
    """A REAL tmp ~/.firekeep-style config pointed at an unreachable (nothing-listening)
    host:port. Unlike client_env's monkeypatch fixture, a subprocess can't be
    monkeypatched -- this is what proves the command is alive end-to-end: the core
    must degrade gracefully against a real (if unreachable) network target, not a
    mocked one.

    Deliberately NOT `host = 127.0.0.1` on the default ports (8100/8050/...):
    this repo's own dev stack (`docker compose up`) binds exactly those ports on
    localhost, so on a machine running the stack `GET /briefing` would SUCCEED and
    the fallback-message assertion below would false-fail. `base_url` on port 1 (a
    reserved port no user process — firekeep or otherwise — ever binds) gives a fast,
    deterministic connection-refused regardless of what's running locally.
    """
    cfg = tmp_path / "config"
    cfg.write_text(textwrap.dedent("""\
        [active]
        profile = personal
        [personal]
        kind = paths
        scheme = http
        base_url = http://127.0.0.1:1
        verify_tls = false
        agent_id = tester
    """))
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)  # idempotent: multi-invocation tests reuse one tmp_path
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    return {"cfg": cfg, "cache": cache, "logs": logs}


def _run_dispatcher(tmp_path: Path, args: list[str], stdin_text: str) -> subprocess.CompletedProcess:
    paths = _write_subprocess_config(tmp_path)
    env = dict(os.environ)
    env["FIREKEEP_CONFIG"] = str(paths["cfg"])
    env["FIREKEEP_CACHE_DIR"] = str(paths["cache"])
    env["FIREKEEP_LOG_DIR"] = str(paths["logs"])
    env.pop("FIREKEEP_AGENT_ID", None)
    env.pop("FIREKEEP_AGENT_GOAL", None)
    return subprocess.run(
        [sys.executable, "-m", "firekeep_client.hooks", *args],
        input=stdin_text,
        capture_output=True,
        text=True,
        cwd=str(CLIENT_DIR),
        env=env,
        timeout=30,
    )


class TestSubprocessAlive:
    """THE integration trap, pinned at the process level."""

    def test_session_start_degrades_gracefully_and_prints_systemmessage(self, tmp_path):
        proc = _run_dispatcher(tmp_path, ["session_start"], json.dumps({"goal": "g"}))
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert "systemMessage" in out
        assert "Firekeep MCP servers are available" in out["systemMessage"]

    def test_precompact_command_line_is_alive_and_prints_its_systemmessage(self, tmp_path):
        """The bug this file exists for: a core absent from _CORE_MODULES exits 0
        silently and the rendered hook is dead. Only the real command line proves
        otherwise."""
        proc = _run_dispatcher(tmp_path, ["precompact"], json.dumps({"session_id": "s1"}))
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert "systemMessage" in out
        assert "ctx_get_shadow" in out["systemMessage"]

    def test_unknown_core_exits_0_with_usage_on_stderr(self, tmp_path):
        proc = _run_dispatcher(tmp_path, ["bogus"], "")
        assert proc.returncode == 0
        assert proc.stdout.strip() == ""
        assert "usage" in proc.stderr.lower()
        assert "pre_tool" in proc.stderr  # usage line enumerates the known cores

    def test_missing_core_arg_exits_0_with_usage_on_stderr(self, tmp_path):
        proc = _run_dispatcher(tmp_path, [], "")
        assert proc.returncode == 0
        assert proc.stdout.strip() == ""
        assert "usage" in proc.stderr.lower()


class TestBlockExitRemapInProcess:
    """main() with monkeypatched core.run — the exit-code translation logic."""

    def _call(self, monkeypatch, argv, rc, stdin_text="{}"):
        from firekeep_client.hooks import __main__ as dispatcher

        monkeypatch.setattr(dispatcher._CORE_MODULES["pre_tool"], "run", lambda payload: rc)
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
        return dispatcher.main(argv)

    def test_gateway_block_rc1_remapped_to_2(self, monkeypatch):
        assert self._call(monkeypatch, ["pre_tool", "--block-exit", "2"], rc=1) == 2

    def test_lease_block_rc2_stays_2(self, monkeypatch):
        assert self._call(monkeypatch, ["pre_tool", "--block-exit", "2"], rc=2) == 2

    def test_allow_rc0_stays_0(self, monkeypatch):
        assert self._call(monkeypatch, ["pre_tool", "--block-exit", "2"], rc=0) == 0

    def test_no_block_exit_flag_passes_code_through_unmodified(self, monkeypatch):
        # post_tool is rendered WITHOUT --block-exit; a nonzero rc (if it ever happened)
        # must pass through unmodified rather than being silently remapped.
        assert self._call(monkeypatch, ["pre_tool"], rc=1) == 1


class TestMalformedStdinInProcess:
    def test_malformed_json_runs_core_with_empty_dict(self, monkeypatch):
        from firekeep_client.hooks import __main__ as dispatcher

        captured = {}

        def fake_run(payload):
            captured["payload"] = payload
            return {}

        monkeypatch.setattr(dispatcher._CORE_MODULES["session_start"], "run", fake_run)
        monkeypatch.setattr(sys, "stdin", io.StringIO("{not valid json"))
        rc = dispatcher.main(["session_start"])
        assert rc == 0
        assert captured["payload"] == {}

    def test_empty_stdin_runs_core_with_empty_dict(self, monkeypatch):
        from firekeep_client.hooks import __main__ as dispatcher

        captured = {}

        def fake_run(payload):
            captured["payload"] = payload
            return {}

        monkeypatch.setattr(dispatcher._CORE_MODULES["prompt"], "run", fake_run)
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        rc = dispatcher.main(["prompt"])
        assert rc == 0
        assert captured["payload"] == {}

    def test_valid_json_payload_passed_through(self, monkeypatch):
        from firekeep_client.hooks import __main__ as dispatcher

        captured = {}

        def fake_run(payload):
            captured["payload"] = payload
            return {}

        monkeypatch.setattr(dispatcher._CORE_MODULES["stop"], "run", fake_run)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "s1"})))
        rc = dispatcher.main(["stop"])
        assert rc == 0
        assert captured["payload"] == {"session_id": "s1"}


class TestDictCoreStdout:
    def test_truthy_result_printed_as_json_on_stdout(self, monkeypatch, capsys):
        from firekeep_client.hooks import __main__ as dispatcher

        monkeypatch.setattr(
            dispatcher._CORE_MODULES["stop"], "run", lambda payload: {"systemMessage": "hi"}
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        rc = dispatcher.main(["stop"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out == {"systemMessage": "hi"}

    def test_empty_dict_result_prints_nothing(self, monkeypatch, capsys):
        from firekeep_client.hooks import __main__ as dispatcher

        monkeypatch.setattr(dispatcher._CORE_MODULES["prompt"], "run", lambda payload: {})
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        rc = dispatcher.main(["prompt"])
        assert rc == 0
        assert capsys.readouterr().out == ""


class TestDispatcherNeverRaises:
    def test_core_run_raising_is_swallowed_and_exits_0(self, monkeypatch):
        from firekeep_client.hooks import __main__ as dispatcher

        def boom(payload):
            raise RuntimeError("boom")

        monkeypatch.setattr(dispatcher._CORE_MODULES["pre_tool"], "run", boom)
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        rc = dispatcher.main(["pre_tool"])
        assert rc == 0


@pytest.fixture(autouse=True)
def _restore_firekeep_profile_env():
    """Task 5's dispatcher exports FIREKEEP_PROFILE via a raw `os.environ[...] = `
    mutation (by design -- that's what makes the pin reach every nested
    `resolve()` call in a core with zero core changes). `monkeypatch.delenv`
    called at test start records nothing when the var is absent, so it can't
    track cleanup for a mutation the SUT performs later outside of monkeypatch.
    Snapshot/restore explicitly here so TestProfileArg can never leak
    FIREKEEP_PROFILE into the other hook-core tests in this package (whose tmp
    configs, via the `client_env` fixture, have no matching profile section --
    a leaked override would break them with a ConfigError).
    """
    original = os.environ.get("FIREKEEP_PROFILE")
    yield
    if original is None:
        os.environ.pop("FIREKEEP_PROFILE", None)
    else:
        os.environ["FIREKEEP_PROFILE"] = original


class TestProfileArg:
    """Task 5: --profile NAME sets FIREKEEP_PROFILE before the core runs, so every
    nested resolve() call in the core (session_start/_mcp.py, pre_tool/state.py,
    ...) follows the pin with zero core changes."""

    @staticmethod
    def _fake_core(record):
        class Core:
            @staticmethod
            def run(payload):
                record["env"] = os.environ.get("FIREKEEP_PROFILE")
                return {}
        return Core

    def test_profile_arg_sets_env_before_core_runs(self, monkeypatch, capsys):
        from firekeep_client.hooks import __main__ as dispatcher

        monkeypatch.delenv("FIREKEEP_PROFILE", raising=False)
        record = {}
        monkeypatch.setitem(dispatcher._CORE_MODULES, "prompt", self._fake_core(record))
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        rc = dispatcher.main(["prompt", "--profile", "office"])
        assert rc == 0
        assert record["env"] == "office"

    def test_profile_arg_combines_with_block_exit(self, monkeypatch):
        from firekeep_client.hooks import __main__ as dispatcher

        monkeypatch.delenv("FIREKEEP_PROFILE", raising=False)
        record = {}

        class Core:
            @staticmethod
            def run(payload):
                record["env"] = os.environ.get("FIREKEEP_PROFILE")
                return 1

        monkeypatch.setitem(dispatcher._CORE_MODULES, "pre_tool", Core)
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        rc = dispatcher.main(["pre_tool", "--block-exit", "2", "--profile", "office"])
        assert rc == 2
        assert record["env"] == "office"

    def test_missing_profile_value_is_ignored(self, monkeypatch):
        from firekeep_client.hooks import __main__ as dispatcher

        monkeypatch.delenv("FIREKEEP_PROFILE", raising=False)
        record = {}
        monkeypatch.setitem(dispatcher._CORE_MODULES, "prompt", self._fake_core(record))
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        rc = dispatcher.main(["prompt", "--profile"])
        assert rc == 0
        assert record["env"] is None


class TestPersonalTextCommand:
    """'/personal' typed as plain chat text (kiro has no slash-command surface) —
    dispatcher-level intercept, BEFORE the bypass gate, so it can toggle OFF too."""

    def _marker(self, tmp_path: Path) -> Path:
        # _write_subprocess_config puts the config at tmp_path/config, and the
        # marker lives beside the config (resolver.personal_marker_path).
        return tmp_path / "personal"

    def test_personal_text_toggles_on(self, tmp_path):
        proc = _run_dispatcher(tmp_path, ["prompt"], '{"prompt": "/personal"}')
        assert proc.returncode == 0
        assert "PERSONAL MODE ON" in proc.stdout
        assert self._marker(tmp_path).exists()

    def test_personal_text_toggles_off_even_while_bypassed(self, tmp_path):
        """The load-bearing ordering: while bypassed, the prompt core is
        short-circuited — only a pre-gate intercept can ever turn it OFF."""
        _run_dispatcher(tmp_path, ["prompt"], '{"prompt": "/personal on"}')
        assert self._marker(tmp_path).exists()
        proc = _run_dispatcher(tmp_path, ["prompt"], '{"prompt": "/personal off"}')
        assert proc.returncode == 0
        assert "team mode" in proc.stdout
        assert not self._marker(tmp_path).exists()

    def test_personal_status_reports_without_toggling(self, tmp_path):
        proc = _run_dispatcher(tmp_path, ["prompt"], '{"prompt": "/personal status"}')
        assert "OFF" in proc.stdout
        assert not self._marker(tmp_path).exists()

    def test_unknown_action_reports_usage(self, tmp_path):
        proc = _run_dispatcher(tmp_path, ["prompt"], '{"prompt": "/personal frobnicate"}')
        assert "unknown action" in proc.stdout
        assert not self._marker(tmp_path).exists()

    def test_ordinary_prompts_are_not_intercepted(self, tmp_path):
        proc = _run_dispatcher(tmp_path, ["prompt"], '{"prompt": "tell me about /personal later"}')
        assert "PERSONAL MODE" not in proc.stdout
        assert not self._marker(tmp_path).exists()


def test_precompact_is_registered_and_treated_as_a_dict_core():
    """`_DICT_CORES` is INERT -- verified: the dispatcher only ever consults
    `_INT_CORES` (lines 195, 215). What actually makes a dict core is membership
    in `_CORE_MODULES` plus absence from `_INT_CORES`. A core missing from
    `_CORE_MODULES` fails SILENTLY at exit 0, which is why this asserts the real
    mechanism and not the decorative set."""
    from firekeep_client.hooks import __main__ as dispatcher
    assert "precompact" in dispatcher._CORE_MODULES      # load-bearing
    assert "precompact" not in dispatcher._INT_CORES     # load-bearing
    assert "precompact" not in dispatcher._BYPASS_EXEMPT
