import configparser
import json
import os
import tomllib
from pathlib import Path

import pytest
from firekeep_client import cli


class _RecordingAdapter:
    def __init__(self):
        self.calls = []

    def render(self, *, venv_bin):
        self.calls.append(venv_bin)

    def unrender(self):
        pass


@pytest.fixture
def install_env(tmp_path, monkeypatch):
    home = tmp_path / ".firekeep"
    monkeypatch.setenv("FIREKEEP_CONFIG", str(home / "config"))
    monkeypatch.setattr("firekeep_client.state._private", lambda p: None)
    runs = []
    monkeypatch.setattr(cli, "_run", lambda cmd, **kw: runs.append(list(cmd)))
    rec = _RecordingAdapter()
    monkeypatch.setattr(cli, "get_adapter", lambda name: rec)
    # Hermetic: never touch the developer's REAL ~/.zshrc from a test run. The PATH
    # wiring is exercised explicitly in the tests below by re-stubbing this.
    monkeypatch.setattr(cli.pathenv, "ensure_on_path", lambda home, venv_bin, **kw: [])
    return home, runs, rec


def _flatten(runs):
    return " ".join(tok for cmd in runs for tok in cmd)


def test_install_has_no_with_symdex_flag():
    # --with-symdex is removed: argparse must reject it.
    from firekeep_client.cli import _build_parser  # the real top-level parser factory

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["install", "--with-symdex"])


def test_checkout_install_uses_local_symdex_dir(install_env, monkeypatch, tmp_path):
    # From a checkout, symdex installs from the sibling dir BY PATH, never by name.
    calls = []
    monkeypatch.setattr(cli, "_pip_install", lambda py, *pkgs, **k: calls.append(pkgs))
    kit = tmp_path / "client"
    kit.mkdir(parents=True)
    (kit / "pyproject.toml").write_text("[project]\nname='firekeep-client'\n")
    symdex = tmp_path / "symdex"
    symdex.mkdir()
    (symdex / "pyproject.toml").write_text("[project]\nname='firekeep-symdex'\n")
    monkeypatch.setattr(cli, "_kit_dir", lambda: kit)

    rc = cli.main(["install", "--runtime", "claude"])
    assert rc == 0
    installed = [p for pkgs in calls for p in pkgs]
    assert str(symdex) in installed
    assert "firekeep-symdex" not in installed  # NEVER by name


def test_install_bootstraps_home_and_config(install_env):
    home, runs, rec = install_env
    rc = cli.main(["install", "--runtime", "claude"])
    assert rc == 0
    assert home.is_dir()
    assert (home / "logs").is_dir()
    cfg = configparser.ConfigParser()
    cfg.read(home / "config")
    assert cfg["identity"]["agent_id"] == "CHANGEME"
    assert cfg["server"]["kind"] == "ports"
    gi = (home / ".gitignore").read_text(encoding="utf-8")
    assert gi.strip() == "*"


def test_install_creates_venv_and_pip_installs_client(install_env):
    home, runs, rec = install_env
    cli.main(["install", "--runtime", "claude"])
    blob = _flatten(runs)
    assert "-m venv" in blob
    assert "firekeep-symdex" not in blob
    # The client kit must be installed from the LOCAL kit directory (the dir
    # holding client/pyproject.toml), never resolved as a bare name against
    # PyPI — "firekeep-client" on PyPI is owned by a third party.
    assert "firekeep-client" not in blob
    pip_install_cmds = [cmd for cmd in runs if "install" in cmd]
    assert pip_install_cmds, "expected a pip install invocation"
    kit_targets = [
        tok for cmd in pip_install_cmds for tok in cmd
        if (Path(tok) / "pyproject.toml").is_file()
    ]
    assert kit_targets, f"expected a pip install target pointing at the local kit dir, got: {pip_install_cmds}"
    assert Path(kit_targets[0]).name == "client"


def test_install_adds_firekeep_to_path(install_env, monkeypatch):
    home, runs, rec = install_env
    calls = []
    monkeypatch.setattr(cli.pathenv, "ensure_on_path",
                        lambda home, venv_bin, **kw: calls.append((home, venv_bin)) or ["ok"])
    rc = cli.main(["install", "--runtime", "claude"])
    assert rc == 0
    assert calls, "install must put a firekeep launcher on PATH"
    called_home, called_venv_bin = calls[0]
    assert called_venv_bin == home / "venv" / ("Scripts" if os.name == "nt" else "bin")


def test_no_modify_path_flag_skips_path(install_env, monkeypatch, capsys):
    home, runs, rec = install_env
    calls = []
    monkeypatch.setattr(cli.pathenv, "ensure_on_path",
                        lambda *a, **k: calls.append(1) or [])
    rc = cli.main(["install", "--runtime", "claude", "--no-modify-path"])
    assert rc == 0
    assert calls == [], "--no-modify-path must not touch PATH"
    assert "skipped PATH setup" in capsys.readouterr().out


