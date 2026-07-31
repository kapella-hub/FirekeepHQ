import json
import sys

import pytest

from firekeep_client.adapters import get_adapter


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _read(p):
    return json.loads(p.read_text())


def _exe(path):
    """Expected console-script path for the CURRENT (real, unmocked) host platform —
    mirrors the win32 `.exe` handling in firekeep_client.adapters.base.console_script_path
    (see test_claude.py's identical helper)."""
    text = str(path)
    return text + ".exe" if sys.platform == "win32" else text


def test_kiro_render_writes_servers_and_hooks(fake_home, tmp_path):
    venv_bin = tmp_path / "venv" / "Scripts"
    get_adapter("kiro").render(venv_bin=venv_bin)
    data = _read(fake_home / ".kiro" / "agents" / "firekeep.json")

    assert data["mcpServers"]["firekeep-relay"] == {
        "command": _exe(venv_bin / "firekeep-shim"), "args": ["--service", "relay"]}
    # symdex is always-on: present unconditionally with its stdio-local command, no args
    assert data["mcpServers"]["firekeep-symdex"] == {
        "command": _exe(venv_bin / "firekeep-symdex"), "args": []}

    assert data["hooks"]["agentSpawn"][0]["command"].endswith(
        "-m firekeep_client.hooks session_start")
    assert data["hooks"]["userPromptSubmit"][0]["command"].endswith(
        "-m firekeep_client.hooks prompt")
    assert data["hooks"]["stop"][0]["command"].endswith("-m firekeep_client.hooks stop")
    # Validated against kiro-cli 2.12.1 (docs/KIRO-VALIDATION.md): matchers are EXACT kiro
    # tool names, not regex — `fs_write` is kiro's file create/edit tool (Claude's Edit|Write
    # matched nothing, so the hook never fired). pre_tool carries --block-exit 2 (kiro's
    # documented block-on-exit-2 contract), so its command is not a bare `pre_tool`.
    pre = data["hooks"]["preToolUse"][0]
    assert pre["matcher"] == "fs_write"
    assert pre["command"].endswith("-m firekeep_client.hooks pre_tool --block-exit 2")
    post = data["hooks"]["postToolUse"][0]
    assert post["matcher"] == "fs_write"
    assert post["command"].endswith("-m firekeep_client.hooks post_tool")


_PINNED_CFG = """
[active]
profile = personal
[personal]
agent_id = tester
[office]
agent_id = tester
[pins]
kiro = office
"""


def _write_cfg(tmp_path, monkeypatch, text):
    cfg = tmp_path / "config"
    cfg.write_text(text, encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    return cfg


def test_pinned_kiro_renders_env_and_hook_profile(tmp_path, monkeypatch, fake_home):
    _write_cfg(tmp_path, monkeypatch, _PINNED_CFG)
    get_adapter("kiro").render(venv_bin=tmp_path / "vbin")

    data = _read(fake_home / ".kiro" / "agents" / "firekeep.json")
    for name in ("firekeep-cortex", "firekeep-symdex", "firekeep-decision"):
        assert data["mcpServers"][name]["env"] == {"FIREKEEP_PROFILE": "office"}
    for hooks in data["hooks"].values():
        for h in hooks:
            if "firekeep_client.hooks" in h["command"]:
                assert "--profile office" in h["command"]


def test_unpinned_kiro_render_has_no_env_or_profile(tmp_path, monkeypatch, fake_home):
    _write_cfg(tmp_path, monkeypatch, _PINNED_CFG.replace("[pins]\nkiro = office\n", ""))
    get_adapter("kiro").render(venv_bin=tmp_path / "vbin")

    data = _read(fake_home / ".kiro" / "agents" / "firekeep.json")
    assert "env" not in data["mcpServers"]["firekeep-cortex"]
    text = json.dumps(data)
    assert "--profile" not in text


def test_kiro_non_clobbering(fake_home, tmp_path):
    agents = fake_home / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "firekeep.json").write_text(json.dumps({
        "mcpServers": {"custom": {"command": "x"}},
        "hooks": {"agentSpawn": [{"command": "echo foreign"}]},
    }))
    venv_bin = tmp_path / "venv" / "Scripts"
    adapter = get_adapter("kiro")

    adapter.render(venv_bin=venv_bin)
    data = _read(agents / "firekeep.json")
    assert data["mcpServers"]["custom"] == {"command": "x"}       # foreign survived
    assert "firekeep-cortex" in data["mcpServers"]
    spawn_cmds = [h["command"] for h in data["hooks"]["agentSpawn"]]
    assert "echo foreign" in spawn_cmds                           # foreign hook survived
    assert any("firekeep_client.hooks session_start" in c for c in spawn_cmds)

    adapter.unrender()
    data2 = _read(agents / "firekeep.json")
    assert data2["mcpServers"] == {"custom": {"command": "x"}}    # only foreign left
    assert data2["hooks"]["agentSpawn"] == [{"command": "echo foreign"}]  # firekeep pruned


