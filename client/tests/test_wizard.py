import configparser

import pytest

from firekeep_client import wizard


def _cfg(text=""):
    cfg = configparser.ConfigParser(interpolation=None)
    if text:
        cfg.read_string(text)
    return cfg


def _scripted(answers):
    """An `ask` that replays a list of answers. A bare "" means the user hit Enter, so the
    prompt's own default is what lands — the same contract as console_ask."""
    queue = list(answers)
    seen = []

    def ask(prompt, default=""):
        seen.append((prompt, default))
        answer = queue.pop(0) if queue else ""
        return answer or default

    ask.seen = seen
    return ask


SKELETON = """\
[active]
profile = personal

[personal]
kind = ports
scheme = http
host = 127.0.0.1
verify_tls = false
agent_id = CHANGEME
"""


def test_ask_runtime_maps_numbers():
    assert wizard.ask_runtime(ask=_scripted(["1"])) == "claude"
    assert wizard.ask_runtime(ask=_scripted(["2"])) == "codex"
    assert wizard.ask_runtime(ask=_scripted(["3"])) == "kiro"
    assert wizard.ask_runtime(ask=_scripted(["4"])) == "opencode"
    assert wizard.ask_runtime(ask=_scripted(["5"])) == "all"


def test_ask_runtime_accepts_names_and_default():
    assert wizard.ask_runtime(ask=_scripted(["kiro"])) == "kiro"
    assert wizard.ask_runtime(ask=_scripted(["opencode"])) == "opencode"
    assert wizard.ask_runtime(ask=_scripted([""])) == "all"                 # Enter -> default 5 -> all
    assert wizard.ask_runtime(ask=_scripted([""]), default="claude") == "claude"


def test_ask_runtime_reprompts_on_garbage():
    assert wizard.ask_runtime(ask=_scripted(["nope", "3"])) == "kiro"       # invalid then valid


def test_fresh_install_sets_identity_and_host():
    cfg = _cfg(SKELETON)
    ask = _scripted(["Alex", "1", "203.0.113.10", ""])
    wizard.prompt_config(cfg, ask=ask, probe=lambda *_: False)

    assert cfg["personal"]["agent_id"] == "Alex"
    assert cfg["personal"]["host"] == "203.0.113.10"
    assert cfg["active"]["profile"] == "personal"
    assert "api_key" not in cfg["personal"]  # blank answer must not write an empty key


def test_enter_through_everything_keeps_current_values():
    """Re-running install must be a no-op for someone who just hits Enter. This is the
    property that makes the wizard safe to re-run after every kit upgrade."""
    cfg = _cfg(SKELETON.replace("CHANGEME", "Alex").replace("127.0.0.1", "10.0.0.4"))
    wizard.prompt_config(cfg, ask=_scripted([]), probe=lambda *_: False)  # every answer is empty -> take defaults

    assert cfg["personal"]["agent_id"] == "Alex"
    assert cfg["personal"]["host"] == "10.0.0.4"
    assert cfg["active"]["profile"] == "personal"


def test_agent_id_default_is_never_the_placeholder(monkeypatch):
    """CHANGEME is the thing we're here to eliminate — it must never be offered back as a
    default. The OS username is the fallback."""
    monkeypatch.setattr(wizard.getpass, "getuser", lambda: "moganes")
    cfg = _cfg(SKELETON)
    ask = _scripted([])
    wizard.prompt_config(cfg, ask=ask, probe=lambda *_: False)

    identity_default = ask.seen[0][1]
    assert identity_default == "moganes"
    assert cfg["personal"]["agent_id"] == "moganes"


def test_office_profile_prompts_for_tls_shape():
    cfg = _cfg(SKELETON)
    ask = _scripted(["Alex", "2", "https://firekeep.corp", "~/.firekeep/ca.crt", "sk-office"])
    wizard.prompt_config(cfg, ask=ask, probe=lambda *_: False)

    assert cfg["active"]["profile"] == "office"
    assert cfg["office"]["base_url"] == "https://firekeep.corp"
    assert cfg["office"]["ca_path"] == "~/.firekeep/ca.crt"
    assert cfg["office"]["api_key"] == "sk-office"
    assert cfg["office"]["agent_id"] == "Alex"
    assert cfg["office"]["verify_tls"] == "true"  # https without verify is refused by resolver


def test_both_profiles_write_identity_to_each_and_pick_active():
    """agent_id is per-profile in the INI but there is one human behind it: `firekeep profile
    use office` must not silently revert them to CHANGEME."""
    cfg = _cfg(SKELETON)
    ask = _scripted([
        "Alex", "3",
        "203.0.113.10", "",                              # personal host, no key
        "https://firekeep.corp", "~/.firekeep/ca.crt", "k",    # office
        "office",                                        # active
    ])
    wizard.prompt_config(cfg, ask=ask, probe=lambda *_: False)

    assert cfg["personal"]["agent_id"] == "Alex"
    assert cfg["office"]["agent_id"] == "Alex"
    assert cfg["active"]["profile"] == "office"


def test_profile_prompt_reasks_on_garbage(capsys):
    cfg = _cfg(SKELETON)
    ask = _scripted(["Alex", "banana", "1", "127.0.0.1", ""])
    wizard.prompt_config(cfg, ask=ask, probe=lambda *_: False)

    assert cfg["active"]["profile"] == "personal"
    assert "please answer 1, 2, or 3" in capsys.readouterr().out


