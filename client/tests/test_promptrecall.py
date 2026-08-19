"""Proactive recall — pushed memory at every prompt (spec §6, client list).

What these tests are FOR: this is the only feature in the kit that writes into the
user's context unasked, on every single turn. The prompt core already carries the
scar of getting that wrong once (raw JSON, five stale messages, every prompt,
2026-07-14) — so the properties worth binding are the ones that keep it quiet:
what is never queried at all, what is filtered out, what is never shown twice, and
that every failure path is silent rather than loud.

The transport is always faked. Nothing here touches a network, and the fake is
what pins the request contract (task/top_k/trigger) the server is built against.
"""
from __future__ import annotations

import json
import textwrap

import pytest

from firekeep_client import promptrecall, resolver, state, transport

REAL_PROMPT = "why does the docdex ingest time out on large PDFs and how do we fix it"


@pytest.fixture
def client_env(tmp_path, monkeypatch):
    """A tmp ~/.firekeep, mirroring tests/hooks/conftest.py's fixture of the same
    name. Defined here rather than imported because promptrecall is a top-level
    module, not a hook core — this file must be runnable without the hooks package's
    conftest, and the last two classes exercise the hook wiring on the same env."""
    cfg = tmp_path / "config"
    cfg.write_text(textwrap.dedent("""\
        [identity]
        agent_id = tester
        [server]
        kind = ports
        scheme = http
        host = 127.0.0.1
        verify_tls = false
    """))
    cache = tmp_path / "cache"
    cache.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(cache))
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(logs))
    monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)
    monkeypatch.delenv("FIREKEEP_NO_RECALL_PUSH", raising=False)
    monkeypatch.delenv("FIREKEEP_RECALL_PUSH_MIN_SCORE", raising=False)
    monkeypatch.delenv("FIREKEEP_RECALL_PUSH_TIMEOUT_SECONDS", raising=False)
    return {"tmp": tmp_path, "cfg": cfg, "cache": cache, "logs": logs, "agent": "tester"}


def _cfg():
    return resolver.load_config()


def _source(content, raw_score, *, mid=None, store="vector"):
    """A source shaped like the ones /memory/recall really returns: `score` is the
    within-set normalized RANK, the honest relevance lives in metadata.raw_score."""
    metadata = {"raw_score": raw_score}
    if mid is not None:
        metadata["id"] = mid
    return {"store": store, "content": content, "score": 1.0, "metadata": metadata}


@pytest.fixture
def fake_recall(monkeypatch):
    """Patch transport.post_json; return the recorded calls plus a setter for the
    response (or an exception to raise)."""
    state_ = {"sources": [], "raise": None, "degraded": False}
    calls = []

    def fake_post(url, body, **kwargs):
        calls.append({"url": url, "body": body, "kwargs": kwargs})
        if state_["raise"] is not None:
            raise state_["raise"]
        return {"context_block": "", "sources": state_["sources"], "score": 1.0,
                "degraded": state_["degraded"]}

    monkeypatch.setattr(transport, "post_json", fake_post)

    class Handle:
        def returns(self, *sources, degraded=False):
            state_["sources"] = list(sources)
            state_["degraded"] = degraded

        def fails(self, exc):
            state_["raise"] = exc

        @property
        def calls(self):
            return calls

    return Handle()


