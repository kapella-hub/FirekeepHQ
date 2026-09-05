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
