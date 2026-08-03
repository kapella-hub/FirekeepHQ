"""Pin the load-bearing order inside the production Redis script."""

from app.enroll.lua import ENROLL_CONSUME


def test_rate_count_precedes_ticket_lookup():
    assert ENROLL_CONSUME.index("INCR") < ENROLL_CONSUME.index("EXISTS', KEYS[1]")


def test_replay_precedes_duplicate_credential_guard():
    replay = ENROLL_CONSUME.index("issued_hash == ARGV[4]")
    duplicate = ENROLL_CONSUME.index("EXISTS', KEYS[3]", replay + 1)
    assert replay < duplicate


def test_unknown_path_cannot_create_ticket_or_credential():
    unknown_return = ENROLL_CONSUME.index("return {'unknown'}")
    assert ENROLL_CONSUME.index("HSET', KEYS[3]") > unknown_return
    assert ENROLL_CONSUME.index("SET', KEYS[4]") > unknown_return
