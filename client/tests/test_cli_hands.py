"""`firekeep hands enable|disable|status|allow|chord|config|evidence` and the
doctor row.

Hands is a capability, not an index (Task 1, `dexes.KNOWN_DEXES["hands"]`,
`role="capability"`) — never seeded, opt-in only via `enable`. `enable` and
`disable` are the kit's own (they touch the venv and the registry); every
other action is a translator onto `firekeep_hands.cli.main`, imported lazily
so a kit without the wheel keeps every other command working.

PyPI squat guard (ruling, 2026-09-05): `firekeep-hands` is not yet published,
so `enable` refuses a bare install (no `--from` and no `--pypi`) rather than
`pip install`-ing a name a third party could still claim.
"""
import types

import pytest

from firekeep_client import cli, dexes


@pytest.fixture
def registry_home(tmp_path, monkeypatch):
    """Like test_cli_dex.py's fixture of the same name: leaves `is_installed`
    at its real probe, since these tests care whether hands' wheel is
    ACTUALLY present (it is not, until `enable` installs it)."""
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(tmp_path / "logs"))
    return tmp_path


def _args(**kw):
    base = {"action": None, "source": None, "pypi": False, "no_autostart": False,
            "purge": False, "rest": []}
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_enable_with_no_source_and_pypi_not_published_refuses(registry_home, capsys):
    assert cli.cmd_hands(_args(action="enable")) == 2
    err = capsys.readouterr().err
    assert "not yet published to PyPI" in err
    assert "hands" not in dexes.read_registry()


def test_enable_pypi_when_published_installs_registers_and_installs_autostart(
    registry_home, monkeypatch, capsys
):
    calls = []
    monkeypatch.setattr(cli, "_pip_install", lambda python, spec: calls.append(("pip", spec)))
    monkeypatch.setattr(dexes, "is_installed", lambda m: True)
    monkeypatch.setattr(cli, "_run_hands_broker", lambda argv: calls.append(("broker", tuple(argv))) or 0)
    monkeypatch.setattr(cli, "HANDS_PYPI_PUBLISHED", True)
    assert cli.cmd_hands(_args(action="enable", pypi=True)) == 0
    assert calls == [("pip", cli.HANDS_WHEEL_SPEC), ("broker", ("install-autostart",))]
    assert "hands" in dexes.read_registry()
    assert "next agent session" in capsys.readouterr().out