class TestGates:
    """Prompts that are never queried at all. Each of these must cost ZERO server
    round-trips — the gate exists to protect the hook budget, not just the output."""

    def test_short_prompt_is_never_queried(self, client_env, fake_recall):
        fake_recall.returns(_source("relevant thing", 0.9, mid="m1"))
        assert promptrecall.nudge(_cfg(), {"prompt": "ok thanks"}) == ""
        assert fake_recall.calls == []

    def test_prompt_is_measured_after_whitespace_collapse(self, client_env, fake_recall):
        # 23 characters of text padded out with whitespace: still no signal.
        fake_recall.returns(_source("relevant thing", 0.9, mid="m1"))
        assert promptrecall.nudge(_cfg(), {"prompt": "  ok   thanks   do   it  "}) == ""
        assert fake_recall.calls == []

    def test_slash_command_is_never_queried(self, client_env, fake_recall):
        fake_recall.returns(_source("relevant thing", 0.9, mid="m1"))
        slash = "/commit and push the proactive recall work to main please"
        assert promptrecall.nudge(_cfg(), {"prompt": slash}) == ""
        assert fake_recall.calls == []

    def test_runtime_that_delivers_no_prompt_text_is_skipped(self, client_env, fake_recall):
        """opencode maps session.idle and carries no prompt — the honest outcome is
        nothing, not a recall against an empty string."""
        fake_recall.returns(_source("relevant thing", 0.9, mid="m1"))
        assert promptrecall.nudge(_cfg(), {}) == ""
        assert fake_recall.calls == []

    def test_env_kill_switch(self, client_env, fake_recall, monkeypatch):
        monkeypatch.setenv("FIREKEEP_NO_RECALL_PUSH", "1")
        fake_recall.returns(_source("relevant thing", 0.9, mid="m1"))
        assert promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT}) == ""
        assert fake_recall.calls == []

    def test_config_kill_switch(self, client_env, fake_recall):
        client_env["cfg"].write_text(
            client_env["cfg"].read_text() + "[recall]\npush = false\n")
        fake_recall.returns(_source("relevant thing", 0.9, mid="m1"))
        assert promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT}) == ""
        assert fake_recall.calls == []

    def test_blank_config_value_means_default_on_not_off(self, client_env, fake_recall):
        """A half-edited config gets the documented default, not silence."""
        client_env["cfg"].write_text(client_env["cfg"].read_text() + "[recall]\npush =\n")
        fake_recall.returns(_source("relevant thing", 0.9, mid="m1"))
        assert promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT}) != ""

    def test_on_by_default(self, client_env, fake_recall):
        fake_recall.returns(_source("relevant thing", 0.9, mid="m1"))
        assert promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT}) != ""


class TestRequestContract:
    def test_request_shape(self, client_env, fake_recall):
        fake_recall.returns(_source("relevant thing", 0.9, mid="m1"))
        promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT})
        call = fake_recall.calls[0]
        assert call["url"].endswith("/memory/recall")
        assert call["body"] == {"task": REAL_PROMPT, "top_k": 3, "format": "raw",
                                "trigger": "prompt-hook"}

    def test_format_is_raw_so_no_llm_runs_inside_the_hook_budget(self, client_env,
                                                                 fake_recall):
        """ContextQuery defaults to "synthesized", which runs an LLM pass over the
        results. Against a 2.5s hook timeout that is not a slower answer — it is no
        answer, on every prompt. Bridge's proactive recall passes raw for exactly
        this reason (SP0 C6, defect #11)."""
        fake_recall.returns()
        promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT})
        assert fake_recall.calls[0]["body"]["format"] == "raw"

    def test_no_namespace_is_sent_so_every_category_is_searched(self, client_env,
                                                               fake_recall):
        """Sending the literal "default" would scope the recall to that one
        category and hide everything filed under `infrastructure` and friends —
        146 memories on the live store."""
        fake_recall.returns()
        promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT})
        assert "namespace" not in fake_recall.calls[0]["body"]

    def test_trigger_is_sent_so_pushed_recall_stays_attributable(self, client_env, fake_recall):
        """A pushed recall IS a recall: the fleet's 'recall before you answer'
        number moves the day this ships. `trigger` is what keeps that separable
        from deliberate recall in the replay record instead of confounding it."""
        fake_recall.returns(_source("relevant thing", 0.9, mid="m1"))
        promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT})
        assert fake_recall.calls[0]["body"]["trigger"] == "prompt-hook"

    def test_task_is_truncated_to_the_server_limit(self, client_env, fake_recall):
        fake_recall.returns()
        promptrecall.nudge(_cfg(), {"prompt": "x" * 5000})
        assert len(fake_recall.calls[0]["body"]["task"]) == 2000

    def test_timeout_is_bounded_and_configurable(self, client_env, fake_recall, monkeypatch):
        fake_recall.returns()
        promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT})
        assert fake_recall.calls[0]["kwargs"]["timeout"] == 2.5
        monkeypatch.setenv("FIREKEEP_RECALL_PUSH_TIMEOUT_SECONDS", "0.75")
        promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT})
        assert fake_recall.calls[1]["kwargs"]["timeout"] == 0.75


