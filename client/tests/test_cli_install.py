import configparser
import json
import os
import tomllib
from pathlib import Path

import pytest
from firekeep_client import cli, wizard


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
    # Deterministic side-by-side version: checkout installs provision
    # home/venvs/<kit-version> and flip `current` at it, so these tests pin the
    # version rather than parsing the real pyproject.toml — otherwise every
    # release bump would ripple through this file's layout assertions.
    monkeypatch.setattr(cli, "_kit_version", lambda kit: "1.2.3")
    runs = []

    def fake_run(cmd, **kw):
        runs.append(list(cmd))
        # Mirror the ONE side effect cmd_install depends on: `python -m venv
        # <path>` creates the directory. _point_current then aliases `current`
        # at it — and a Windows junction (a real one is created when these tests
        # run on Windows) cannot be made onto a missing target, so without this
        # every checkout-install test dies at the flip instead of its assertion.
        if "-m" in cmd and "venv" in cmd:
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(cli, "_run", fake_run)
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


def _checkout(tmp_path, monkeypatch, *, dexes=("symdex", "docdex", "maildex")):
    """A monorepo checkout: the kit dir plus whichever sibling dex dirs exist."""
    kit = tmp_path / "client"
    kit.mkdir(parents=True, exist_ok=True)
    (kit / "pyproject.toml").write_text("[project]\nname='firekeep-client'\n")
    for name in dexes:
        sibling = tmp_path / name
        sibling.mkdir(exist_ok=True)
        (sibling / "pyproject.toml").write_text(f"[project]\nname='firekeep-{name}'\n")
    monkeypatch.setattr(cli, "_kit_dir", lambda: kit)
    return kit


def test_checkout_install_uses_local_symdex_dir(install_env, monkeypatch, tmp_path):
    # From a checkout, symdex installs from the sibling dir BY PATH, never by name.
    calls = []
    monkeypatch.setattr(cli, "_pip_install", lambda py, *pkgs, **k: calls.append(pkgs))
    _checkout(tmp_path, monkeypatch)

    rc = cli.main(["install", "--runtime", "claude"])
    assert rc == 0
    installed = [p for pkgs in calls for p in pkgs]
    assert str(tmp_path / "symdex") in installed
    assert "firekeep-symdex" not in installed  # NEVER by name


def test_checkout_install_uses_local_docdex_dir(install_env, monkeypatch, tmp_path):
    """The docdex twin. `firekeep-docdex` is an unclaimed name on PyPI, which is
    exactly the hazard `firekeep-client` and `firekeep-symdex` already guard
    against — a bare name would resolve to whatever a stranger uploads there."""
    calls = []
    monkeypatch.setattr(cli, "_pip_install", lambda py, *pkgs, **k: calls.append(pkgs))
    _checkout(tmp_path, monkeypatch)

    rc = cli.main(["install", "--runtime", "claude"])
    assert rc == 0
    installed = [p for pkgs in calls for p in pkgs]
    assert str(tmp_path / "docdex") in installed
    assert "firekeep-docdex" not in installed  # NEVER by name


def test_checkout_install_uses_local_maildex_dir(install_env, monkeypatch, tmp_path):
    """The third wheel, same rule: `firekeep-maildex` is an unclaimed name on
    PyPI, so a bare name would resolve to whatever a stranger uploads there —
    into a venv that is about to be handed a mailbox password."""
    calls = []
    monkeypatch.setattr(cli, "_pip_install", lambda py, *pkgs, **k: calls.append(pkgs))
    _checkout(tmp_path, monkeypatch)

    rc = cli.main(["install", "--runtime", "claude"])
    assert rc == 0
    installed = [p for pkgs in calls for p in pkgs]
    assert str(tmp_path / "maildex") in installed
    assert "firekeep-maildex" not in installed  # NEVER by name


def test_checkout_install_fails_loudly_without_the_maildex_dir(
    install_env, monkeypatch, tmp_path, capsys
):
    """A kit built from half a checkout would install cleanly and then have a
    dead `firekeep maildex` and a doctor row nobody can explain."""
    monkeypatch.setattr(cli, "_pip_install", lambda py, *pkgs, **k: None)
    _checkout(tmp_path, monkeypatch, dexes=("symdex", "docdex"))

    assert cli.main(["install", "--runtime", "claude"]) == 1
    err = capsys.readouterr().err
    assert "maildex source not found" in err
    assert "incomplete checkout" in err


