import pytest

from app.dreams import profile, store


class FakeVector:
    def __init__(self):
        self.points = {}

    async def upsert_point(self, point_id, text, payload):
        self.points[point_id] = {"text": text, "payload": payload}
        return point_id


def test_profile_payload_keys_on_member_id_not_agent_id():
    p = profile.build_profile_payload("who they are", member_id="mem1",
                                      workspace_id="ws1", run_id="r")
    assert p["member_id"] == "mem1"
    assert p["source"] == "dream_profile"
    assert p["agent_id"] == "dream"


def test_profile_is_excluded_from_future_candidate_selection():
    from datetime import datetime, timedelta, timezone
    from app.dreams.select import is_candidate

    p = profile.build_profile_payload("x", member_id="m", workspace_id="w", run_id="r")
    p["timestamp"] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    assert not is_candidate(p, now=datetime.now(timezone.utc), min_age_days=2,
                            owm_floor=0.35, owm_prior_n=5)


def test_parse_profile_rejects_empty_and_overlong():
    assert profile.parse_profile("   ", max_chars=800) is None
    assert profile.parse_profile("x" * 900, max_chars=800) is None
    assert profile.parse_profile("  real  ", max_chars=800) == "real"


@pytest.mark.asyncio
async def test_repeated_profile_writes_leave_exactly_one_point():
    v = FakeVector()
    for text in ("v1", "v2", "v3"):
        await profile.write_profile(v, text, member_id="mem1", workspace_id="ws1", run_id="r")
    assert len(v.points) == 1
    only = next(iter(v.points.values()))
    assert only["text"] == "v3"


@pytest.mark.asyncio
async def test_two_members_get_two_points():
    v = FakeVector()
    await profile.write_profile(v, "a", member_id="m1", workspace_id="ws1", run_id="r")
    await profile.write_profile(v, "b", member_id="m2", workspace_id="ws1", run_id="r")
    assert len(v.points) == 2
    assert store.profile_point_id("m1", "ws1") in v.points
