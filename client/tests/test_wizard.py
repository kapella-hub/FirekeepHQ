import configparser

import pytest

from firekeep_client import wizard


def _cfg(text=""):
    cfg = configparser.ConfigParser(interpolation=None)
    if text:
        cfg.read_string(text)
    return cfg


def _scripted(answers):
    queue = list(answers)
    seen = []

    def ask(prompt, default=""):
        seen.append((prompt, default))
        answer = queue.pop(0) if queue else ""
        return answer or default

    ask.seen = seen
    return ask


SKELETON = """\
[identity]
agent_id = CHANGEME

[server]
kind = ports
scheme = http
host = 127.0.0.1
verify_tls = false
"""


PATHS = """\
[identity]
agent_id = Alex

[server]
kind = paths
scheme = https
base_url = https://firekeep.example
verify_tls = true
ca_path = ~/.firekeep/firekeep-root-ca.crt
api_key =
"""


def test_fresh_install_sets_identity_host_and_key():
    cfg = _cfg(SKELETON)
    wizard.prompt_config(cfg, ask=_scripted(["Alex", "203.0.113.10", "sk-local"]))

    assert cfg["identity"]["agent_id"] == "Alex"
    assert cfg["server"]["host"] == "203.0.113.10"
    assert cfg["server"]["api_key"] == "sk-local"
    assert not cfg.has_section("active")


def test_blank_ports_key_stays_absent():
    cfg = _cfg(SKELETON + "\n[dist]\nbase_url = https://releases.example\n")
    wizard.prompt_config(cfg, ask=_scripted(["Alex", "127.0.0.1", ""]))
    assert "api_key" not in cfg["server"]
    assert cfg["dist"]["base_url"] == "https://releases.example"


def test_enter_through_everything_keeps_current_values():
    cfg = _cfg(SKELETON.replace("CHANGEME", "Alex").replace("127.0.0.1", "10.0.0.4"))
    wizard.prompt_config(cfg, ask=_scripted([]))
    assert cfg["identity"]["agent_id"] == "Alex"
    assert cfg["server"]["host"] == "10.0.0.4"


def test_agent_id_default_is_never_placeholder(monkeypatch):
    monkeypatch.setattr(wizard.getpass, "getuser", lambda: "moganes")
    cfg = _cfg(SKELETON)
    ask = _scripted([])
    wizard.prompt_config(cfg, ask=ask)
    assert ask.seen[0][1] == "moganes"
    assert cfg["identity"]["agent_id"] == "moganes"


def test_flags_seed_defaults_and_host_forces_ports():
    cfg = _cfg(PATHS)
    wizard.prompt_config(cfg, ask=_scripted([]), agent_id="ci-bot", host="10.0.0.9")
    assert cfg["identity"]["agent_id"] == "ci-bot"
    assert cfg["server"]["kind"] == "ports"
    assert cfg["server"]["scheme"] == "http"
    assert cfg["server"]["host"] == "10.0.0.9"
    assert "base_url" not in cfg["server"]
    assert "ca_path" not in cfg["server"]


def test_existing_paths_connection_prompts_tls_shape():
    cfg = _cfg(PATHS)
    ask = _scripted(["Alex", "https://firekeep.corp", "~/.firekeep/ca.crt", "sk-hosted"])
    wizard.prompt_config(cfg, ask=ask, probe=lambda *_: False)
    assert cfg["server"]["base_url"] == "https://firekeep.corp"
    assert cfg["server"]["ca_path"] == "~/.firekeep/ca.crt"
    assert cfg["server"]["api_key"] == "sk-hosted"


def test_paths_ca_defaults_to_os_when_certificate_verifies():
    cfg = _cfg(PATHS)
    ask = _scripted(["Alex", "https://firekeep.corp", "", "sk-hosted"])
    wizard.prompt_config(cfg, ask=ask, probe=lambda *_: True)
    assert cfg["server"]["ca_path"] == "os"
    assert next(default for prompt, default in ask.seen if "CA cert path" in prompt) == "os"


def test_paths_probe_never_overrides_deliberate_ca_path():
    cfg = _cfg(PATHS.replace("~/.firekeep/firekeep-root-ca.crt", "~/.firekeep/mine.pem"))
    wizard.prompt_config(cfg, ask=_scripted([]), probe=lambda *_: True)
    assert cfg["server"]["ca_path"] == "~/.firekeep/mine.pem"


def test_server_defaults_accept_legacy_office_payload():
    cfg = _cfg(PATHS.replace("https://firekeep.example", ""))
    wizard.prompt_config(
        cfg,
        ask=_scripted(["Alex", "", "", "sk"]),
        probe=lambda *_: False,
        fetch_defaults=lambda _cfg: {
            "office": {"base_url": "https://firekeep.corp.example", "ca_path": "os"}
        },
    )
    assert cfg["server"]["base_url"] == "https://firekeep.corp.example"
    assert cfg["server"]["ca_path"] == "os"


@pytest.mark.parametrize("stream,expected", [
    (type("S", (), {"isatty": lambda self: True})(), True),
    (type("S", (), {"isatty": lambda self: False})(), False),
    (type("S", (), {})(), False),
    (type("S", (), {"isatty": lambda self: (_ for _ in ()).throw(ValueError)})(), False),
])
def test_is_interactive_never_raises(stream, expected):
    assert wizard.is_interactive(stream) is expected


def test_console_ask_treats_eof_as_default(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: (_ for _ in ()).throw(EOFError))
    assert wizard.console_ask("Agent identity", "Alex") == "Alex"


def test_set_dist_base_creates_and_updates_section():
    cfg = _cfg(SKELETON)
    wizard.set_dist_base(cfg, "http://gl/rel/v1/")
    wizard.set_dist_base(cfg, "http://gl/rel/v2")
    assert cfg["dist"]["base_url"] == "http://gl/rel/v2"


def test_probe_os_trust_is_false_for_non_https_and_garbage():
    assert wizard._probe_os_trust("http://plain.example") is False
    assert wizard._probe_os_trust("not a url") is False
    assert wizard._probe_os_trust("") is False