def test_checkout_install_fails_loudly_without_the_docdex_dir(
    install_env, monkeypatch, tmp_path, capsys
):
    """Same shape as symdex's: a checkout missing a sibling dex dir is an
    incomplete checkout, and a silent skip would ship a kit whose `firekeep
    docdex` and doctor rows are dead with no explanation."""
    monkeypatch.setattr(cli, "_pip_install", lambda py, *pkgs, **k: None)
    _checkout(tmp_path, monkeypatch, dexes=("symdex",))

    assert cli.main(["install", "--runtime", "claude"]) == 1
    err = capsys.readouterr().err
    assert "docdex source not found" in err
    assert "incomplete checkout" in err


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
    # Side-by-side layout: the venv is provisioned AT its final versioned path
    # (venvs are not relocatable — pyvenv.cfg bakes the absolute path), never at
    # the legacy home/venv location.
    venv_cmds = [cmd for cmd in runs if "venv" in cmd and "-m" in cmd]
    assert venv_cmds, "expected a venv-provision invocation"
    assert Path(venv_cmds[0][-1]) == home / "venvs" / "1.2.3"
    assert "firekeep-symdex" not in blob
    assert "firekeep-docdex" not in blob
    assert "firekeep-maildex" not in blob
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
    # The launcher must route through the `current` alias — the versioned
    # venvs/<V> dir is GC-able, and the legacy home/venv dir is what the
    # side-by-side layout retires. Only `current` survives every update.
    assert called_venv_bin == home / "current" / ("Scripts" if os.name == "nt" else "bin")


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
    # Rendered surfaces reference the `current` alias so the embedded paths stay
    # literally identical across updates (the flip retargets the alias, never
    # the configs) — see test_adapters_never_receive_a_versioned_venvs_path for
    # the inverse guard.
    expected = home / "current" / ("Scripts" if os.name == "nt" else "bin")
    assert venv_bin == expected


def test_checkout_install_points_current_at_the_versioned_venv(install_env):
    """Side-by-side guard (a): after a checkout install, home/current resolves to
    home/venvs/<kit-version>.

    `current` is the single alias every rendered surface (shims, all four
    adapters, doctor) routes through; a checkout install that provisioned the
    versioned venv but failed to flip the alias would leave every rendered
    config pointing at nothing — a dead client that looks installed. The flip
    is a REAL junction on Windows / symlink on POSIX (cli._point_current), so
    this exercises the actual link primitive on whichever OS runs the suite."""
    home, runs, rec = install_env
    assert cli.main(["install", "--runtime", "claude"]) == 0
    current = home / "current"
    versioned = home / "venvs" / "1.2.3"
    assert versioned.is_dir(), "the versioned venv must be provisioned at its final path"
    assert current.exists(), "the current alias must exist after a checkout install"
    assert current.resolve() == versioned.resolve()


def test_adapters_never_receive_a_versioned_venvs_path(install_env):
    """Side-by-side guard (b): adapters render against `current`, NEVER against
    venvs/<version>.

    A rendered config embedding the versioned path would pin that runtime to a
    directory a later update's GC removes — the config keeps working right up
    until the sweep, then every MCP spawn dies file-not-found with no visible
    cause. Routing through `current` is what makes updates render-free AND makes
    old venvs safely collectable; both halves break if even one adapter sees the
    versioned path."""
    home, runs, rec = install_env
    assert cli.main(["install", "--runtime", "all"]) == 0
    assert len(rec.calls) == 4
    for venv_bin in rec.calls:
        assert venv_bin.parent == home / "current", (
            f"adapter rendered against {venv_bin}, not the current alias"
        )
        assert "venvs" not in venv_bin.parts, (
            f"adapter received a GC-able versioned path: {venv_bin}"
        )


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
    monkeypatch.setattr("firekeep_client.wizard.prompt_config",
                        lambda cfg, **k: wizard.Plan(cfg, wizard.EXISTING_SERVER))
    cli.main(["install"])
    assert len(rec.calls) == 4


