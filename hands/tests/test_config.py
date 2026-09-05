import json

import pytest

from firekeep_hands import config


def test_defaults_when_no_files():
    cfg = config.load_config()
    assert (cfg.chord, cfg.deny_chord, cfg.permit_ttl_s, cfg.max_steps) == ("ctrl+alt+y", "ctrl+alt+n", 60, 400)
    pol = config.load_policy()
    assert (pol.apps, pol.domains, pol.remembered) == ([], [], [])


def test_roundtrip_and_unknown_keys_survive(isolated_home):
    cfg = config.load_config()
    cfg.chord = "ctrl+alt+u"
    config.save_config(cfg)
    assert config.load_config().chord == "ctrl+alt+u"
    pol = config.load_policy()
    pol.domains.append("example.com")
    pol.remembered.append(config.Remembered(cls="send", app="Mail", match="Send", until="2099-01-01T00:00:00Z"))
    config.save_policy(pol)
    again = config.load_policy()
    assert again.domains == ["example.com"] and again.remembered[0].cls == "send"


def test_phone_approvals_default_off_and_round_trip(isolated_home):
    """Relay records no actor on a task update, so a phone approval proves
    only that a workspace-key holder completed the task. It stays opt-in."""
    assert config.load_config().phone_approvals is False
    cfg = config.load_config()
    cfg.phone_approvals = True
    config.save_config(cfg)
    assert config.load_config().phone_approvals is True


def test_corrupt_policy_is_treated_as_empty_not_fatal(isolated_home):
    p = isolated_home / "hands" / "policy.json"
    p.parent.mkdir(parents=True)
    p.write_text("{nope")
    assert config.load_policy().apps == []


# -- values, not just the file ---------------------------------------------
#
# "Every load degrades to safe defaults on a missing or corrupt file" used to
# be true of the FILE and false of what was in it: `HandsConfig(**known)` took
# whatever JSON held, so a hand-edited `"permit_ttl_s": "60"` raised inside
# `PermitStore.__init__` and a `"max_steps": "12"` raised a TypeError in
# `_step_guard` — both a long way from the typo that caused them.


def _write_config(home, payload: dict) -> None:
    path = home / "hands" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_a_hand_edited_config_never_raises_and_keeps_what_it_can(isolated_home):
    _write_config(isolated_home, {
        "max_steps": "12",             # a string that IS an int — coerced
        "phone_approvals": "nope",     # not a bool in any spelling — dropped
        "chord": "ctrl+alt+k",         # fine
    })
    cfg = config.load_config()
    assert cfg.max_steps == 12                       # coerced, not dropped
    assert cfg.phone_approvals is False              # the default, not "nope"
    assert cfg.chord == "ctrl+alt+k"                 # untouched


@pytest.mark.parametrize("raw,expected", [
    ({"permit_ttl_s": "60"}, 60),
    ({"permit_ttl_s": "-5"}, -5),
    ({"permit_ttl_s": 90}, 90),
    ({"permit_ttl_s": " 45 "}, 45),
    ({"permit_ttl_s": "lots"}, 60),      # default
    ({"permit_ttl_s": 1.5}, 60),         # a float is not a whole number of seconds
    ({"permit_ttl_s": True}, 60),        # bool is an int subclass; a TTL of 1 is not what was meant
    ({"permit_ttl_s": None}, 60),
    ({"permit_ttl_s": [60]}, 60),
])
def test_int_fields_take_ints_and_int_strings_and_nothing_else(isolated_home, raw, expected):
    _write_config(isolated_home, raw)
    assert config.load_config().permit_ttl_s == expected


@pytest.mark.parametrize("raw,expected", [
    ({"phone_approvals": True}, True),
    ({"phone_approvals": "true"}, True),
    ({"phone_approvals": "YES"}, True),
    ({"phone_approvals": "1"}, True),
    ({"phone_approvals": "false"}, False),
    ({"phone_approvals": "0"}, False),
    ({"phone_approvals": 1}, False),      # not a word and not a bool — dropped
    ({"phone_approvals": "maybe"}, False),
])
def test_bool_fields_use_the_same_words_the_config_set_command_does(
    isolated_home, raw, expected
):
    """`firekeep hands config set phone_approvals yes` and a hand-edited
    `"phone_approvals": "yes"` have to mean the same thing — `cli` imports
    these word lists from `config` rather than keeping a second copy."""
    _write_config(isolated_home, raw)
    assert config.load_config().phone_approvals is expected


def test_a_string_field_refuses_a_number(isolated_home):
    _write_config(isolated_home, {"chord": 7})
    assert config.load_config().chord == "ctrl+alt+y"


def test_a_dropped_value_is_logged_so_the_typo_is_findable(isolated_home, monkeypatch):
    logged = []
    monkeypatch.setattr(config.hooklog, "log_failure",
                        lambda hook, message, exc=None: logged.append(message))
    _write_config(isolated_home, {"max_steps": "lots"})
    config.load_config()
    assert len(logged) == 1
    assert "max_steps" in logged[0] and "lots" in logged[0] and "default" in logged[0]


def test_saving_after_a_dropped_value_heals_the_file(isolated_home):
    """A setting Hands could not read was not in force, and the file should
    say so rather than keep a value that never applied."""
    _write_config(isolated_home, {"max_steps": "lots", "browser": "edge"})
    config.save_config(config.load_config())
    on_disk = json.loads((isolated_home / "hands" / "config.json").read_text(encoding="utf-8"))
    assert on_disk["max_steps"] == 400
    assert on_disk["browser"] == "edge"      # the good value survives the heal


def test_unknown_keys_still_survive_a_coerced_load(isolated_home):
    """The downgrade-safety property from this module's docstring has to
    outlive the coercion pass: a newer client's field is not a bad value."""
    _write_config(isolated_home, {"max_steps": "12", "from_a_newer_client": {"a": 1}})
    config.save_config(config.load_config())
    on_disk = json.loads((isolated_home / "hands" / "config.json").read_text(encoding="utf-8"))
    assert on_disk["from_a_newer_client"] == {"a": 1}
    assert on_disk["max_steps"] == 12
