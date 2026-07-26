import textwrap
import pytest
from firekeep_client import cli, resolver, __version__

CONFIG = textwrap.dedent("""\
    [active]
    profile = personal

    [personal]
    kind = ports
    scheme = http
    host = 10.0.0.5
    verify_tls = false
    agent_id = tester

    [office]
    kind = paths
    scheme = https
    base_url = https://firekeep.office.example
    verify_tls = true
    ca_path = %(ca)s
    api_key = nxs_secretvalue
    agent_id = tester
""")


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    ca = tmp_path / "ca.crt"
    ca.write_text("dummy", encoding="utf-8")
    cfg = tmp_path / ".firekeep" / "config"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(CONFIG % {"ca": ca.as_posix()}, encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    # keep perms hermetic (no real icacls/chmod on the temp file)
    monkeypatch.setattr("firekeep_client.state._private", lambda p: None)
    return cfg


def test_config_path_honors_env(config_file, monkeypatch):
    assert cli._config_path() == config_file


def test_profile_use_flips_active(config_file):
    rc = cli.main(["profile", "use", "office"])
    assert rc == 0
    cfg = resolver.load_config(config_file)
    assert resolver.active_profile(cfg) == "office"


def test_profile_use_unknown_profile_fails_loud(config_file, capsys):
    rc = cli.main(["profile", "use", "nope"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown profile 'nope'" in err


def test_profile_show_redacts_key(config_file, capsys):
    cli.main(["profile", "use", "office"])
    rc = cli.main(["profile", "show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "active profile: office" in out
    assert "REDACTED" in out
    assert "nxs_secretvalue" not in out


def test_version_prints_anchor(config_file, capsys):
    rc = cli.main(["version"])
    assert rc == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help_returns_1(config_file, capsys):
    assert cli.main([]) == 1


_PIN_CFG = """\
[active]
profile = personal

[personal]
agent_id = tester

[office]
agent_id = tester
"""


def _pin_setup(tmp_path, monkeypatch):
    from firekeep_client import cli
    cfg = tmp_path / "config"
    cfg.write_text(_PIN_CFG, encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    rendered = []
    monkeypatch.setattr(cli, "_rerender_runtime", lambda rt: rendered.append(rt))
    return cli, cfg, rendered


def test_profile_pin_writes_and_rerenders(tmp_path, monkeypatch, capsys):
    cli, cfg, rendered = _pin_setup(tmp_path, monkeypatch)
    rc = cli.main(["profile", "pin", "kiro", "office"])
    assert rc == 0
    assert rendered == ["kiro"]
    assert "[pins]" in cfg.read_text() and "kiro = office" in cfg.read_text()


def test_profile_pin_rejects_unknown_profile(tmp_path, monkeypatch, capsys):
    cli, cfg, rendered = _pin_setup(tmp_path, monkeypatch)
    assert cli.main(["profile", "pin", "kiro", "nope"]) == 1
    assert rendered == []
    assert "unknown profile" in capsys.readouterr().err


def test_profile_pin_rejects_unsafe_name(tmp_path, monkeypatch, capsys):
    cli, cfg, rendered = _pin_setup(tmp_path, monkeypatch)
    assert cli.main(["profile", "pin", "kiro", "my office"]) == 1
    assert rendered == []


def test_profile_pin_rejects_reserved_section_names(tmp_path, monkeypatch, capsys):
    """[active]/[pins]/[dist] are structural sections, not profiles. 'active' is the
    load-bearing case: it EXISTS as a section, so without the reserved-name guard the
    has_section check would wave `pin kiro active` straight through to render."""
    cli, cfg, rendered = _pin_setup(tmp_path, monkeypatch)
    for name in ("active", "pins", "dist"):
        assert cli.main(["profile", "pin", "kiro", name]) == 1
        assert "reserved" in capsys.readouterr().err
    assert rendered == []
    assert "[pins]" not in cfg.read_text()


def test_profile_unpin_roundtrip(tmp_path, monkeypatch):
    cli, cfg, rendered = _pin_setup(tmp_path, monkeypatch)
    assert cli.main(["profile", "pin", "kiro", "office"]) == 0
    assert cli.main(["profile", "unpin", "kiro"]) == 0
    assert "[pins]" not in cfg.read_text()
    assert rendered == ["kiro", "kiro"]


def test_profile_show_lists_pins(tmp_path, monkeypatch, capsys):
    cli, cfg, rendered = _pin_setup(tmp_path, monkeypatch)
    cli.main(["profile", "pin", "kiro", "office"])
    capsys.readouterr()
    cli.main(["profile", "show"])
    assert "pin: kiro -> office" in capsys.readouterr().out


def test_env_override_notice(tmp_path, monkeypatch, capsys):
    cli, cfg, rendered = _pin_setup(tmp_path, monkeypatch)
    monkeypatch.setenv("FIREKEEP_PROFILE", "office")
    cli.main(["profile", "show"])
    assert "FIREKEEP_PROFILE=office overrides" in capsys.readouterr().out


def test_env_override_notice_names_the_overridden_active_profile(tmp_path, monkeypatch, capsys):
    """Best-effort enrichment: when the config is readable, the notice names WHICH
    [active] profile the env var is overriding."""
    cli, cfg, rendered = _pin_setup(tmp_path, monkeypatch)
    monkeypatch.setenv("FIREKEEP_PROFILE", "office")
    cli.main(["profile", "show"])
    assert "overrides [active] profile 'personal' for this shell" in capsys.readouterr().out


def test_env_override_notice_degrades_without_config(tmp_path, monkeypatch, capsys):
    """No config at all: the notice must still print (generic text), never raise."""
    from firekeep_client import cli
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "missing"))
    monkeypatch.setenv("FIREKEEP_PROFILE", "office")
    cli._env_profile_notice()
    out = capsys.readouterr().out
    assert "FIREKEEP_PROFILE=office overrides the [active] profile" in out


def test_night_shift_maps_args_and_exit_codes(monkeypatch):
    seen = {}

    def fake_run(max_tasks=5, dry_run=False, **kw):
        seen.update(max_tasks=max_tasks, dry_run=dry_run)
        return {"distilled": 2, "legacy": 1, "skipped": 0, "failed": 0}

    monkeypatch.setattr("firekeep_client.nightshift.run", fake_run)
    assert cli.main(["night-shift", "--max", "3", "--dry-run"]) == 0
    assert seen == {"max_tasks": 3, "dry_run": True}


def test_night_shift_error_exits_nonzero(monkeypatch):
    monkeypatch.setattr("firekeep_client.nightshift.run",
                        lambda **kw: {"distilled": 0, "legacy": 0, "skipped": 0,
                                      "failed": 0, "error": "LM Studio unreachable"})
    assert cli.main(["night-shift"]) == 1
