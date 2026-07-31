import json
import sys

import pytest

from firekeep_client.adapters import get_adapter


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    # Redirect Path.home() to a tmp dir on both Windows (USERPROFILE) and POSIX (HOME) — never real ~.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _read(p):
    return json.loads(p.read_text())


def _exe(path):
    """Expected console-script path for the CURRENT (real, unmocked) host platform —
    mirrors the win32 `.exe` handling in firekeep_client.adapters.base.console_script_path."""
    text = str(path)
    return text + ".exe" if sys.platform == "win32" else text


def test_claude_render_writes_shim_servers_and_hooks(fake_home, tmp_path):
    venv_bin = tmp_path / "venv" / "Scripts"
    get_adapter("claude").render(venv_bin=venv_bin)

    cfg = _read(fake_home / ".claude.json")
    assert cfg["mcpServers"]["firekeep-cortex"] == {
        "type": "stdio", "command": _exe(venv_bin / "firekeep-shim"),
        "args": ["--service", "cortex"]}
    # symdex is always-on: present unconditionally with its stdio-local command, no args
    assert cfg["mcpServers"]["firekeep-symdex"] == {
        "type": "stdio", "command": _exe(venv_bin / "firekeep-symdex"), "args": []}

    settings = _read(fake_home / ".claude" / "settings.json")
    assert settings["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "1"
    ss = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert ss.endswith("-m firekeep_client.hooks session_start")
    assert settings["hooks"]["Stop"][0]["hooks"][0]["command"].endswith(
        "-m firekeep_client.hooks stop")
    assert settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"].endswith(
        "-m firekeep_client.hooks prompt")
    pre = settings["hooks"]["PreToolUse"][0]
    assert pre["matcher"] == "^(Edit|Write)$"
    # exit-code remap: rendered WITH --block-exit 2 so pre_tool's rc=1 (gateway
    # block/rethink) AND rc=2 (lease conflict) both actually block Claude's PreToolUse
    # gate, which otherwise blocks ONLY on exit 2.
    assert pre["hooks"][0]["command"].endswith("-m firekeep_client.hooks pre_tool --block-exit 2")
    post = settings["hooks"]["PostToolUse"][0]
    assert post["matcher"] == "^(Edit|Write|MultiEdit|Bash)$"
    # post_tool renders WITHOUT the flag -- it always returns 0, so the remap is a no-op.
    assert post["hooks"][0]["command"].endswith("-m firekeep_client.hooks post_tool")


def test_claude_render_is_non_clobbering(fake_home, tmp_path):
    (fake_home / ".claude.json").write_text(json.dumps(
        {"mcpServers": {"other-mcp": {"type": "http", "url": "http://x"}}}))
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / "settings.json").write_text(json.dumps({
        "env": {"FOO": "bar"},
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo foreign"}]}]},
    }))

    venv_bin = tmp_path / "venv" / "Scripts"
    get_adapter("claude").render(venv_bin=venv_bin)

    cfg = _read(fake_home / ".claude.json")
    assert cfg["mcpServers"]["other-mcp"] == {"type": "http", "url": "http://x"}  # foreign survived
    assert "firekeep-cortex" in cfg["mcpServers"]

    settings = _read(fake_home / ".claude" / "settings.json")
    assert settings["env"]["FOO"] == "bar"                                       # foreign env survived
    cmds = [h["command"] for g in settings["hooks"]["SessionStart"] for h in g["hooks"]]
    assert "echo foreign" in cmds                                                # foreign hook survived
    assert any("firekeep_client.hooks session_start" in c for c in cmds)            # firekeep group added


def test_claude_unrender_removes_only_firekeep(fake_home, tmp_path):
    (fake_home / ".claude.json").write_text(json.dumps(
        {"mcpServers": {"other-mcp": {"type": "http", "url": "http://x"}}}))
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / "settings.json").write_text(json.dumps({
        "env": {"FOO": "bar"},
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo foreign"}]}]},
    }))
    venv_bin = tmp_path / "venv" / "Scripts"
    adapter = get_adapter("claude")
    adapter.render(venv_bin=venv_bin)
    adapter.unrender()

    cfg = _read(fake_home / ".claude.json")
    assert cfg["mcpServers"] == {"other-mcp": {"type": "http", "url": "http://x"}}
    settings = _read(fake_home / ".claude" / "settings.json")
    assert settings["env"] == {"FOO": "bar"}
    cmds = [h["command"] for g in settings["hooks"]["SessionStart"] for h in g["hooks"]]
    assert cmds == ["echo foreign"]


