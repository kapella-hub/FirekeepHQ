import json
import sys

import pytest

from firekeep_client.adapters import get_adapter
from firekeep_client.adapters.pi import PACKAGE


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    # The adapter honors PI_CODING_AGENT_CONFIG_DIR; a developer's real value must
    # never leak a test render outside tmp_path (the XDG precedent in test_opencode).
    monkeypatch.delenv("PI_CODING_AGENT_CONFIG_DIR", raising=False)
    return tmp_path


def _read(p):
    return json.loads(p.read_text(encoding="utf-8"))


def _exe(path):
    """Expected console-script path for the CURRENT (real, unmocked) host platform —
    mirrors console_script_path's win32 `.exe` handling (test_claude.py's helper)."""
    text = str(path)
    return text + ".exe" if sys.platform == "win32" else text


def _settings(fake_home):
    return fake_home / ".pi" / "agent" / "settings.json"


def _agents_md(fake_home):
    return fake_home / ".pi" / "agent" / "AGENTS.md"


def _sidecar(tmp_path):
    """The sidecar sits beside ~/.firekeep/config, which conftest's autouse
    `_isolate_firekeep_home` re-points at tmp_path/_isolated so no test can write
    to the developer's real home. Resolve it the same way the adapter does
    rather than assuming a path."""
    return tmp_path / "_isolated" / "pi-extension.json"


def test_render_adds_the_package_entry(fake_home, tmp_path):
    get_adapter("pi").render(venv_bin=tmp_path / "venv" / "Scripts")
    assert _read(_settings(fake_home))["packages"] == [PACKAGE]


def test_render_writes_sidecar_naming_the_venv_interpreter(fake_home, tmp_path):
    venv_bin = tmp_path / "venv" / "Scripts"
    get_adapter("pi").render(venv_bin=venv_bin)

    data = _read(_sidecar(tmp_path))
    # Forward slashes: the extension passes this straight to spawnSync as argv[0].
    assert data["python"] == _exe(venv_bin / "python").replace("\\", "/")
    assert data["runtime"] == "pi"


def test_render_preserves_foreign_packages_and_keys(fake_home, tmp_path):
    _settings(fake_home).parent.mkdir(parents=True, exist_ok=True)
    _settings(fake_home).write_text(
        json.dumps({"packages": ["pi-skills"], "defaultProjectTrust": "ask"}),
        encoding="utf-8",
    )

    get_adapter("pi").render(venv_bin=tmp_path / "venv" / "Scripts")

    data = _read(_settings(fake_home))
    assert data["packages"] == ["pi-skills", PACKAGE]
    assert data["defaultProjectTrust"] == "ask"


def test_render_is_idempotent(fake_home, tmp_path):
    adapter = get_adapter("pi")
    adapter.render(venv_bin=tmp_path / "venv" / "Scripts")
    adapter.render(venv_bin=tmp_path / "venv" / "Scripts")

    # A re-render must not append a second copy.
    assert _read(_settings(fake_home))["packages"].count(PACKAGE) == 1


def test_render_upserts_only_the_marked_block_in_agents_md(fake_home, tmp_path):
    _agents_md(fake_home).parent.mkdir(parents=True, exist_ok=True)
    _agents_md(fake_home).write_text("# My rules\n\nDo the thing.\n", encoding="utf-8")

    get_adapter("pi").render(venv_bin=tmp_path / "venv" / "Scripts")

    text = _agents_md(fake_home).read_text(encoding="utf-8")
    assert "Do the thing." in text  # user content is never clobbered
    assert "firekeep:instructions:begin" in text


def test_unrender_removes_only_firekeep_entries(fake_home, tmp_path):
    _settings(fake_home).parent.mkdir(parents=True, exist_ok=True)
    _settings(fake_home).write_text(
        json.dumps({"packages": ["pi-skills"], "defaultProjectTrust": "ask"}),
        encoding="utf-8",
    )
    _agents_md(fake_home).write_text("# My rules\n\nDo the thing.\n", encoding="utf-8")

    adapter = get_adapter("pi")
    adapter.render(venv_bin=tmp_path / "venv" / "Scripts")
    adapter.unrender()

    data = _read(_settings(fake_home))
    assert data["packages"] == ["pi-skills"]
    assert data["defaultProjectTrust"] == "ask"
    assert not _sidecar(tmp_path).exists()

    text = _agents_md(fake_home).read_text(encoding="utf-8")
    assert "Do the thing." in text
    assert "firekeep:instructions:begin" not in text


def test_unrender_is_safe_with_no_prior_render(fake_home, tmp_path):
    # A user who never installed the pi runtime must not get a crash or a file.
    get_adapter("pi").unrender()
    assert not _sidecar(tmp_path).exists()


def test_renders_no_mcp_key(fake_home, tmp_path):
    """Pi ships no MCP client, so there is no server surface to configure.

    This is a capability BOUNDARY, not an omission: writing an `mcp` block here
    would imply a tool surface that does not exist, which is precisely the false
    claim contract/matrix.py warns about. If Pi ever gains built-in MCP, this
    test is the thing that should fail and force the matrix to be revisited.
    """
    get_adapter("pi").render(venv_bin=tmp_path / "venv" / "Scripts")
    assert "mcp" not in _read(_settings(fake_home))


def test_config_dir_override_is_honored(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_CONFIG_DIR", str(tmp_path / "elsewhere"))
    get_adapter("pi").render(venv_bin=tmp_path / "venv" / "Scripts")
    assert (tmp_path / "elsewhere" / "settings.json").exists()


def test_app_present_is_false_without_a_pi_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_CONFIG_DIR", str(tmp_path / "never-run"))
    from firekeep_client.adapters.pi import app_present

    assert app_present() is False


def test_app_present_is_true_once_pi_has_run(tmp_path, monkeypatch):
    agent_dir = tmp_path / "pi-agent"
    agent_dir.mkdir(parents=True)
    monkeypatch.setenv("PI_CODING_AGENT_CONFIG_DIR", str(agent_dir))
    from firekeep_client.adapters.pi import app_present

    assert app_present() is True


def test_pi_joins_the_all_fan_out_only_when_present():
    """The gate that keeps `firekeep install` off machines without Pi.

    Pinned because the failure mode is silent and machine-specific: with the gate
    inverted, a developer who has Pi installed gets a fifth runtime in "all" and
    the count invariants in test_cli_install fail only on their machine.
    """
    from firekeep_client.cli import _selected_runtimes

    assert "pi" not in _selected_runtimes("all")
    assert "pi" in _selected_runtimes("all", include_pi=True)
    # An explicit --runtime pi always renders, gate or no gate.
    assert _selected_runtimes("pi") == ["pi"]