def test_env_opt_out_skips_path(install_env, monkeypatch):
    home, runs, rec = install_env
    monkeypatch.setenv("FIREKEEP_NO_MODIFY_PATH", "1")
    calls = []
    monkeypatch.setattr(cli.pathenv, "ensure_on_path",
                        lambda *a, **k: calls.append(1) or [])
    cli.main(["install", "--runtime", "claude"])
    assert calls == [], "FIREKEEP_NO_MODIFY_PATH must not touch PATH"


def test_path_failure_does_not_fail_the_install(install_env, monkeypatch, capsys):
    """PATH setup is a convenience — a failure prints a fallback and the install
    still succeeds (rc==0), never a traceback or nonzero exit."""
    home, runs, rec = install_env

    def boom(*a, **k):
        raise RuntimeError("registry locked")

    monkeypatch.setattr(cli.pathenv, "ensure_on_path", boom)
    rc = cli.main(["install", "--runtime", "claude"])
    assert rc == 0
    assert "could not modify PATH" in capsys.readouterr().out


def test_install_renders_selected_adapter_with_venv_bin(install_env):
    home, runs, rec = install_env
    cli.main(["install", "--runtime", "claude"])
    assert len(rec.calls) == 1
    venv_bin = rec.calls[0]
    expected = home / "venv" / ("Scripts" if os.name == "nt" else "bin")
    assert venv_bin == expected


def test_install_all_renders_four_runtimes(install_env):
    home, runs, rec = install_env
    cli.main(["install", "--runtime", "all"])
    assert len(rec.calls) == 4  # claude, codex, kiro, opencode


def test_no_runtime_headless_defaults_to_all(install_env):
    # non-interactive (pytest has no TTY) + no --runtime -> all four, unchanged.
    home, runs, rec = install_env
    cli.main(["install"])
    assert len(rec.calls) == 4


def test_interactive_without_runtime_renders_all_adapters(install_env, monkeypatch):
    # The client is agent-agnostic: a normal first install must prepare every shipped
    # runtime without asking the customer to predict which client they will use later.
    home, runs, rec = install_env
    monkeypatch.setattr("firekeep_client.wizard.is_interactive", lambda *a, **k: True)
    monkeypatch.setattr("firekeep_client.wizard.prompt_config", lambda cfg, **k: cfg)
    cli.main(["install"])
    assert len(rec.calls) == 4


def test_explicit_runtime_renders_only_that_adapter(install_env, monkeypatch):
    # Explicit --runtime remains the targeted re-render/repair path.
    home, runs, rec = install_env
    monkeypatch.setattr("firekeep_client.wizard.is_interactive", lambda *a, **k: True)
    monkeypatch.setattr("firekeep_client.wizard.prompt_config", lambda cfg, **k: cfg)
    cli.main(["install", "--runtime", "claude"])
    assert len(rec.calls) == 1  # only claude


def test_fresh_install_renders_every_native_adapter(tmp_path, monkeypatch):
    """Exercise the real adapters from an empty user home through the real CLI.

    Unit tests for each adapter can pass while the install command never selects it;
    this is the customer-facing invariant that caught Codex and Kiro being absent after
    an interactive install that selected Claude.
    """
    user_home = tmp_path / "user"
    firekeep_home = user_home / ".firekeep"
    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("FIREKEEP_CONFIG", str(firekeep_home / "config"))
    monkeypatch.setattr(cli, "_kit_dir", lambda: None)
    monkeypatch.setattr(cli.state, "_private", lambda _p: None)
    monkeypatch.setattr(cli.pathenv, "ensure_on_path", lambda *a, **k: [])
    # Native kiro activation is covered in test_kiro.py; do not launch a real client
    # process from this installer topology test.
    monkeypatch.setattr("firekeep_client.adapters.kiro.shutil.which", lambda _name: None)

    assert cli.main(["install", "--non-interactive", "--no-modify-path"]) == 0

    expected = {
        "firekeep-cortex", "firekeep-bridge", "firekeep-sentinel", "firekeep-relay",
        "firekeep-symdex", "firekeep-decision",
    }
    claude = json.loads((user_home / ".claude.json").read_text(encoding="utf-8"))
    codex = tomllib.loads(
        (user_home / ".codex" / "config.toml").read_text(encoding="utf-8")
    )
    kiro = json.loads(
        (user_home / ".kiro" / "agents" / "firekeep.json").read_text(encoding="utf-8")
    )
    opencode = json.loads(
        (user_home / ".config" / "opencode" / "opencode.json").read_text(encoding="utf-8")
    )
    assert expected <= claude["mcpServers"].keys()
    assert expected <= codex["mcp_servers"].keys()
    assert expected <= kiro["mcpServers"].keys()
    assert expected <= opencode["mcp"].keys()