def test_claude_render_appends_exe_on_win32(fake_home, tmp_path, monkeypatch):
    """Pins the Windows console-script fix: pip installs `firekeep-shim.exe` / `python.exe`
    in Scripts\\, not the extensionless name — real regardless of the host actually running
    this suite, since we force the branch explicitly rather than relying on the host OS."""
    monkeypatch.setattr(sys, "platform", "win32")
    venv_bin = tmp_path / "venv" / "Scripts"
    get_adapter("claude").render(venv_bin=venv_bin)

    cfg = _read(fake_home / ".claude.json")
    assert cfg["mcpServers"]["firekeep-cortex"]["command"] == str(venv_bin / "firekeep-shim") + ".exe"
    assert cfg["mcpServers"]["firekeep-symdex"]["command"] == str(venv_bin / "firekeep-symdex") + ".exe"

    settings = _read(fake_home / ".claude" / "settings.json")
    ss_cmd = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    # hook commands are bash-executed -> forward slashes (MCP commands above are
    # direct-spawned and keep native separators)
    assert ss_cmd.startswith(str(venv_bin / "python").replace("\\", "/") + ".exe ")
    assert "\\" not in ss_cmd
    assert ss_cmd.endswith("-m firekeep_client.hooks session_start")


def test_claude_render_no_exe_on_posix(fake_home, tmp_path, monkeypatch):
    """Non-Windows hosts (CI, Linux/macOS local installs) must NOT gain a `.exe` suffix —
    pins the other side of the branch so this test suite covers both regardless of host OS."""
    monkeypatch.setattr(sys, "platform", "linux")
    venv_bin = tmp_path / "venv" / "bin"
    get_adapter("claude").render(venv_bin=venv_bin)

    cfg = _read(fake_home / ".claude.json")
    assert cfg["mcpServers"]["firekeep-cortex"]["command"] == str(venv_bin / "firekeep-shim")
    assert cfg["mcpServers"]["firekeep-symdex"]["command"] == str(venv_bin / "firekeep-symdex")

    settings = _read(fake_home / ".claude" / "settings.json")
    ss_cmd = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert ss_cmd.startswith(str(venv_bin / "python").replace("\\", "/") + " ")


def test_claude_render_is_idempotent(fake_home, tmp_path):
    """Re-rendering (e.g. re-running setup) must not duplicate MCP servers or hook groups."""
    venv_bin = tmp_path / "venv" / "Scripts"
    adapter = get_adapter("claude")
    adapter.render(venv_bin=venv_bin)
    adapter.render(venv_bin=venv_bin)

    cfg = _read(fake_home / ".claude.json")
    assert set(cfg["mcpServers"]) == {
        "firekeep-cortex", "firekeep-bridge", "firekeep-sentinel", "firekeep-relay",
        "firekeep-symdex", "firekeep-decision"}

    settings = _read(fake_home / ".claude" / "settings.json")
    for event in ("SessionStart", "Stop", "UserPromptSubmit", "PreToolUse", "PostToolUse"):
        assert len(settings["hooks"][event]) == 1  # exactly one (firekeep) group, not duplicated


def _legacy_settings():
    """The real shape left behind on a machine upgraded from the retired local-setup.sh:
    the bash hook groups AND (from a subsequent kit install) the hook-core groups, side by
    side. The bash scripts no longer exist, so every session errors on them."""
    return {
        "env": {
            "NEXUS_CORTEX_URL": "http://203.0.113.10:8100",
            "NEXUS_RELAY_URL": "http://203.0.113.10:8050",
            "FIREKEEP_AGENT_ID": "Alex",
            "FOO": "bar",
        },
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command",
                            "command": "bash /repo/scripts/briefing.sh", "timeout": 15}]},
                {"hooks": [{"type": "command",
                            "command": "/old/venv/bin/python -m firekeep_client.hooks session_start",
                            "timeout": 15}]},
            ],
            "Stop": [
                {"hooks": [{"type": "command",
                            "command": "bash /repo/scripts/debrief.sh", "timeout": 5}]},
            ],
            "PreToolUse": [
                {"matcher": "^(Edit|Write)$",
                 "hooks": [{"type": "command",
                            "command": "bash /repo/scripts/multi-agent-precheck.sh"}]},
            ],
            "PreCompact": [
                {"hooks": [{"type": "command", "command": "echo '{\"systemMessage\":\"...\"}'"}]},
            ],
        },
    }


