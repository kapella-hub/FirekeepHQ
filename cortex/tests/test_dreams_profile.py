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


def test_parse_profile_rejects_the_refusal_a_live_run_actually_stored():
    """The exact text a live run stored at a member's deterministic point id
    and then served through the briefing. parse_profile only checked
    empty/overlong, so a refusal was a valid profile — and because a profile is
    replaced IN PLACE, it overwrote the real one."""
    refusal = ("No human is mentioned in the memories. The text describes "
               "system behavior, configuration changes, and automated "
               "processes rather than any individual person.")
    assert profile.parse_profile(refusal, max_chars=800) is None


@pytest.mark.parametrize("refusal", [
    "There is no person described in these memories.",
    "I'm sorry, but I cannot build a profile from the provided memories.",
    "The memories do not mention a specific human being.",
    "Insufficient information to describe how this person works.",
])
def test_parse_profile_rejects_other_refusal_shapes(refusal):
    assert profile.parse_profile(refusal, max_chars=800) is None


def test_parse_profile_accepts_a_real_profile_that_negates_mid_body():
    """The guard is windowed to the OPENING for exactly this case: a genuine
    profile may say "there is no evidence ..." about some trait once it is past
    the first sentence. Rejecting that would be worse than the defect — this is
    the false-positive direction the window exists to bound."""
    text = (
        "Works primarily on the cortex service and reviews changes by reading "
        "the diff before the tests. Consistently asks for measured evidence "
        "rather than assertions, and corrects claims that outrun their data. "
        "There is no evidence they work on the dashboard or the client kit."
    )
    assert profile.parse_profile(text, max_chars=800) == text
    assert text.lower().index("there is no ") >= profile._REFUSAL_WINDOW_CHARS, (
        "the negation must fall OUTSIDE the window or this test proves nothing"
    )


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


def test_build_profile_payload_defaults_match_pre_fix_behaviour():
    """Backward compatibility: a caller that doesn't pass namespace/project
    (as every pre-fix-round caller did) still gets the old hardcoded values."""
    p = profile.build_profile_payload("x", member_id="m", workspace_id="w", run_id="r")
    assert p["namespace"] == "default"
    assert p["project"] is None


def test_build_profile_payload_derives_namespace_and_project():
    """Fix-round review I2: namespace/project must be DERIVED from the
    member's memories, not hardcoded — project is a hard `must` filter in
    VectorClient.search, so a profile stamped project=None when its source
    memories actually carried a project was invisible to project-scoped
    recall."""
    p = profile.build_profile_payload(
        "x", member_id="m", workspace_id="w", run_id="r",
        namespace="acme", project="firekeep",
    )
    assert p["namespace"] == "acme"
    assert p["project"] == "firekeep"


@pytest.mark.asyncio
async def test_write_profile_passes_through_namespace_and_project():
    v = FakeVector()
    point_id = await profile.write_profile(
        v, "text", member_id="m1", workspace_id="ws1", run_id="r",
        namespace="acme", project="firekeep",
    )
    payload = v.points[point_id]["payload"]
    assert payload["namespace"] == "acme"
    assert payload["project"] == "firekeep"