class TestThreshold:
    def test_below_floor_is_filtered_out(self, client_env, fake_recall):
        fake_recall.returns(_source("weakly related", 0.31, mid="m1"))
        assert promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT}) == ""

    def test_floor_is_configurable(self, client_env, fake_recall, monkeypatch):
        fake_recall.returns(_source("weakly related", 0.31, mid="m1"))
        monkeypatch.setenv("FIREKEEP_RECALL_PUSH_MIN_SCORE", "0.3")
        assert "weakly related" in promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT})

    def test_unparseable_floor_falls_back_to_the_default(self, client_env, monkeypatch):
        monkeypatch.setenv("FIREKEEP_RECALL_PUSH_MIN_SCORE", "banana")
        assert promptrecall.min_score() == promptrecall.DEFAULT_MIN_SCORE

    def test_nan_floor_falls_back_rather_than_disabling_the_comparison(
            self, client_env, monkeypatch):
        """`float('nan')` parses fine and makes every `>=` False — a floor that
        silently drops everything. It must not be accepted."""
        monkeypatch.setenv("FIREKEEP_RECALL_PUSH_MIN_SCORE", "nan")
        assert promptrecall.min_score() == promptrecall.DEFAULT_MIN_SCORE

    def test_floor_reads_raw_score_not_the_normalized_rank(self, client_env, fake_recall):
        """THE load-bearing test of this file.

        `MemorySource.score` is the within-set min-max rank: the best entry is
        exactly 1.0 by construction, so a nonsense query's top hit scores 1.0 just
        like a real one (measured live 2026-08-06). Thresholding on it would admit
        the top of every result set on every prompt — a push that fires always,
        which is precisely the noise failure this feature was designed against.
        Both sources below carry score=1.0; only the one whose REAL relevance
        clears the floor may be shown.
        """
        fake_recall.returns(
            {"store": "vector", "content": "actually irrelevant", "score": 1.0,
             "metadata": {"id": "m1", "raw_score": 0.12}},
        )
        assert promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT}) == ""

    def test_degraded_response_injects_nothing(self, client_env, fake_recall):
        """`degraded` means vector search failed and the results are graph-only.
        The floor is calibrated on cosine; a graph-only set is a different scale,
        so pushing it would be pushing something the threshold cannot judge."""
        fake_recall.returns(_source("a graph hit", 0.99, mid="m1", store="graph"),
                            degraded=True)
        assert promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT}) == ""

    def test_source_without_a_real_score_is_dropped_not_admitted(self, client_env, fake_recall):
        """The resolution-bonus entry carries a sentinel score and no raw_score.
        Unknown relevance must fail dark, not loud."""
        fake_recall.returns({"store": "graph", "content": "a resolution", "score": 1.2,
                             "metadata": {"name": "resolution"}})
        assert promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT}) == ""


class TestDedupe:
    def test_a_memory_is_injected_once_and_then_suppressed(self, client_env, fake_recall):
        fake_recall.returns(_source("the docdex ingest timeout fix", 0.88, mid="m1"))
        first = promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT})
        assert "the docdex ingest timeout fix" in first
        assert promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT}) == ""

    def test_a_different_memory_still_gets_through(self, client_env, fake_recall):
        fake_recall.returns(_source("first memory", 0.88, mid="m1"))
        assert "first memory" in promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT})
        fake_recall.returns(_source("first memory", 0.88, mid="m1"),
                            _source("second memory", 0.80, mid="m2"))
        out = promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT})
        assert "second memory" in out
        assert "first memory" not in out

    def test_only_ids_actually_shown_are_recorded(self, client_env, fake_recall):
        """Four eligible sources, three injected. Recording the fourth would
        suppress a memory the user never saw."""
        fake_recall.returns(*[_source(f"memory {i}", 0.9 - i / 100, mid=f"m{i}")
                              for i in range(4)])
        first = promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT})
        assert "memory 3" not in first
        assert "memory 3" in promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT})

    def test_duplicate_ids_within_one_response_render_once(self, client_env, fake_recall):
        fake_recall.returns(_source("same memory", 0.9, mid="m1"),
                            _source("same memory", 0.8, mid="m1"))
        out = promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT})
        assert out.count("same memory") == 1

    def test_seen_list_declares_a_ttl_so_suppression_cannot_be_permanent(
            self, client_env, fake_recall):
        """The key carries no session component. Without an expiry a memory shown
        once would be suppressed on this machine forever."""
        import time

        fake_recall.returns(_source("the docdex ingest timeout fix", 0.88, mid="m1"))
        assert promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT}) != ""
        key = "recall_push_tester"
        assert state._scratch_ttl_file(key).exists(), (
            "the dedupe list declared no expiry — it would suppress this memory "
            "for the life of the machine")
        state._scratch_ttl_file(key).write_text(str(time.time() - 1), encoding="utf-8")
        assert promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT}) != ""

    def test_seen_list_is_capped(self, client_env):
        promptrecall.remember("tester", [f"m{i}" for i in range(120)])
        assert len(promptrecall.read_seen("tester")) == promptrecall.SEEN_CAP
        assert promptrecall.read_seen("tester")[-1] == "m119"

    def test_corrupt_seen_list_costs_a_repeat_not_the_feature(self, client_env, fake_recall):
        state.write_scratch("recall_push_tester", "{not json")
        fake_recall.returns(_source("still shown", 0.9, mid="m1"))
        assert "still shown" in promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT})

    def test_sources_without_ids_dedupe_on_content(self, client_env, fake_recall):
        bare = {"store": "graph", "content": "an unidentified memory", "score": 1.0,
                "metadata": {"raw_score": 0.9}}
        fake_recall.returns(bare)
        assert "an unidentified memory" in promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT})
        assert promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT}) == ""

    def test_graph_memory_ids_are_used_as_the_dedupe_key(self, client_env, fake_recall):
        graph = {"store": "graph", "content": "a graph memory", "score": 1.0,
                 "metadata": {"raw_score": 0.9, "memory_ids": ["g-1"]}}
        fake_recall.returns(graph)
        assert promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT}) != ""
        assert promptrecall.read_seen("tester") == ["g-1"]


