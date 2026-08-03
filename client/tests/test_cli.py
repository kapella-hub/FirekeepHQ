import textwrap

import pytest

from firekeep_client import __version__, cli


CONFIG = textwrap.dedent("""\
    [identity]
    agent_id = tester

    [server]
    kind = ports
    scheme = http
    host = 10.0.0.5
    verify_tls = false
""")


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    cfg = tmp_path / ".firekeep" / "config"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    monkeypatch.setattr("firekeep_client.state._private", lambda _p: None)
    return cfg


def test_config_path_honors_env(config_file):
    assert cli._config_path() == config_file


@pytest.mark.parametrize("args", [
    ["profile"],
    ["profile", "show"],
    ["profile", "use", "office"],
    ["profile", "pin", "kiro", "office"],
    ["profile", "unpin", "kiro"],
])
def test_profile_command_is_exit_2_deprecation_stub(config_file, capsys, args):
    before = config_file.read_bytes()
    assert cli.main(args) == 2
    err = capsys.readouterr().err
    assert "was removed" in err
    assert "[server]" in err
    assert str(config_file.resolve()) in err
    assert config_file.read_bytes() == before


def test_version_prints_anchor(config_file, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_check_versions", lambda _cfg: ("versions", "ok", "offline"))
    assert cli.main(["version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help_returns_1(config_file):
    assert cli.main([]) == 1


def test_retired_profile_env_is_a_doctor_warning(config_file, monkeypatch):
    monkeypatch.setenv("FIREKEEP_PROFILE", "office")
    row = cli._check_retired_profile_env()
    assert row[0:2] == ("retired-profile-env", "warn")
    assert "ignored" in row[2]
    assert str(config_file.resolve()) in row[2]


def test_no_retired_profile_env_means_no_row(config_file, monkeypatch):
    monkeypatch.delenv("FIREKEEP_PROFILE", raising=False)
    assert cli._check_retired_profile_env() is None


def test_night_shift_maps_args_and_exit_codes(monkeypatch):
    seen = {}

    def fake_run(max_tasks=5, dry_run=False, **_kw):
        seen.update(max_tasks=max_tasks, dry_run=dry_run)
        return {"distilled": 2, "legacy": 1, "skipped": 0, "failed": 0}

    monkeypatch.setattr("firekeep_client.nightshift.run", fake_run)
    assert cli.main(["night-shift", "--max", "3", "--dry-run"]) == 0
    assert seen == {"max_tasks": 3, "dry_run": True}


def test_night_shift_error_exits_nonzero(monkeypatch):
    monkeypatch.setattr(
        "firekeep_client.nightshift.run",
        lambda **_kw: {"distilled": 0, "legacy": 0, "skipped": 0,
                       "failed": 0, "error": "LM Studio unreachable"},
    )
    assert cli.main(["night-shift"]) == 1


# --- firekeep restore (2026-08-02) ------------------------------------------
# Snapshots without a recovery path are write-only machinery, and this repo has
# deleted features for exactly that (the corpus entity graph, "0 entities ever
# extracted"; ~161K BACKLINK edges never traversed). The CLI is what makes the
# snapshot store readable, so it is part of the feature, not polish on top.
def _mkrepo(tmp_path):
    import subprocess
    r = tmp_path / "proj"
    r.mkdir()
    def g(*a):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a],
                       cwd=str(r), capture_output=True, text=True, check=False)
    g("init", "-q")
    (r / "a.py").write_text("committed\n", encoding="utf-8")
    g("add", "-A"); g("commit", "-qm", "base")
    return r


def test_restore_list_shows_captured_snapshots(tmp_path, monkeypatch, capsys):
    from firekeep_client import cli, worktree_snapshot as ws
    monkeypatch.setenv("FIREKEEP_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    repo = _mkrepo(tmp_path)
    (repo / "a.py").write_text("work in progress\n", encoding="utf-8")
    snap = ws.capture(repo, reason="unit")
    monkeypatch.chdir(repo)

    assert cli.main(["restore", "--list"]) == 0
    out = capsys.readouterr().out
    assert snap.name in out
    assert "unit" in out


def test_restore_apply_brings_the_work_back(tmp_path, monkeypatch, capsys):
    """End-to-end through the CLI: the incident's command, then recovery."""
    import subprocess
    from firekeep_client import cli, worktree_snapshot as ws
    monkeypatch.setenv("FIREKEEP_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    repo = _mkrepo(tmp_path)
    (repo / "a.py").write_text("MY WORK\n", encoding="utf-8")
    snap = ws.capture(repo, reason="unit")
    subprocess.run(["git", "checkout", "--", "."], cwd=str(repo), capture_output=True)
    assert (repo / "a.py").read_text(encoding="utf-8") == "committed\n"
    monkeypatch.chdir(repo)

    assert cli.main(["restore", "--apply", snap.name]) == 0
    assert (repo / "a.py").read_text(encoding="utf-8") == "MY WORK\n"


def test_restore_list_is_calm_when_there_is_nothing(tmp_path, monkeypatch, capsys):
    from firekeep_client import cli
    monkeypatch.setenv("FIREKEEP_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    monkeypatch.chdir(_mkrepo(tmp_path))
    assert cli.main(["restore", "--list"]) == 0
    assert "no snapshots" in capsys.readouterr().out.lower()