def test_explicit_runtime_renders_only_that_adapter(install_env, monkeypatch):
    # Explicit --runtime remains the targeted re-render/repair path.
    home, runs, rec = install_env
    monkeypatch.setattr("firekeep_client.wizard.is_interactive", lambda *a, **k: True)
    monkeypatch.setattr("firekeep_client.wizard.prompt_config",
                        lambda cfg, **k: wizard.Plan(cfg, wizard.EXISTING_SERVER))
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
    monkeypatch.setenv("XDG_CONFIG_HOME", str(user_home / ".config"))
    monkeypatch.setenv("FIREKEEP_CONFIG", str(firekeep_home / "config"))
    monkeypatch.setattr(cli, "_kit_dir", lambda: None)
    monkeypatch.setattr(cli.state, "_private", lambda _p: None)
    monkeypatch.setattr(cli.pathenv, "ensure_on_path", lambda *a, **k: [])
    # Native kiro activation is covered in test_kiro.py; do not launch a real client
    # process from this installer topology test.
    monkeypatch.setattr("firekeep_client.adapters.kiro.shutil.which", lambda _name: None)

    assert cli.main(["install", "--non-interactive", "--no-modify-path"]) == 0

    expected = {"firekeep"}
    claude = json.loads((user_home / ".claude.json").read_text(encoding="utf-8"))
    codex = tomllib.loads(
        (user_home / ".codex" / "config.toml").read_text(encoding="utf-8")
    )
    codex_instructions = (
        user_home / ".codex" / "AGENTS.md"
    ).read_text(encoding="utf-8")
    kiro = json.loads(
        (user_home / ".kiro" / "agents" / "firekeep.json").read_text(encoding="utf-8")
    )
    opencode = json.loads(
        (user_home / ".config" / "opencode" / "opencode.json").read_text(encoding="utf-8")
    )
    assert set(claude["mcpServers"]) == expected
    assert set(codex["mcp_servers"]) == expected
    assert "decision_board" in codex_instructions
    assert "memory_recall" in codex_instructions
    assert set(kiro["mcpServers"]) == expected
    assert set(opencode["mcp"]) == expected


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
    # A pre-0.1.35 install has no `current` link: _venv_root falls back to the
    # legacy home/venv so a re-render keeps working until the first
    # side-by-side update migrates the layout.
    assert rec.calls[0] == home / "venv" / ("Scripts" if os.name == "nt" else "bin")
    assert "skipping pip" in capsys.readouterr().out


def test_installed_venv_run_renders_through_current_when_present(install_env, monkeypatch):
    """The release bootstrap's wizard hand-off runs `firekeep install` FROM the
    freshly provisioned venvs/<V> (kit=None) after flipping `current` — the
    adapters it renders must reference the alias, not the versioned dir the
    process happens to be executing from. This is the installed-run half of the
    guard test_adapters_never_receive_a_versioned_venvs_path pins for checkout
    installs."""
    home, runs, rec = install_env
    monkeypatch.setattr(cli, "_kit_dir", lambda: None)
    versioned = home / "venvs" / "1.2.3"
    versioned.mkdir(parents=True)
    cli._point_current(home, versioned)

    assert cli.main(["install", "--runtime", "claude"]) == 0
    assert rec.calls == [home / "current" / ("Scripts" if os.name == "nt" else "bin")]


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
    # "3" is the routing answer for "it is already running", the only branch
    # where a host and key are answerable. A fresh machine is asked WHERE its
    # server is before being asked to describe it.
    # Trailing "" skips the last prompt: the optional "other MCP client" rules
    # file. Blank means "no generic runtime", which is what these tests assume.
    answers = iter(["Alex", "3", "203.0.113.10", "", ""])
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


def test_install_join_is_zero_prompt_even_with_a_tty(install_env, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("install --join prompted despite carrying every answer")

    calls = []
    monkeypatch.setattr(cli.wizard, "is_interactive", lambda *a: True)
    monkeypatch.setattr("builtins.input", boom)
    monkeypatch.setattr(
        "firekeep_client.join.join",
        lambda code, **kwargs: calls.append((code, kwargs)) or 0,
    )
    assert cli.main(["install", "--join", "fk_join_test", "--runtime", "all"]) == 0
    assert calls == [("fk_join_test", {"agent_id": None})]
    assert len(install_env[2].calls) == 4


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
    # "3" is the routing answer for "it is already running", the only branch
    # where a host and key are answerable. A fresh machine is asked WHERE its
    # server is before being asked to describe it.
    # Trailing "" skips the last prompt: the optional "other MCP client" rules
    # file. Blank means "no generic runtime", which is what these tests assume.
    answers = iter(["Alex", "3", "203.0.113.10", "", ""])
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
    create OR rebuild the venv when kit is None.

    Under the side-by-side layout the kit=None venv is DERIVED from
    sys.executable (it may live at venvs/<V> or the legacy venv/ — the code must
    never guess), so no venv path is assumed here either; the invariant is
    simply that NO venv/pip subprocess runs. The _venv_has_pip stub stays: if a
    regression ever routes kit=None through _create_venv again, 'no pip' forces
    the destructive --clear branch and the `runs` assertion catches it."""
    home, runs, rec = install_env
    monkeypatch.setattr(cli, "_kit_dir", lambda: None)
    venv_bin = home / "venv" / ("Scripts" if os.name == "nt" else "bin")
    venv_bin.mkdir(parents=True)
    monkeypatch.setattr(cli, "_venv_has_pip", lambda v: False)  # exactly a uv venv

    rc = cli.main(["install", "--runtime", "claude"])
    assert rc == 0
    assert not runs, f"no venv/pip subprocess may run from the installed venv, got {runs}"
    assert rec.calls, "adapters must still be rendered"
