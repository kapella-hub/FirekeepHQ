import json

import httpx
import pytest

from app.dreams import synthesize as syn
from app.dreams.select import Candidate


def _members(n=4):
    return [Candidate(id=f"m{i}", text=f"episode {i}", vector=[1.0], payload={}) for i in range(n)]


def test_request_body_always_disables_thinking():
    body = syn.build_request_body("qwen3:4b", [{"role": "user", "content": "x"}])
    assert body["think"] is False
    assert body["chat_template_kwargs"]["enable_thinking"] is False
    assert body["response_format"] == {"type": "json_object"}


def test_messages_carry_indexed_episodes():
    msgs = syn.build_messages(_members(3))
    joined = " ".join(m["content"] for m in msgs)
    assert "[0]" in joined and "episode 2" in joined


def _raw(**kw):
    ins = {"content": "a durable lesson", "memory_type": "procedural", "source_indices": [0, 1]}
    ins.update(kw)
    return json.dumps({"insights": [ins]})


def test_parse_maps_indices_to_real_ids():
    got = syn.parse_insights(_raw(), _members(), max_chars=800)
    assert len(got) == 1
    assert got[0].source_ids == ["m0", "m1"]


def test_parse_rejects_overlong_insight():
    assert syn.parse_insights(_raw(content="x" * 900), _members(), max_chars=800) == []


def test_parse_forces_procedural_never_reference():
    got = syn.parse_insights(_raw(memory_type="reference"), _members(), max_chars=800)
    assert got and got[0].memory_type == "procedural"


def test_parse_rejects_out_of_range_indices():
    assert syn.parse_insights(_raw(source_indices=[99]), _members(), max_chars=800) == []


def test_parse_rejects_empty_content():
    assert syn.parse_insights(_raw(content="   "), _members(), max_chars=800) == []


@pytest.mark.parametrize("bad", ["", "not json", "{}", '{"insights": "nope"}', '{"insights": [1]}'])
def test_parse_never_raises_on_garbage(bad):
    assert syn.parse_insights(bad, _members(), max_chars=800) == []


def test_parse_caps_at_three_insights():
    many = json.dumps({"insights": [
        {"content": f"c{i}", "memory_type": "procedural", "source_indices": [0]} for i in range(9)
    ]})
    assert len(syn.parse_insights(many, _members(), max_chars=800)) == 3


@pytest.mark.asyncio
async def test_synthesize_sends_think_false_over_the_wire():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": _raw()}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await syn.synthesize(
            _members(), base_url="http://x/v1", model="qwen3:4b", api_key="",
            timeout=5.0, max_chars=800, client=client,
        )
    assert seen["think"] is False
    assert len(out) == 1


@pytest.mark.asyncio
async def test_synthesize_reads_reasoning_when_content_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "", "reasoning": _raw()}}]
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await syn.synthesize(
            _members(), base_url="http://x/v1", model="m", api_key="",
            timeout=5.0, max_chars=800, client=client,
        )
    assert len(out) == 1


@pytest.mark.asyncio
async def test_synthesize_returns_empty_on_backend_error_never_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await syn.synthesize(
            _members(), base_url="http://x/v1", model="m", api_key="",
            timeout=5.0, max_chars=800, client=client,
        ) == []