def test_flags_seed_the_defaults():
    cfg = _cfg(SKELETON)
    ask = _scripted([])  # user hits Enter through everything
    wizard.prompt_config(cfg, ask=ask, agent_id="ci-bot", host="10.0.0.9", profile="personal")

    assert cfg["personal"]["agent_id"] == "ci-bot"
    assert cfg["personal"]["host"] == "10.0.0.9"


@pytest.mark.parametrize("stream,expected", [
    (type("S", (), {"isatty": lambda self: True})(), True),
    (type("S", (), {"isatty": lambda self: False})(), False),
    (type("S", (), {})(), False),                                    # no isatty at all
    (type("S", (), {"isatty": lambda self: (_ for _ in ()).throw(ValueError)})(), False),
])
def test_is_interactive_never_raises(stream, expected):
    """A piped/CI/closed-stdin install must fall through to non-interactive, never blow up
    and never block on input()."""
    assert wizard.is_interactive(stream) is expected


def test_console_ask_treats_eof_as_take_the_default(monkeypatch, capsys):
    def eof(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert wizard.console_ask("Agent identity", "Alex") == "Alex"


def test_set_dist_base_creates_the_section():
    cfg = _cfg(SKELETON)
    wizard.set_dist_base(cfg, "http://gl/rel/v1/")
    assert cfg["dist"]["base_url"] == "http://gl/rel/v1"   # trailing slash normalized


def test_set_dist_base_is_idempotent():
    cfg = _cfg(SKELETON)
    wizard.set_dist_base(cfg, "http://gl/rel/v1")
    wizard.set_dist_base(cfg, "http://gl/rel/v2")
    assert cfg["dist"]["base_url"] == "http://gl/rel/v2"


def test_office_ca_prompt_defaults_to_os_when_cert_verifies_against_os_trust():
    """A corporate-CA-signed server whose CA lives in the OS keychain (MDM) needs no
    CA file at all — when the TLS probe verifies, 'os' is offered as the default and
    Enter accepts it."""
    cfg = _cfg(SKELETON)
    ask = _scripted(["Alex", "2", "https://firekeep.corp", "", "sk-office"])  # Enter on ca_path
    wizard.prompt_config(cfg, ask=ask, probe=lambda *_: True)

    assert cfg["office"]["ca_path"] == "os"
    ca_prompts = [(p, d) for p, d in ask.seen if "CA cert path" in p]
    assert ca_prompts and ca_prompts[0][1] == "os"


def test_office_ca_probe_never_overrides_a_deliberate_ca_path():
    """A previously configured non-default ca_path is the user's choice — the probe
    must not steal the default even when OS trust would work."""
    cfg = _cfg(SKELETON + """
[office]
kind = paths
scheme = https
base_url = https://firekeep.corp
verify_tls = true
ca_path = ~/.firekeep/corp-ca.pem
agent_id = Alex
""")
    ask = _scripted(["Alex", "2", "", "", ""])  # Enter through everything
    wizard.prompt_config(cfg, ask=ask, probe=lambda *_: True)

    assert cfg["office"]["ca_path"] == "~/.firekeep/corp-ca.pem"


def test_office_ca_prompt_keeps_file_default_when_probe_fails():
    cfg = _cfg(SKELETON)
    ask = _scripted(["Alex", "2", "https://firekeep.corp", "", ""])
    wizard.prompt_config(cfg, ask=ask, probe=lambda *_: False)

    assert cfg["office"]["ca_path"] == "~/.firekeep/firekeep-root-ca.crt"


def test_probe_os_trust_is_false_for_non_https_and_garbage():
    """The probe may only ever improve the default — every failure shape is False."""
    assert wizard._probe_os_trust("http://plain.example") is False
    assert wizard._probe_os_trust("not a url") is False
    assert wizard._probe_os_trust("") is False


def test_office_base_url_prefilled_from_org_defaults():
    """Board 2026-07-14: a new teammate's wizard prefills the office connection
    from the org-published defaults — Enter accepts, typing overrides."""
    cfg = _cfg(SKELETON)
    ask = _scripted(["Alex", "2", "", "", "sk-office"])  # Enter on base_url
    wizard.prompt_config(
        cfg, ask=ask, probe=lambda *_: False,
        fetch_defaults=lambda c: {"office": {"base_url": "https://firekeep.corp.example",
                                             "ca_path": "os"}},
    )
    assert cfg["office"]["base_url"] == "https://firekeep.corp.example"
    assert cfg["office"]["ca_path"] == "os"


def test_org_defaults_never_override_a_configured_machine():
    cfg = _cfg(SKELETON + """
[office]
kind = paths
scheme = https
base_url = https://already.configured
verify_tls = true
ca_path = ~/.firekeep/mine.pem
agent_id = Alex
""")
    fetched = []

    def fetch(c):
        fetched.append(True)
        return {"office": {"base_url": "https://firekeep.corp.example"}}

    ask = _scripted(["Alex", "2", "", "", ""])  # Enter through everything
    wizard.prompt_config(cfg, ask=ask, probe=lambda *_: False, fetch_defaults=fetch)
    assert cfg["office"]["base_url"] == "https://already.configured"
    assert cfg["office"]["ca_path"] == "~/.firekeep/mine.pem"
    assert not fetched  # configured base_url -> defaults not even fetched


def test_org_defaults_fetch_failure_falls_back_to_plain_prompt():
    cfg = _cfg(SKELETON)
    ask = _scripted(["Alex", "2", "https://typed.by.hand", "~/.firekeep/ca.crt", ""])
    wizard.prompt_config(cfg, ask=ask, probe=lambda *_: False,
                         fetch_defaults=lambda c: {})
    assert cfg["office"]["base_url"] == "https://typed.by.hand"