def _legacy_mcp_json(fake_home, payload):
    p = fake_home / ".kiro" / "settings" / "mcp.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(payload, encoding="utf-8")
    return p


def test_render_drops_legacy_firekeep_entries_from_settings_mcp_json(fake_home, tmp_path):
    p = _legacy_mcp_json(fake_home, json.dumps({"mcpServers": {
        "firekeep-cortex": {"type": "http", "url": "http://old:8080/mcp"},
        "firekeep-cortex_DISABLED": {"type": "http", "url": "https://old.office/mcp",
                                   "headers": {"X-Vault-User-Key": "SECRET"}},
        "foreign-server": {"command": "keep-me"},
    }}, indent=2))
    get_adapter("kiro").render(venv_bin=tmp_path / "vbin")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "firekeep-cortex" not in data["mcpServers"]
    assert "firekeep-cortex_DISABLED" not in data["mcpServers"]
    assert data["mcpServers"]["foreign-server"] == {"command": "keep-me"}
    assert "SECRET" not in p.read_text(encoding="utf-8")


def test_render_tolerates_missing_and_malformed_legacy_mcp_json(fake_home, tmp_path):
    # missing: no settings/mcp.json at all -> render succeeds
    get_adapter("kiro").render(venv_bin=tmp_path / "vbin")
    # malformed: left byte-identical, render still succeeds
    p = _legacy_mcp_json(fake_home, "{not json")
    get_adapter("kiro").render(venv_bin=tmp_path / "vbin")
    assert p.read_text(encoding="utf-8") == "{not json"


def test_render_survives_non_object_top_level_mcp_json(fake_home, tmp_path):
    # The malformed-mcp.json step must skip (file byte-identical) WITHOUT aborting
    # the rest of render(). This previously also asserted that the legacy-archival
    # step still ran in the same call; that step is gone — it archived the kit's own
    # agent file, discarding the user's live config (see
    # test_render_does_not_archive_its_own_agent_file). What survives, and is what
    # actually mattered, is that a legacy artifact cannot fail an install: render()
    # completes and still writes its own output.
    p = _legacy_mcp_json(fake_home, "[]")
    get_adapter("kiro").render(venv_bin=tmp_path / "vbin")
    assert p.read_text(encoding="utf-8") == "[]"
    agent = fake_home / ".kiro" / "agents" / "firekeep.json"
    assert agent.exists(), "render() aborted before writing its own agent file"
    assert "firekeep-cortex" in _read(agent)["mcpServers"]

def test_render_survives_non_dict_mcpservers_value(fake_home, tmp_path):
    # Object top level but mcpServers itself is a list: iterating yields the legacy name,
    # and `del servers["firekeep-cortex"]` on a list raises TypeError. Must skip the edit
    # (file byte-identical) instead of escaping into render()'s caller.
    payload = json.dumps({"mcpServers": ["firekeep-cortex"]})
    p = _legacy_mcp_json(fake_home, payload)
    get_adapter("kiro").render(venv_bin=tmp_path / "vbin")
    assert p.read_text(encoding="utf-8") == payload


