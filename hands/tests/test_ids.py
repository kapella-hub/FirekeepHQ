from firekeep_hands import ids


def test_machine_id_is_stable_and_private(isolated_home):
    a = ids.machine_id()
    b = ids.machine_id()
    assert a == b and len(a) == 32 and int(a, 16) >= 0


def test_action_hash_is_order_independent():
    assert ids.action_hash({"kind": "click", "ref": "c1"}) == ids.action_hash({"ref": "c1", "kind": "click"})
    assert len(ids.action_hash({"kind": "wait"})) == 16


def test_challenge_id_is_deterministic_and_sensitive_to_every_field():
    base = ids.challenge_id_for("m", "s", "t", 3, "abcd")
    assert base == ids.challenge_id_for("m", "s", "t", 3, "abcd") and len(base) == 32
    for variant in [("x","s","t",3,"abcd"), ("m","x","t",3,"abcd"), ("m","s","x",3,"abcd"),
                    ("m","s","t",4,"abcd"), ("m","s","t",3,"abce")]:
        assert ids.challenge_id_for(*variant) != base