def test_install_from_installed_venv_skips_pip_and_still_renders(install_env, monkeypatch, capsys):
    """`firekeep install --runtime claude` run from the INSTALLED venv (exactly how CLAUDE.md
    documents re-rendering one runtime) used to hand pip the site-packages dir — which has
    no pyproject.toml — and die with 'Directory ... is not installable'. There is nothing to
    install in that situation: the code IS the installed code. Skip pip, still render."""
    home, runs, rec = install_env
    monkeypatch.setattr(cli, "_kit_dir", lambda: None)

    rc = cli.main(["install", "--runtime", "claude"])
    assert rc == 0
    assert not [cmd for cmd in runs if "install" in cmd], "pip must not run with no kit dir"
    assert rec.calls, "adapters must still be rendered"
    assert "skipping pip" in capsys.readouterr().out


def test_kit_dir_is_none_without_a_pyproject(monkeypatch, tmp_path):
    """The honest test for 'unpacked kit vs site-packages' is the presence of pyproject.toml,
    not a path guess. Pin both sides."""
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "pkg" / "cli.py"))
    assert cli._kit_dir() is None
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert cli._kit_dir() == tmp_path


def test_install_does_not_clobber_existing_config(install_env):
    home, runs, rec = install_env
    home.mkdir(parents=True)
    (home / "config").write_text(
        "[identity]\nagent_id = Alex\n[server]\nkind = paths\nscheme = https\n"
        "base_url = https://already.example\nverify_tls = true\nca_path = os\n",
        encoding="utf-8",
    )
    cli.main(["install", "--runtime", "claude"])
    cfg = configparser.ConfigParser()
    cfg.read(home / "config")
    assert cfg["server"]["base_url"] == "https://already.example"


def test_install_without_tty_prompts_nothing(install_env, monkeypatch, capsys):
    """CI / `./install < /dev/null` must never block on input(). Any call to input() here
    would hang the suite, so stubbing it to explode is the assertion."""
    def boom(*a, **kw):
        raise AssertionError("install prompted with no TTY")

    monkeypatch.setattr("builtins.input", boom)
    monkeypatch.setattr(cli.wizard, "is_interactive", lambda *a: False)
    rc = cli.main(["install", "--runtime", "claude"])
    assert rc == 0
    # Still the placeholder -> the hand-edit NEXT STEPS line is still the right advice.
    assert "CHANGEME" in capsys.readouterr().out


def test_install_flags_write_config_without_a_tty(install_env, monkeypatch, capsys):
    monkeypatch.setattr(cli.wizard, "is_interactive", lambda *a: False)
    rc = cli.main([
        "install", "--runtime", "claude",
        "--agent-id", "ci-bot", "--host", "10.0.0.4",
    ])
    assert rc == 0
    home, _, _ = install_env
    cfg = configparser.ConfigParser()
    cfg.read(home / "config")
    assert cfg["identity"]["agent_id"] == "ci-bot"
    assert cfg["server"]["host"] == "10.0.0.4"
    # Identity is set, so don't tell the user to go edit the file they just configured.
    assert "CHANGEME" not in capsys.readouterr().out


def test_install_prompts_when_interactive(install_env, monkeypatch):
    monkeypatch.setattr(cli.wizard, "is_interactive", lambda *a: True)
    answers = iter(["Alex", "203.0.113.10", ""])
    monkeypatch.setattr("builtins.input", lambda _p: next(answers))

    rc = cli.main(["install", "--runtime", "claude"])
    assert rc == 0
    home, _, _ = install_env
    cfg = configparser.ConfigParser()
    cfg.read(home / "config")
    assert cfg["identity"]["agent_id"] == "Alex"
    assert cfg["server"]["host"] == "203.0.113.10"


