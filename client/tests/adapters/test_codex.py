import sys
import tomllib

import pytest

from firekeep_client.adapters import get_adapter
from firekeep_client.adapters.base import strip_block, upsert_block


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _exe(path):
    """Expected console-script path for the CURRENT (real, unmocked) host platform —
    mirrors the win32 `.exe` handling in firekeep_client.adapters.base.console_script_path."""
    text = str(path)
    return text + ".exe" if sys.platform == "win32" else text


def test_codex_render_writes_mcp_servers(fake_home, tmp_path):
    venv_bin = tmp_path / "venv" / "Scripts"
    get_adapter("codex").render(venv_bin=venv_bin)
    text = (fake_home / ".codex" / "config.toml").read_text()
    assert "[mcp_servers.firekeep-cortex]" in text
    # TOML literal string, Windows-safe; _exe() accounts for the win32 console-script
    # `.exe` suffix that console_script_path (used inside shim_servers) appends.
    assert f"command = '{_exe(venv_bin / 'firekeep-shim')}'" in text
    assert 'args = ["--service", "cortex"]' in text
    assert "[mcp_servers.firekeep-relay]" in text
    # symdex is always-on: its stdio-local block is present unconditionally
    assert "[mcp_servers.firekeep-symdex]" in text
    assert f"command = '{_exe(venv_bin / 'firekeep-symdex')}'" in text


_PINNED_CFG = """
[active]
profile = personal
[personal]
agent_id = tester
[office]
agent_id = tester
[pins]
codex = office
"""


def _write_cfg(tmp_path, monkeypatch, text):
    cfg = tmp_path / "config"
    cfg.write_text(text, encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    return cfg


def test_pinned_codex_renders_env_inline_table(tmp_path, monkeypatch, fake_home):
    _write_cfg(tmp_path, monkeypatch, _PINNED_CFG)
    get_adapter("codex").render(venv_bin=tmp_path / "vbin")

    text = (fake_home / ".codex" / "config.toml").read_text()
    parsed = tomllib.loads(text)
    for name in ("firekeep-cortex", "firekeep-symdex", "firekeep-decision"):
        assert parsed["mcp_servers"][name]["env"] == {"FIREKEEP_PROFILE": "office"}
    assert 'env = { FIREKEEP_PROFILE = "office" }' in text


def test_unpinned_codex_render_has_no_env(tmp_path, monkeypatch, fake_home):
    _write_cfg(tmp_path, monkeypatch, _PINNED_CFG.replace("[pins]\ncodex = office\n", ""))
    get_adapter("codex").render(venv_bin=tmp_path / "vbin")

    text = (fake_home / ".codex" / "config.toml").read_text()
    assert "FIREKEEP_PROFILE" not in text


def test_codex_non_clobbering(fake_home, tmp_path):
    cfgdir = fake_home / ".codex"
    cfgdir.mkdir()
    original = (
        "# user notes: do not touch\n"
        "[mcp_servers.custom]\ncommand = 'foo'\nargs = []\n"
    )
    (cfgdir / "config.toml").write_text(original)
    venv_bin = tmp_path / "venv" / "Scripts"
    adapter = get_adapter("codex")

    adapter.render(venv_bin=venv_bin)
    text = (cfgdir / "config.toml").read_text()
    assert "[mcp_servers.custom]" in text          # foreign survived render
    assert "# user notes: do not touch" in text    # arbitrary foreign text survived
    assert "[mcp_servers.firekeep-cortex]" in text    # firekeep added

    adapter.render(venv_bin=venv_bin)  # idempotent re-render
    text2 = (cfgdir / "config.toml").read_text()
    assert text2.count("[mcp_servers.firekeep-cortex]") == 1

    adapter.unrender()
    text3 = (cfgdir / "config.toml").read_text()
    assert "[mcp_servers.custom]" in text3         # foreign survived unrender
    assert "# user notes: do not touch" in text3   # arbitrary foreign text survived unrender
    assert "firekeep-cortex" not in text3             # firekeep removed
    # Foreign content round-trips byte-for-byte modulo the known trailing-newline nit.
    assert text3.rstrip("\n") == original.rstrip("\n")


def test_codex_render_produces_parseable_toml(fake_home, tmp_path):
    """Carry-forward from T20/T21 review: TOML basic (double-quoted) strings escape
    backslashes, so a raw Windows path in a basic string would be INVALID TOML. The
    rendered `command` value must use a TOML literal (single-quoted) string instead --
    pin this by round-tripping the rendered file through stdlib tomllib."""
    venv_bin = tmp_path / "venv" / "Scripts"
    get_adapter("codex").render(venv_bin=venv_bin)
    text = (fake_home / ".codex" / "config.toml").read_text()
    parsed = tomllib.loads(text)

    cortex = parsed["mcp_servers"]["firekeep-cortex"]
    assert cortex["command"] == _exe(venv_bin / "firekeep-shim")
    assert cortex["args"] == ["--service", "cortex"]

    relay = parsed["mcp_servers"]["firekeep-relay"]
    assert relay["args"] == ["--service", "relay"]

    symdex = parsed["mcp_servers"]["firekeep-symdex"]
    assert symdex["command"] == _exe(venv_bin / "firekeep-symdex")
    assert symdex["args"] == []


def test_upsert_and_strip_block_treat_backslashes_literally():
    """Carry-forward from T20/T21 review: a naive `pattern.sub(text, ...)` interprets
    backslashes in the REPLACEMENT argument as regex group references (`\\1`, `\\g<0>`,
    or raises re.error on unknown escapes) -- corrupting Windows paths. upsert_block must
    use the function-replacement form (`pattern.sub(lambda _m: wrapped, text)`) so
    backslash-laden content survives literally. Hardcoded string, not Path-derived, so
    this regression is pinned on every host OS, not just Windows."""
    content = r"command = 'C:\Users\test\.firekeep\venv\Scripts\firekeep-shim.exe'"
    start, end = "# >>> marker >>>", "# <<< marker <<<"

    text = upsert_block("", content, start, end)
    assert content in text

    # Re-upsert with the SAME backslash-laden content to exercise the pattern.search
    # (replace-in-place) branch, not just the append branch.
    text2 = upsert_block(text, content, start, end)
    assert text2.count(content) == 1

    stripped = strip_block(text2, start, end)
    assert content not in stripped
    assert "firekeep-shim" not in stripped