def test_render_survives_pathologically_deep_mcp_json(fake_home, tmp_path):
    # The malformed-mcp.json step must skip (file byte-identical) WITHOUT aborting
    # the rest of render(). This previously also asserted that the legacy-archival
    # step still ran in the same call; that step is gone — it archived the kit's own
    # agent file, discarding the user's live config (see
    # test_render_does_not_archive_its_own_agent_file). What survives, and is what
    # actually mattered, is that a legacy artifact cannot fail an install: render()
    # completes and still writes its own output.
    payload = "[" * 100_000 + "]" * 100_000
    p = _legacy_mcp_json(fake_home, payload)
    get_adapter("kiro").render(venv_bin=tmp_path / "vbin")
    assert p.read_text(encoding="utf-8") == payload
    agent = fake_home / ".kiro" / "agents" / "firekeep.json"
    assert agent.exists(), "render() aborted before writing its own agent file"
    assert "firekeep-cortex" in _read(agent)["mcpServers"]

def test_render_does_not_archive_its_own_agent_file(fake_home, tmp_path):
    """render() must never move ~/.kiro/agents/firekeep.json aside.

    This test asserted the OPPOSITE — that the file was archived to .bak — and
    it passed, because the adapter really did that. It was wrong, and it
    contradicted test_kiro_non_clobbering above, which asserts foreign entries in
    that same file survive a render. Both cannot hold.

    The cause was the rename. In the predecessor the kit's agent file and the
    pre-kit artifact it archived were two distinct names under `agents/` — one
    the short product name, one the longer full one. Mapping both onto
    `firekeep` collapsed them, so the adapter archived its own output and every
    `firekeep install --runtime kiro` silently discarded the user's live kiro
    config into a .bak nobody reads.
    """
    agent = fake_home / ".kiro" / "agents" / "firekeep.json"
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_text(json.dumps({"mcpServers": {"mine": {"command": "keepme"}}}), encoding="utf-8")

    get_adapter("kiro").render(venv_bin=tmp_path / "vbin")

    assert agent.exists(), "render archived its own agent file"
    assert not (agent.parent / "firekeep.json.bak").exists(), "a .bak was created"
    data = _read(agent)
    assert data["mcpServers"]["mine"] == {"command": "keepme"}, "user's own entry lost"
    assert "firekeep-cortex" in data["mcpServers"], "kit entries not merged in"

    # idempotent: a second render must not raise and must still preserve it
    get_adapter("kiro").render(venv_bin=tmp_path / "vbin")
    assert _read(agent)["mcpServers"]["mine"] == {"command": "keepme"}


def test_render_migrates_predecessor_default_agent(fake_home, tmp_path):
    """A predecessor install left plain kiro sessions on the `nexus` agent even after
    Firekeep rendered successfully. Migrate that owned value and preserve other settings."""
    cli_json = fake_home / ".kiro" / "settings" / "cli.json"
    cli_json.parent.mkdir(parents=True, exist_ok=True)
    cli_json.write_text(json.dumps(
        {"chat.defaultAgent": "nexus", "app.beta": True}), encoding="utf-8")
    get_adapter("kiro").render(venv_bin=tmp_path / "vbin")
    data = _read(cli_json)
    assert data["chat.defaultAgent"] == "firekeep"
    assert data["app.beta"] is True


def test_render_leaves_a_foreign_default_agent_alone(fake_home, tmp_path):
    cli_json = fake_home / ".kiro" / "settings" / "cli.json"
    cli_json.parent.mkdir(parents=True, exist_ok=True)
    cli_json.write_text(json.dumps(
        {"chat.defaultAgent": "automation_portal"}), encoding="utf-8")
    get_adapter("kiro").render(venv_bin=tmp_path / "vbin")
    assert _read(cli_json)["chat.defaultAgent"] == "automation_portal"
    # and a missing cli.json is a silent no-op (render already ran fine above without one
    # in the other legacy tests; assert explicitly for the file-absent case)
    cli_json.unlink()
    get_adapter("kiro").render(venv_bin=tmp_path / "vbin")
    assert not cli_json.exists()