def test_claude_render_migrates_legacy_bash_hooks(fake_home, tmp_path):
    """The retired bash hooks are firekeep-owned, not foreign: render() must collapse each
    event down to exactly ONE firekeep group. Pins the observed bug — the bash groups lack the
    `firekeep_client.hooks` marker, so the non-clobbering merge preserved them and every
    session fired `briefing.sh: No such file or directory` alongside the real hook core."""
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / "settings.json").write_text(json.dumps(_legacy_settings()))

    get_adapter("claude").render(venv_bin=tmp_path / "venv" / "bin")
    settings = _read(fake_home / ".claude" / "settings.json")

    blob = json.dumps(settings["hooks"])
    assert "briefing.sh" not in blob
    assert "debrief.sh" not in blob
    assert "multi-agent-precheck.sh" not in blob

    for event in ("SessionStart", "Stop", "UserPromptSubmit", "PreToolUse", "PostToolUse"):
        groups = settings["hooks"][event]
        assert len(groups) == 1, f"{event} should collapse to one firekeep group, got {groups}"
        assert "firekeep_client.hooks" in groups[0]["hooks"][0]["command"]


def test_claude_render_drops_retired_url_env_but_keeps_agent_id(fake_home, tmp_path):
    """URL/auth/TLS come from ~/.firekeep/config via the resolver; the FIREKEEP_*_URL keys the old
    installer wrote are dead and only contradict it. FIREKEEP_AGENT_ID is a LIVE override
    (resolver.agent_id reads it) — dropping it would silently re-attribute the user's work."""
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / "settings.json").write_text(json.dumps(_legacy_settings()))

    get_adapter("claude").render(venv_bin=tmp_path / "venv" / "bin")
    env = _read(fake_home / ".claude" / "settings.json")["env"]

    assert "NEXUS_CORTEX_URL" not in env
    assert "NEXUS_RELAY_URL" not in env
    assert env["FIREKEEP_AGENT_ID"] == "Alex"
    assert env["FOO"] == "bar"


def test_claude_render_adds_its_precompact_group_beside_the_legacy_echo(fake_home, tmp_path):
    """The kit now renders a PreCompact hook of its own. The legacy echo hook is
    still deliberately treated as foreign-but-working: migration removes what is
    BROKEN, not everything the old installer happened to write. Both must survive.
    """
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / "settings.json").write_text(json.dumps(_legacy_settings()))
    settings_path = fake_home / ".claude" / "settings.json"

    adapter = get_adapter("claude")
    adapter.render(venv_bin=tmp_path / "venv" / "bin")
    groups = _read(settings_path)["hooks"]["PreCompact"]

    commands = [h["command"] for g in groups for h in g["hooks"]]
    assert any("systemMessage" in c for c in commands), "legacy echo hook was clobbered"
    assert any(c.endswith("-m firekeep_client.hooks precompact") for c in commands)

    # Re-render with the foreign group still present: upsert_hook_group must collapse
    # to exactly one firekeep group beside the untouched legacy one, not accumulate a
    # second firekeep group. This is the specific combination neither the plain
    # idempotency test (clean home, no foreign group) nor the check above (foreign
    # group present, rendered once) exercises.
    before = settings_path.read_text(encoding="utf-8")
    adapter.render(venv_bin=tmp_path / "venv" / "bin")
    after = settings_path.read_text(encoding="utf-8")
    assert after == before, "re-render with a foreign sibling present must not rewrite the file"

    groups = _read(settings_path)["hooks"]["PreCompact"]
    assert len(groups) == 2
    commands = [h["command"] for g in groups for h in g["hooks"]]
    assert any("systemMessage" in c for c in commands), "legacy echo hook was clobbered on re-render"
    firekeep_cmds = [c for c in commands if "firekeep_client.hooks" in c]
    assert len(firekeep_cmds) == 1, f"expected exactly one firekeep PreCompact command, got {firekeep_cmds}"


def test_claude_unrender_removes_only_our_precompact_group(fake_home, tmp_path):
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / "settings.json").write_text(json.dumps(_legacy_settings()))
    adapter = get_adapter("claude")
    adapter.render(venv_bin=tmp_path / "venv" / "bin")
    adapter.unrender()

    groups = _read(fake_home / ".claude" / "settings.json")["hooks"].get("PreCompact", [])
    commands = [h["command"] for g in groups for h in g["hooks"]]
    assert any("systemMessage" in c for c in commands)      # legacy survives unrender
    assert not any("firekeep_client.hooks" in c for c in commands)