class TestRender:
    def test_block_format(self, client_env, fake_recall):
        fake_recall.returns(_source("the docdex ingest timeout fix", 0.712, mid="m1"))
        out = promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT})
        assert out == (
            "[firekeep recall] team memory that may be relevant "
            "(verify before relying on it):\n"
            "- the docdex ingest timeout fix (score 0.71)"
        )

    def test_at_most_three(self, client_env, fake_recall):
        fake_recall.returns(*[_source(f"memory {i}", 0.9, mid=f"m{i}") for i in range(9)])
        out = promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT})
        assert len(out.splitlines()) == 4  # header + 3

    def test_long_snippet_is_trimmed_to_one_bounded_line(self, client_env, fake_recall):
        fake_recall.returns(_source("z" * 900, 0.9, mid="m1"))
        line = promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT}).splitlines()[1]
        snippet = line[2:line.rindex(" (score")]
        assert len(snippet) == promptrecall.MAX_LINE_CHARS
        assert snippet.endswith("...")

    def test_multiline_snippet_is_collapsed(self, client_env, fake_recall):
        fake_recall.returns(_source("line one\nline two\n\n  line three", 0.9, mid="m1"))
        out = promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT})
        assert len(out.splitlines()) == 2
        assert "- line one line two line three (score 0.90)" in out

    def test_never_raw_json(self, client_env, fake_recall):
        fake_recall.returns(_source("a memory", 0.9, mid="m1"))
        assert "{" not in promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT})


class TestFailOpen:
    def _log(self, client_env):
        path = client_env["logs"] / "hooks.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_timeout_injects_nothing_and_leaves_a_hooklog_line(self, client_env, fake_recall):
        fake_recall.fails(transport.TransportError(
            "POST http://127.0.0.1:8100/memory/recall timed out after 2.5s"))
        assert promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT}) == ""
        assert "timed out" in self._log(client_env)

    def test_unreachable_server_injects_nothing_and_logs(self, client_env, fake_recall):
        fake_recall.fails(transport.TransportError("unreachable: Connection refused"))
        assert promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT}) == ""
        assert "proactive recall failed" in self._log(client_env)

    def test_malformed_response_injects_nothing(self, client_env, monkeypatch):
        monkeypatch.setattr(transport, "post_json", lambda *a, **k: "not a dict")
        assert promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT}) == ""

    def test_unresolvable_endpoint_injects_nothing_and_never_raises(self, client_env,
                                                                    fake_recall):
        cfg_path = client_env["cfg"]
        cfg_path.write_text(cfg_path.read_text().replace("kind = ports", "kind = carrier-pigeon"))
        assert promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT}) == ""
        assert "proactive recall failed" in self._log(client_env)

    def test_a_failed_dedupe_write_still_injects(self, client_env, fake_recall, monkeypatch):
        """Losing the dedupe record costs one future repeat of a memory the user
        has already seen. Losing the injection costs them the memory — so the
        bookkeeping write must not be able to swallow the thing it books."""
        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(state, "write_scratch", boom)
        fake_recall.returns(_source("still shown", 0.9, mid="m1"))
        assert "still shown" in promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT})
        assert "dedupe write failed" in self._log(client_env)

    def test_hooklog_is_written_under_the_prompt_hook_name(self, client_env, fake_recall):
        fake_recall.fails(RuntimeError("boom"))
        promptrecall.nudge(_cfg(), {"prompt": REAL_PROMPT})
        assert "| prompt |" in self._log(client_env)


def test_seen_list_is_stored_as_json(client_env):
    """The scratch value is a plain JSON list — greppable by a human debugging why
    a memory stopped showing up."""
    promptrecall.remember("tester", ["m1", "m2"])
    assert json.loads(state.read_scratch("recall_push_tester")) == ["m1", "m2"]