def test_enable_from_local_path_uses_that_path(registry_home, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cli, "_pip_install", lambda python, spec: calls.append(spec))
    monkeypatch.setattr(dexes, "is_installed", lambda m: True)
    monkeypatch.setattr(cli, "_run_hands_broker", lambda argv: 0)
    src = tmp_path / "hands"
    src.mkdir()
    (src / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert cli.cmd_hands(_args(action="enable", source=str(src))) == 0
    assert calls == [str(src)]


def test_the_registry_records_where_the_hands_wheel_actually_came_from(
    registry_home, monkeypatch, tmp_path
):
    """`source` is provenance, and Hands is the one wheel the bootstrap never
    bundles — so the registry's `"bundled"` default would have written a false
    statement about this machine into the user's own file."""
    monkeypatch.setattr(cli, "_pip_install", lambda python, spec: None)
    monkeypatch.setattr(dexes, "is_installed", lambda m: True)
    monkeypatch.setattr(cli, "_run_hands_broker", lambda argv: 0)

    src = tmp_path / "hands"
    src.mkdir()
    assert cli.cmd_hands(_args(action="enable", source=str(src))) == 0
    assert dexes.read_registry()["hands"]["source"] == "checkout"

    dexes.remove("hands")
    monkeypatch.setattr(cli, "HANDS_PYPI_PUBLISHED", True)
    assert cli.cmd_hands(_args(action="enable", pypi=True)) == 0
    assert dexes.read_registry()["hands"]["source"] == "pypi"


def test_a_dex_registered_without_a_source_is_still_bundled(registry_home):
    """The default keeps every existing caller — and the two tests in
    test_dexes.py that pin it — saying exactly what they said before."""
    dexes.add("symdex")
    assert dexes.read_registry()["symdex"]["source"] == "bundled"


def test_enable_refuses_to_register_when_import_probe_fails(registry_home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_pip_install", lambda python, spec: None)
    monkeypatch.setattr(dexes, "is_installed", lambda m: False)
    assert cli.cmd_hands(_args(action="enable", source="firekeep-hands==0.0.0")) == 1
    assert "hands" not in dexes.read_registry()
    assert "not importable" in capsys.readouterr().err


def test_disable_deregisters_and_removes_autostart(registry_home, monkeypatch):
    dexes.add("hands")
    calls = []
    monkeypatch.setattr(cli, "_run_hands_broker", lambda argv: calls.append(tuple(argv)) or 0)
    assert cli.cmd_hands(_args(action="disable")) == 0
    assert "hands" not in dexes.read_registry()
    assert calls == [("uninstall-autostart",)]


def test_disable_purge_removes_hands_dir(registry_home, monkeypatch, tmp_path):
    hands_dir = dexes.registry_path().parent / "hands"
    (hands_dir / "evidence").mkdir(parents=True)
    monkeypatch.setattr(cli, "_run_hands_broker", lambda argv: 0)
    assert cli.cmd_hands(_args(action="disable", purge=True)) == 0
    assert not hands_dir.exists()


def test_other_actions_delegate_to_the_wheel(monkeypatch):
    seen = []
    fake = types.SimpleNamespace(main=lambda argv: seen.append(list(argv)) or 0)
    monkeypatch.setitem(__import__("sys").modules, "firekeep_hands.cli", fake)
    monkeypatch.setitem(__import__("sys").modules, "firekeep_hands", types.SimpleNamespace(cli=fake))
    assert cli.cmd_hands(_args(action="allow", rest=["domain", "example.com"])) == 0
    assert seen == [["allow", "domain", "example.com"]]


def test_delegation_without_wheel_explains_enable(monkeypatch, capsys):
    import builtins
    real = builtins.__import__
    def fake_import(name, *a, **k):
        if name.startswith("firekeep_hands"):
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert cli.cmd_hands(_args(action="status")) == 1
    assert "firekeep hands enable" in capsys.readouterr().err


def test_doctor_hands_row_reports_broker(registry_home, monkeypatch):
    dexes.add("hands")
    monkeypatch.setattr(dexes, "is_installed", lambda m: True)
    monkeypatch.setattr(cli, "read_broker_health", lambda timeout=1.0: {"ok": True, "chord": "ctrl+alt+y", "listeners": {"chord": "active", "phone": "active"}})
    rows = dict((r[0], r) for r in cli._check_dexes())
    assert rows["hands"][1] == "ok"
    assert "chord ctrl+alt+y" in rows["hands"][2]
    monkeypatch.setattr(cli, "read_broker_health", lambda timeout=1.0: None)
    rows = dict((r[0], r) for r in cli._check_dexes())
    assert rows["hands"][1] == "warn"
    assert "broker not running" in rows["hands"][2]


def test_doctor_hands_row_warns_when_nothing_can_approve(registry_home, monkeypatch):
    """A broker that is UP but has no approval path refuses every protected step
    just as completely as one that is down, and says so nowhere else. With phone
    approvals off by default, a failed chord listener is exactly that state, so
    the row must warn and name the fix on both platforms — doctor cannot tell
    which one broke the listener."""
    dexes.add("hands")
    monkeypatch.setattr(dexes, "is_installed", lambda m: True)
    monkeypatch.setattr(cli, "read_broker_health", lambda timeout=1.0: {
        "ok": True, "chord": "ctrl+alt+y",
        "listeners": {"chord": "unavailable", "phone": "off"}})
    row = dict((r[0], r) for r in cli._check_dexes())["hands"]
    assert row[1] == "warn"
    assert "Input Monitoring" in row[2]
    assert "firekeep hands enable" in row[2]
    assert "phone_approvals true" in row[2]


def test_doctor_hands_row_stays_ok_when_the_phone_can_approve(registry_home, monkeypatch):
    """The phone is a real approval path. A dead chord listener with phone
    approvals ACTIVE is degraded, not broken, and must not warn."""
    dexes.add("hands")
    monkeypatch.setattr(dexes, "is_installed", lambda m: True)
    monkeypatch.setattr(cli, "read_broker_health", lambda timeout=1.0: {
        "ok": True, "chord": "ctrl+alt+y",
        "listeners": {"chord": "unavailable", "phone": "active"}})
    row = dict((r[0], r) for r in cli._check_dexes())["hands"]
    assert row[1] == "ok"


def test_doctor_hands_row_warns_when_phone_is_opted_in_but_offline(registry_home, monkeypatch):
    """`offline` means opted in with no Keep to post to — an approval path that
    does not exist, so it counts the same as `off`."""
    dexes.add("hands")
    monkeypatch.setattr(dexes, "is_installed", lambda m: True)
    monkeypatch.setattr(cli, "read_broker_health", lambda timeout=1.0: {
        "ok": True, "chord": "ctrl+alt+y",
        "listeners": {"chord": "unavailable", "phone": "offline"}})
    row = dict((r[0], r) for r in cli._check_dexes())["hands"]
    assert row[1] == "warn"
    assert "phone approvals are offline" in row[2]


def test_parser_keeps_from_out_of_rest():
    from firekeep_client import cli
    args = cli._build_parser().parse_args(["hands", "enable", "--from", "X:/hands", "--no-autostart"])
    assert (args.action, args.source, args.no_autostart, args.rest) == ("enable", "X:/hands", True, [])
    args = cli._build_parser().parse_args(["hands", "allow", "domain", "example.com"])
    assert (args.action, args.rest) == ("allow", ["domain", "example.com"])
    args = cli._build_parser().parse_args(["hands", "enable", "--pypi"])
    assert (args.action, args.pypi, args.rest) == ("enable", True, [])
