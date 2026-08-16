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
    # The answer sequence gained one step: a FRESH machine is now asked WHERE its
    # server is before being asked to describe it. "3" is "it is already running",
    # the only branch for which host and key are answerable questions -- which is
    # the whole point of the change, since this test's own old sequence
    # (identity, host, key) was one a first-time user could not supply.
    cfg = _cfg(SKELETON)
    plan = wizard.prompt_config(
        cfg, ask=_scripted(["Alex", "3", "203.0.113.10", "sk-local"])
    )

    assert plan.action == wizard.EXISTING_SERVER
    assert cfg["identity"]["agent_id"] == "Alex"
    assert cfg["server"]["host"] == "203.0.113.10"
    assert cfg["server"]["api_key"] == "sk-local"
    assert not cfg.has_section("active")


def test_blank_ports_key_stays_absent():
    cfg = _cfg(SKELETON + "\n[dist]\nbase_url = https://releases.example\n")
    wizard.prompt_config(cfg, ask=_scripted(["Alex", "3", "127.0.0.1", ""]))
    assert "api_key" not in cfg["server"]
    assert cfg["dist"]["base_url"] == "https://releases.example"


def test_blank_paths_key_stays_absent():
    cfg = _cfg(PATHS)
    wizard.prompt_config(
        cfg,
        ask=_scripted([]),
        probe=lambda *_: False,
        fetch_defaults=lambda _cfg: {},
    )
    assert "api_key" not in cfg["server"]


@pytest.mark.parametrize(
    "config_text",
    [
        SKELETON + "\napi_key = nxs_existing_secret\n",
        PATHS.replace("api_key =", "api_key = nxs_existing_secret"),
    ],
)
def test_existing_api_key_is_not_a_prompt_default_and_blank_keeps_it(config_text):
    cfg = _cfg(config_text)
    ask = _scripted([])

    wizard.prompt_config(
        cfg,
        ask=ask,
        probe=lambda *_: False,
        fetch_defaults=lambda _cfg: {},
    )

    api_prompt = next(item for item in ask.seen if item[0].startswith("API key"))
    assert api_prompt[1] == ""
    assert all("nxs_existing_secret" not in part for item in ask.seen for part in item)
    assert cfg["server"]["api_key"] == "nxs_existing_secret"


def test_typed_api_key_replaces_existing_without_exposing_it_as_default():
    cfg = _cfg(SKELETON + "\napi_key = nxs_existing_secret\n")

    def ask(prompt, default=""):
        if prompt.startswith("API key"):
            assert default == ""
            return "nxs_replacement"
        return default

    wizard.prompt_config(cfg, ask=ask)
    assert cfg["server"]["api_key"] == "nxs_replacement"


def test_console_prompt_never_contains_existing_api_key(monkeypatch):
    cfg = _cfg(
        SKELETON.replace("CHANGEME", "Alex")
        + "\napi_key = nxs_existing_secret\n"
    )
    rendered = []

    def answer_blank(prompt):
        rendered.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", answer_blank)
    wizard.prompt_config(cfg)

    assert all("nxs_existing_secret" not in prompt for prompt in rendered)
    assert any("Enter keeps existing" in prompt for prompt in rendered)
    assert cfg["server"]["api_key"] == "nxs_existing_secret"


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
    # PATHS already carries a base_url, so this machine IS connected: it goes
    # straight to the edit-in-place prompts rather than being asked where its
    # server is. The routing menu exists for the state that had no way out --
    # a client with nothing to talk to -- not for reconfiguring a working one.
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
    # base_url blanked, so this is an UNCONNECTED paths config and the menu runs;
    # "3" ("it is already running") is the branch that reaches the org prefill.
    cfg = _cfg(PATHS.replace("https://firekeep.example", ""))
    wizard.prompt_config(
        cfg,
        ask=_scripted(["Alex", "3", "", "", "sk"]),
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


# --- the routing question ----------------------------------------------------
#
# The prompts this replaced -- "Server host [127.0.0.1]" and "API key" -- were
# asked at the one moment neither could be answered, and pressing Enter through
# them produced a valid config pointing at nothing. These tests are about that:
# a fresh machine must never end up silently pointed at a server that is not
# there.


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("1", wizard.PROVISION_HERE),
        ("", wizard.PROVISION_HERE),          # bare Enter, docker present
        ("here", wizard.PROVISION_HERE),
        ("4", wizard.DECIDE_LATER),
        ("later", wizard.DECIDE_LATER),
        ("n", wizard.DECIDE_LATER),
    ],
)
def test_routing_answers_map_to_actions(answer, expected):
    cfg = _cfg(SKELETON)
    plan = wizard.prompt_config(cfg, ask=_scripted(["Alex", answer]), docker=True)
    assert plan.action == expected