def test_claude_unrender_removes_legacy_bash_hooks(fake_home, tmp_path):
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / "settings.json").write_text(json.dumps(_legacy_settings()))

    get_adapter("claude").unrender()
    settings = _read(fake_home / ".claude" / "settings.json")

    assert "SessionStart" not in settings["hooks"]  # only firekeep groups lived there
    assert "PreCompact" in settings["hooks"]        # foreign survives
    assert settings["env"] == {"FIREKEEP_AGENT_ID": "Alex", "FOO": "bar"}


_PINNED_CFG = """
[active]
profile = personal
[personal]
agent_id = tester
[office]
agent_id = tester
[pins]
claude = office
"""


def _write_cfg(tmp_path, monkeypatch, text):
    cfg = tmp_path / "config"
    cfg.write_text(text, encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    return cfg


def test_legacy_pinned_claude_renders_no_profile_artifacts(tmp_path, monkeypatch, fake_home):
    _write_cfg(tmp_path, monkeypatch, _PINNED_CFG)
    get_adapter("claude").render(venv_bin=tmp_path / "vbin")

    claude_json = _read(fake_home / ".claude.json")
    for name in ("firekeep-cortex", "firekeep-symdex", "firekeep-decision"):
        assert "env" not in claude_json["mcpServers"][name]
    settings = _read(fake_home / ".claude" / "settings.json")
    for groups in settings["hooks"].values():
        for g in groups:
            for h in g["hooks"]:
                if "firekeep_client.hooks" in h["command"]:
                    assert "--profile" not in h["command"]


def test_unpinned_render_has_no_env_or_profile(tmp_path, monkeypatch, fake_home):
    _write_cfg(tmp_path, monkeypatch, _PINNED_CFG.replace("[pins]\nclaude = office\n", ""))
    get_adapter("claude").render(venv_bin=tmp_path / "vbin")

    claude_json = _read(fake_home / ".claude.json")
    assert "env" not in claude_json["mcpServers"]["firekeep-cortex"]
    settings = _read(fake_home / ".claude" / "settings.json")
    text = json.dumps(settings)
    assert "--profile" not in text


def test_rerender_removes_legacy_pin_artifacts(tmp_path, monkeypatch, fake_home):
    """A re-render removes legacy env/argument carriers from owned entries."""
    cfg = _write_cfg(tmp_path, monkeypatch, _PINNED_CFG)
    adapter = get_adapter("claude")
    adapter.render(venv_bin=tmp_path / "vbin")
    cfg.write_text(_PINNED_CFG.replace("[pins]\nclaude = office\n", ""), encoding="utf-8")
    adapter.render(venv_bin=tmp_path / "vbin")

    claude_json = _read(fake_home / ".claude.json")
    assert "env" not in claude_json["mcpServers"]["firekeep-cortex"]


def test_render_without_config_is_unpinned(tmp_path, monkeypatch, fake_home):
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "does-not-exist"))
    get_adapter("claude").render(venv_bin=tmp_path / "vbin")  # must not raise


def test_claude_render_only_pretool_gets_block_exit_remap(fake_home, tmp_path):
    """Task 21-fix: Claude's PreToolUse process gate blocks ONLY on exit code 2, but
    pre_tool.run() returns 1 for an agent-gateway block/rethink -- without the adapter
    remapping that to a blocking exit, the gateway's 'block' decision is silently
    defeated. Pin: PreToolUse alone carries `--block-exit 2`; every other rendered
    hook command uses the bare dispatcher form with no extra flags."""
    venv_bin = tmp_path / "venv" / "Scripts"
    get_adapter("claude").render(venv_bin=venv_bin)
    settings = _read(fake_home / ".claude" / "settings.json")

    pre_cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert pre_cmd.endswith("-m firekeep_client.hooks pre_tool --block-exit 2")

    for event, core in (
        ("SessionStart", "session_start"),
        ("Stop", "stop"),
        ("UserPromptSubmit", "prompt"),
        ("PostToolUse", "post_tool"),
    ):
        cmd = settings["hooks"][event][0]["hooks"][0]["command"]
        assert cmd.endswith(f"-m firekeep_client.hooks {core}")
        assert "--block-exit" not in cmd
