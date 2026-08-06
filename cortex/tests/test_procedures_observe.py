"""The gateway stage: recognise, observe, warn. I5 and I6 are asserted here."""
import pytest
import fakeredis.aioredis as fr

from app.agent_gateway.models import Action, ActionBeforeRequest
from app.procedures import store
from app.procedures.observe import ProcedureObserver


class _Settings:
    PROCEDURE_ENABLED = True
    PROCEDURE_WARN_ENABLED = True
    PROCEDURE_EXEC_TTL_DAYS = 90
    PROCEDURE_INDEX_CACHE_SECONDS = 0  # no memoisation in tests
    PROCEDURE_MAX_SPECS = 50
    QDRANT_COLLECTION = "c"


class _ExplodingVector:
    """I5: the pre-edit path must never touch Qdrant."""

    def __getattr__(self, name):
        raise AssertionError(f"the pre-edit path touched Qdrant: {name}")


@pytest.fixture
def r():
    return fr.FakeRedis(decode_responses=True)


def _observer(r, settings=None):
    s = settings or _Settings()
    return ProcedureObserver(get_redis=lambda: r, settings_fn=lambda: s)


def _req(target="requirements.txt", type_="edit_file", session="sess"):
    return ActionBeforeRequest(
        session_id=session, agent_id="ag", adapter="shell-hook",
        action=Action(type=type_, target=target),
    )


async def _seed(r, load_bearing=True):
    import json
    await r.set(store.INDEX_KEY, json.dumps([
        {"skill_id": "s1", "skill_trigger": "dependency change", "step_id": "a",
         "step_text": "regenerate the lock", "pattern": "*.lock",
         "load_bearing": load_bearing, "order": 0},
        {"skill_id": "s1", "skill_trigger": "dependency change", "step_id": "b",
         "step_text": "edit requirements", "pattern": "requirements.txt",
         "load_bearing": False, "order": 1},
    ]))


@pytest.mark.asyncio
async def test_a_match_opens_an_execution_and_warns(r):
    await _seed(r)
    advisories = await _observer(r).observe(_req())
    assert len(advisories) == 1
    assert advisories[0].code == "procedure_step_missing"
    assert "regenerate the lock" in advisories[0].message
    assert advisories[0].evidence_event_id  # the exec_id receipt
    ex = await store.get_execution(r, "sess", "s1")
    assert "b" in ex["observed"]


@pytest.mark.asyncio
async def test_the_same_step_warns_only_once_per_execution(r):
    await _seed(r)
    obs = _observer(r)
    assert len(await obs.observe(_req())) == 1
    assert await obs.observe(_req()) == []


@pytest.mark.asyncio
async def test_an_observed_earlier_step_produces_no_warning(r):
    await _seed(r)
    obs = _observer(r)
    await obs.observe(_req(target="poetry.lock"))   # step a
    assert await obs.observe(_req()) == []          # step b: a is satisfied


@pytest.mark.asyncio
async def test_a_non_load_bearing_earlier_step_never_warns(r):
    await _seed(r, load_bearing=False)
    assert await _observer(r).observe(_req()) == []


@pytest.mark.asyncio
async def test_non_edit_actions_are_ignored(r):
    await _seed(r)
    assert await _observer(r).observe(_req(target="rm -rf *.lock", type_="run_command")) == []


@pytest.mark.asyncio
async def test_an_unknown_session_records_nothing(r):
    """An execution that cannot be joined to an outcome is not evidence."""
    await _seed(r)
    for sid in ("", "unknown"):
        assert await _observer(r).observe(_req(session=sid)) == []


@pytest.mark.asyncio
async def test_disabled_does_nothing_at_all(r):
    await _seed(r)

    class Off(_Settings):
        PROCEDURE_ENABLED = False

    assert await _observer(r, Off()).observe(_req()) == []
    assert await store.get_execution(r, "sess", "s1") is None


@pytest.mark.asyncio
async def test_warn_disabled_still_observes(r):
    await _seed(r)

    class NoWarn(_Settings):
        PROCEDURE_WARN_ENABLED = False

    assert await _observer(r, NoWarn()).observe(_req()) == []
    assert await store.get_execution(r, "sess", "s1") is not None


@pytest.mark.asyncio
async def test_a_dead_redis_never_raises(r):
    await _seed(r)

    class Dead:
        def __getattr__(self, name):
            async def boom(*a, **k):
                raise ConnectionError("redis is down")
            return boom

    obs = ProcedureObserver(get_redis=lambda: Dead(), settings_fn=lambda: _Settings())
    assert await obs.observe(_req()) == []


@pytest.mark.asyncio
async def test_the_stage_never_touches_qdrant(r):
    """I5. ProcedureObserver has no vector client at all — this asserts the
    constructor signature keeps it that way."""
    import inspect

    params = inspect.signature(ProcedureObserver.__init__).parameters
    assert "vector" not in params and "get_vector" not in params