def test_join_code_is_captured_at_the_prompt():
    cfg = _cfg(SKELETON)
    plan = wizard.prompt_config(
        cfg, ask=_scripted(["Alex", "2", "fk_join_abc.def"]), docker=True
    )
    assert plan.action == wizard.JOIN_WITH_CODE
    assert plan.join_code == "fk_join_abc.def"


def test_choosing_join_but_pasting_nothing_falls_back_to_later():
    """Not an error, and NOT a silent success. Someone who picks "I have a code"
    and then finds they do not is in the same position as someone who picked
    "not yet", and must get the same routing on the way out."""
    cfg = _cfg(SKELETON)
    plan = wizard.prompt_config(cfg, ask=_scripted(["Alex", "2", ""]), docker=True)
    assert plan.action == wizard.DECIDE_LATER


def test_default_is_join_when_docker_is_absent():
    """A laptop with no Docker cannot host a server, so offering to install one
    as the default would be an unrunnable suggestion -- which is the exact defect
    class being fixed, not a new one to introduce."""
    cfg = _cfg(SKELETON)
    ask = _scripted(["Alex", "", "fk_join_x.y"])
    plan = wizard.prompt_config(cfg, ask=ask, docker=False)
    assert plan.action == wizard.JOIN_WITH_CODE
    assert next(default for prompt, default in ask.seen if prompt == "Choose") == "2"


def test_deferring_marks_the_config_unconfigured():
    """The sentinel is what lets doctor tell "never set up" apart from
    "deliberate localhost". Without it, host=127.0.0.1 is all doctor can see."""
    cfg = _cfg(SKELETON)
    wizard.prompt_config(cfg, ask=_scripted(["Alex", "4"]), docker=True)
    assert cfg["server"][wizard.UNCONFIGURED_MARKER] == "false"


@pytest.mark.parametrize("answer", ["1", "2", "3"])
def test_every_answer_that_leads_to_a_connection_clears_the_sentinel(answer):
    cfg = _cfg(SKELETON)
    wizard.prompt_config(
        cfg,
        ask=_scripted(["Alex", answer, "fk_join_a.b", "sk"]),
        docker=True,
    )
    assert wizard.UNCONFIGURED_MARKER not in cfg["server"]


def test_a_fresh_machine_is_never_asked_for_a_key_it_cannot_have():
    """The regression guard for the original report. Accepting every default on a
    bare machine must not surface either of the two unanswerable prompts."""
    cfg = _cfg(SKELETON)
    ask = _scripted([])
    wizard.prompt_config(cfg, ask=ask, docker=True)
    asked = [prompt for prompt, _ in ask.seen]
    assert not any(p.startswith("API key") for p in asked), asked
    assert not any(p.startswith("Server host") for p in asked), asked


def test_a_connected_machine_is_not_asked_where_its_server_is():
    """`firekeep install --runtime claude` is a documented re-render, not an
    invitation to repoint the machine."""
    cfg = _cfg(SKELETON.replace("127.0.0.1", "10.0.0.4"))
    ask = _scripted([])
    wizard.prompt_config(cfg, ask=ask, docker=True)
    assert not any(prompt == "Choose" for prompt, _ in ask.seen)
    assert cfg["server"]["host"] == "10.0.0.4"