def test_install_non_interactive_flag_beats_a_tty(install_env, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("--non-interactive still prompted")

    monkeypatch.setattr(cli.wizard, "is_interactive", lambda *a: True)
    monkeypatch.setattr("builtins.input", boom)
    assert cli.main(["install", "--runtime", "claude", "--non-interactive"]) == 0


def test_install_failure_is_fail_loud_not_traceback(install_env, monkeypatch, capsys):
    """A failing step prints a firekeep:-prefixed message naming the step and
    returns nonzero — never a raw traceback at a teammate."""
    import subprocess

    from firekeep_client import cli

    def boom(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    # Override the fixture's _run stub so the failure path is exercised.
    monkeypatch.setattr(cli, "_run", boom)
    rc = cli.main(["install", "--runtime", "claude"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "firekeep: install failed at" in err
    assert "create venv" in err


def test_install_timeout_is_bounded_and_named(install_env, monkeypatch, capsys):
    import subprocess

    from firekeep_client import cli

    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))

    monkeypatch.setattr(cli, "_run", hang)
    rc = cli.main(["install", "--runtime", "claude"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "timed out" in err and "FIREKEEP_INSTALL_TIMEOUT" in err


def test_run_passes_bounded_timeout(monkeypatch):
    from firekeep_client import cli

    seen = {}

    def capture(cmd, **kw):
        seen.update(kw)

    monkeypatch.setattr(cli.subprocess, "run", capture)
    cli._run(["echo", "x"])
    assert seen.get("timeout") == cli._INSTALL_TIMEOUT > 0


def test_install_dist_base_flag_is_written(install_env, monkeypatch):
    monkeypatch.setattr(cli.wizard, "is_interactive", lambda *a: False)
    rc = cli.main(["install", "--runtime", "claude", "--dist-base", "http://gl/rel/v1"])
    assert rc == 0
    home, _, _ = install_env
    cfg = configparser.ConfigParser()
    cfg.read(home / "config")
    assert cfg["dist"]["base_url"] == "http://gl/rel/v1"


def test_install_dist_base_is_written_on_the_interactive_path(install_env, monkeypatch):
    """The interactive path is the one the bootstrap installer actually uses, so a silent
    regression there would break `firekeep update` for every real teammate while the non-interactive test stayed green."""
    monkeypatch.setattr(cli.wizard, "is_interactive", lambda *a: True)
    answers = iter(["Alex", "203.0.113.10", ""])
    monkeypatch.setattr("builtins.input", lambda _p: next(answers))

    rc = cli.main(["install", "--runtime", "claude", "--dist-base", "http://gl/rel/v1"])
    assert rc == 0
    home, _, _ = install_env
    cfg = configparser.ConfigParser()
    cfg.read(home / "config")
    # Assert dist base_url was written
    assert cfg["dist"]["base_url"] == "http://gl/rel/v1"
    # Also assert the prompted values still land (proving --dist-base doesn't disturb the wizard)
    assert cfg["identity"]["agent_id"] == "Alex"
    assert cfg["server"]["host"] == "203.0.113.10"


def test_create_venv_recreates_a_pipless_venv(tmp_path, monkeypatch):
    """A bootstrap that died between `uv venv` and `uv pip install` leaves a venv with a
    python but NO pip (uv venvs ship none). _create_venv used to short-circuit on 'bin dir
    exists' and the install then crashed in _pip_install — it must rebuild instead."""
    venv = tmp_path / "venv"
    bindir = venv / ("Scripts" if cli.os.name == "nt" else "bin")
    bindir.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(cli, "_run", lambda cmd, **kw: calls.append(list(cmd)))
    monkeypatch.setattr(cli, "_venv_has_pip", lambda v: False)
    cli._create_venv(venv)
    assert calls and "--clear" in calls[0], f"expected a --clear rebuild, got {calls}"


def test_create_venv_skips_a_healthy_venv(tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    bindir = venv / ("Scripts" if cli.os.name == "nt" else "bin")
    bindir.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(cli, "_run", lambda cmd, **kw: calls.append(list(cmd)))
    monkeypatch.setattr(cli, "_venv_has_pip", lambda v: True)
    cli._create_venv(venv)
    assert calls == []


def test_installed_venv_run_never_touches_the_venv(install_env, monkeypatch):
    """Release-breaking bug caught by the 0.1.2 bootstrap acceptance run (2026-07-13):
    the bootstrap's uv venv ships NO pip by design, so when its wizard hand-off ran
    `firekeep install` (kit=None — the process IS the installed venv), the pip-less-venv
    hardening saw 'no pip' and `python -m venv --clear`-rebuilt the venv it was
    executing from, wiping the freshly installed kit (ModuleNotFoundError at the
    render step, bare pip-only venv left behind). With no kit dir there is nothing to
    reinstall afterwards, so rebuilding is never recoverable: cmd_install must not
    create OR rebuild the venv when kit is None."""
    home, runs, rec = install_env
    monkeypatch.setattr(cli, "_kit_dir", lambda: None)
    venv_bin = home / "venv" / ("Scripts" if os.name == "nt" else "bin")
    venv_bin.mkdir(parents=True)
    monkeypatch.setattr(cli, "_venv_has_pip", lambda v: False)  # exactly a uv venv

    rc = cli.main(["install", "--runtime", "claude"])
    assert rc == 0
    assert not runs, f"no venv/pip subprocess may run from the installed venv, got {runs}"
    assert rec.calls, "adapters must still be rendered"