class TestToolsGrant:
    """kiro only exposes MCP tools the agent's `tools` list grants — without it
    the servers connect but the model can call NOTHING (field bug 2026-07-14)."""

    def test_render_grants_tools_and_pretrusts_kit_servers(self, fake_home, tmp_path):
        from firekeep_client.adapters.base import FIREKEEP_MCP_KEYS
        get_adapter("kiro").render(venv_bin=tmp_path / "venv" / "Scripts")
        data = json.loads((fake_home / ".kiro" / "agents" / "firekeep.json").read_text())
        assert data["tools"] == ["*"]  # fresh file: expose everything, like pre-kit
        for key in FIREKEEP_MCP_KEYS:
            assert f"@{key}" in data["allowedTools"]

    def test_render_unions_into_a_user_curated_tools_list(self, fake_home, tmp_path):
        from firekeep_client.adapters.base import FIREKEEP_MCP_KEYS
        path = fake_home / ".kiro" / "agents" / "firekeep.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"name": "firekeep",
                                    "tools": ["fs_read", "@my-server"],
                                    "allowedTools": ["fs_read"]}))
        get_adapter("kiro").render(venv_bin=tmp_path / "venv" / "Scripts")
        data = json.loads(path.read_text())
        assert "fs_read" in data["tools"] and "@my-server" in data["tools"]
        for key in FIREKEEP_MCP_KEYS:
            assert f"@{key}" in data["tools"]
        assert "fs_read" in data["allowedTools"]

    def test_unrender_removes_only_kit_grants(self, fake_home, tmp_path):
        path = fake_home / ".kiro" / "agents" / "firekeep.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"name": "firekeep",
                                    "tools": ["fs_read", "@my-server"],
                                    "allowedTools": ["fs_read"]}))
        adapter = get_adapter("kiro")
        adapter.render(venv_bin=tmp_path / "venv" / "Scripts")
        adapter.unrender()
        data = json.loads(path.read_text())
        assert data["tools"] == ["fs_read", "@my-server"]
        assert data["allowedTools"] == ["fs_read"]


class TestDefaultAgent:
    """Plain `kiro-cli chat` must find the firekeep agent — the kit wires a NAMED
    agent, and a fresh machine has no default pointing at it (field report:
    teammate install, /mcp empty)."""

    def _patch(self, monkeypatch, *, which, settings_out, calls):
        from firekeep_client.adapters import kiro as kiro_mod
        monkeypatch.setattr(kiro_mod.shutil, "which", lambda _: which)

        class _R:
            returncode = 0
            stdout = settings_out
            stderr = ""

        def fake_run(cmd, **k):
            calls.append(cmd)
            return _R()

        monkeypatch.setattr(kiro_mod.subprocess, "run", fake_run)

    def test_sets_default_when_none_configured(self, fake_home, tmp_path, monkeypatch):
        calls = []
        self._patch(monkeypatch, which="/usr/local/bin/kiro-cli",
                    settings_out="chat.defaultModel = \"x\" (global)\n", calls=calls)
        get_adapter("kiro").render(venv_bin=tmp_path / "venv" / "Scripts")
        assert ["kiro-cli", "agent", "set-default", "firekeep"] in calls

    def test_never_overrides_an_existing_default(self, fake_home, tmp_path, monkeypatch):
        calls = []
        self._patch(monkeypatch, which="/usr/local/bin/kiro-cli",
                    settings_out='chat.defaultAgent = "my-own" (global)\n', calls=calls)
        get_adapter("kiro").render(venv_bin=tmp_path / "venv" / "Scripts")
        assert ["kiro-cli", "agent", "set-default", "firekeep"] not in calls

    def test_silent_when_kiro_cli_absent(self, fake_home, tmp_path, monkeypatch):
        calls = []
        self._patch(monkeypatch, which=None, settings_out="", calls=calls)
        get_adapter("kiro").render(venv_bin=tmp_path / "venv" / "Scripts")
        assert calls == []  # no subprocess at all
