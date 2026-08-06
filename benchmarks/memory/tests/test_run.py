import json

import pytest

from bench import dream_ab, report, run as runmod
from bench.common import run_work_dir, sanitize_label
from tests.conftest import FIXTURE_ROWS


def _record(work, label) -> dict:
    """The newest published run-record for `label` under `work`. Sorting by
    name is sorting by time — the filename is timestamp-prefixed."""
    paths = sorted(p for p in work.glob("*.json")
                   if p.name.endswith(f"-{sanitize_label(label)}.json"))
    assert paths, f"no run-record written for {label!r}"
    return json.loads(paths[-1].read_text(encoding="utf-8"))


def test_preflight_flags_cloud_reader(monkeypatch):
    # Isolate from live services: health/dataset/model checks all pass.
    monkeypatch.setattr(runmod, "_check_health", lambda url: None)
    monkeypatch.setattr(runmod, "_check_dataset", lambda: None)
    monkeypatch.setattr(runmod, "_ollama_models", lambda url: ["qwen3:14b", "mxbai-embed-large"])
    monkeypatch.setattr(runmod, "_free_gb", lambda: 100.0)
    fails = runmod.preflight("http://c", "http://o", "minimax-m2:cloud", skip_qa=False)
    assert any("cloud" in f for f in fails)


def test_preflight_skips_reader_check_when_qa_skipped(monkeypatch):
    monkeypatch.setattr(runmod, "_check_health", lambda url: None)
    monkeypatch.setattr(runmod, "_check_dataset", lambda: None)
    monkeypatch.setattr(runmod, "_ollama_models", lambda url: ["mxbai-embed-large"])
    monkeypatch.setattr(runmod, "_free_gb", lambda: 100.0)
    fails = runmod.preflight("http://c", "http://o", "qwen3:14b", skip_qa=True)
    assert fails == []


def test_preflight_requires_embed_model(monkeypatch):
    monkeypatch.setattr(runmod, "_check_health", lambda url: None)
    monkeypatch.setattr(runmod, "_check_dataset", lambda: None)
    monkeypatch.setattr(runmod, "_ollama_models", lambda url: [])
    monkeypatch.setattr(runmod, "_free_gb", lambda: 100.0)
    fails = runmod.preflight("http://c", "http://o", "qwen3:14b", skip_qa=True)
    assert any("mxbai-embed-large" in f for f in fails)


def test_run_rejects_defaults_config_without_skip_qa(monkeypatch):
    # QA always reads the bench config's recall output; --config defaults
    # (without --skip-qa) must be rejected before any stage runs, not just
    # mislabeled in the report. Prove nothing downstream was touched.
    def _boom(*a, **kw):
        raise AssertionError("ingest.ingest must not run — the guard should reject first")

    monkeypatch.setattr(runmod.ingest, "ingest", _boom)
    rc = runmod.run(["--config", "defaults"])
    assert rc != 0


def test_run_accepts_defaults_config_with_skip_qa(monkeypatch, capsys):
    # --skip-qa lifts the bench-config requirement; prove the guard was
    # cleared by showing execution reached preflight (stubbed to fail with a
    # recognizable message, so we don't need a live stack).
    monkeypatch.setattr(runmod, "preflight", lambda *a, **kw: ["stub failure — reached preflight"])
    rc = runmod.run(["--config", "defaults", "--skip-qa"])
    assert rc == 1
    assert "stub failure — reached preflight" in capsys.readouterr().out


def test_assemble_report_threads_reader_model_into_meta(monkeypatch, tmp_path):
    captured = {}

    def fake_load_meta(cortex_url, ollama_url, reader_model):
        captured["reader_model"] = reader_model
        return {"dataset": {}, "cortex_version": {}, "models": {}, "configs": {}}

    monkeypatch.setattr(runmod.report, "_load_meta", fake_load_meta)
    monkeypatch.setattr(runmod, "WORK_DIR", tmp_path)
    monkeypatch.setattr(runmod, "RESULTS_DIR", tmp_path)

    runmod._assemble_report("test-label", "http://c", "http://o", "my-reader-model")
    assert captured["reader_model"] == "my-reader-model"


def test_assemble_report_persists_ingest_errors_and_per_question(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runmod.report, "_load_meta",
        lambda *a, **kw: {"dataset": {}, "cortex_version": {}, "models": {}, "configs": {}},
    )
    monkeypatch.setattr(runmod, "WORK_DIR", tmp_path)
    monkeypatch.setattr(runmod, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(runmod.report, "WORK_DIR", tmp_path)

    # ingest_errors.json is SHARED (work/ root) — it describes the shared
    # ingested store. Recall/score artefacts are per label (work/<label>/).
    (tmp_path / "ingest_errors.json").write_text(json.dumps(["lm_q1/s_a: boom"]), encoding="utf-8")
    work = run_work_dir("test-label", work_dir=tmp_path)
    work.mkdir()
    (work / "recall_bench.jsonl").write_text(
        json.dumps({"question_id": "q1", "hits": [{"session_id": "s_a"}], "error": None}) + "\n",
        encoding="utf-8",
    )
    (work / "scores_bench.json").write_text(
        json.dumps({"k": 10, "overall": {"n": 1, "recall_at_k": 1, "coverage_at_k": 1,
                                          "mrr": 1, "ndcg_at_k": 1},
                    "by_question_type": {}, "errored_questions": [], "missing_questions": [],
                    "abstention_excluded": 0}),
        encoding="utf-8",
    )

    runmod._assemble_report("test-label", "http://c", "http://o", "my-reader-model")

    result = _record(tmp_path, "test-label")
    assert result["ingest_errors"] == 1
    assert result["ingest_error_keys"] == ["lm_q1/s_a: boom"]
    assert result["per_question"]["bench"][0]["question_id"] == "q1"


def test_persist_ingest_errors_writes_full_list(tmp_path, monkeypatch):
    monkeypatch.setattr(runmod, "WORK_DIR", tmp_path)
    runmod._persist_ingest_errors(["lm_q1/s_a: boom", "lm_q2/s_b: boom2"])
    written = json.loads((tmp_path / "ingest_errors.json").read_text(encoding="utf-8"))
    assert written == ["lm_q1/s_a: boom", "lm_q2/s_b: boom2"]


def test_run_persists_ingest_errors_after_ingest_stage(monkeypatch, tmp_path):
    # Full pipeline with every stage stubbed except the write we're testing:
    # ingest reports one error, and by the time _assemble_report runs,
    # work/ingest_errors.json must already reflect it (I3 — ingest failures
    # must reach the published record, not just stdout).
    monkeypatch.setattr(runmod, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(runmod, "load_dataset", lambda path: [])
    monkeypatch.setattr(runmod, "WORK_DIR", tmp_path)
    monkeypatch.setattr(runmod, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(runmod.report, "WORK_DIR", tmp_path)

    class FakeIngestStats:
        sessions_done = 0
        sessions_skipped = 0
        learn_calls = 0
        errors = ["lm_q1/s_a: boom"]

    async def fake_ingest(*a, **kw):
        return FakeIngestStats()

    monkeypatch.setattr(runmod.ingest, "ingest", fake_ingest)

    class FakeRecallStats:
        completed = 0
        skipped = 0
        errored = 0

    async def fake_run_recall(*a, **kw):
        return FakeRecallStats()

    monkeypatch.setattr(runmod.recall, "run_recall", fake_run_recall)
    monkeypatch.setattr(
        runmod.score_retrieval, "score_run",
        lambda *a, **kw: {
            "k": 10,
            "overall": {"n": 0, "recall_at_k": 0, "coverage_at_k": 0, "mrr": 0, "ndcg_at_k": 0},
            "by_question_type": {}, "errored_questions": [], "missing_questions": [],
            "abstention_excluded": 0,
        },
    )
    monkeypatch.setattr(
        runmod.report, "_load_meta",
        lambda *a, **kw: {"dataset": {}, "cortex_version": {}, "models": {}, "configs": {}},
    )

    rc = runmod.run(["--skip-qa"])
    assert rc == 0
    errors = json.loads((tmp_path / "ingest_errors.json").read_text(encoding="utf-8"))
    assert errors == ["lm_q1/s_a: boom"]


# --- run-label scoping ------------------------------------------------------

_SCORE_RUN = {
    "k": 10,
    "overall": {"n": 1, "recall_at_k": 1, "coverage_at_k": 1, "mrr": 1, "ndcg_at_k": 1},
    "by_question_type": {}, "errored_questions": [], "missing_questions": [],
    "abstention_excluded": 0,
}


@pytest.fixture
def stubbed_run(monkeypatch, tmp_path):
    """Every stage stubbed EXCEPT recall, which runs for real (over a mock
    transport) so its resume logic — the thing that leaked across labels — is
    the code actually under test."""
    import httpx

    from bench import recall as recallmod

    monkeypatch.setattr(runmod, "preflight", lambda *a, **kw: [])
    monkeypatch.setattr(runmod, "load_dataset", lambda path: list(FIXTURE_ROWS))
    monkeypatch.setattr(runmod, "WORK_DIR", tmp_path)
    monkeypatch.setattr(runmod, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(runmod.report, "WORK_DIR", tmp_path)
    monkeypatch.setattr(
        runmod.report, "_load_meta",
        lambda *a, **kw: {"dataset": {}, "cortex_version": {}, "models": {}, "configs": {}},
    )

    class FakeIngestStats:
        sessions_done = 0
        sessions_skipped = 0
        learn_calls = 0
        errors = []

    async def fake_ingest(*a, **kw):
        return FakeIngestStats()

    monkeypatch.setattr(runmod.ingest, "ingest", fake_ingest)
    monkeypatch.setattr(
        runmod.score_retrieval, "score_run", lambda *a, **kw: dict(_SCORE_RUN))

    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"context_block": "ctx", "sources": []})

    real_run_recall = recallmod.run_recall

    async def recall_over_mock(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return await real_run_recall(*args, **kwargs)

    monkeypatch.setattr(runmod.recall, "run_recall", recall_over_mock)
    return tmp_path, calls


def test_recall_and_score_artefacts_land_under_the_run_label(stubbed_run):
    work, _ = stubbed_run
    assert runmod.run(["--skip-qa", "--config", "bench", "--run-label", "pre-dream"]) == 0

    label_dir = run_work_dir("pre-dream", work_dir=work)
    assert (label_dir / "recall_bench.jsonl").exists()
    assert (label_dir / "scores_bench.json").exists()
    # Nothing may be written back into the unscoped root — that is what leaked.
    assert not (work / "recall_bench.jsonl").exists()
    assert not (work / "scores_bench.json").exists()
    # The ingest ledger stays shared and unscoped by design.
    assert not (label_dir / "ingest_ledger.jsonl").exists()


def test_a_second_label_re_recalls_instead_of_skipping(stubbed_run):
    """The defect: `--run-label post-dream` completed in 40s, skipped all 500
    recalls, and re-scored the pre-dream artefacts to a +0.0000 delta."""
    work, calls = stubbed_run

    assert runmod.run(["--skip-qa", "--config", "bench", "--run-label", "pre-dream"]) == 0
    first = len(calls)
    assert first == len(FIXTURE_ROWS)

    assert runmod.run(["--skip-qa", "--config", "bench", "--run-label", "post-dream"]) == 0
    assert len(calls) == 2 * first, "second label must recall again, not reuse the first label's rows"

    counts_path = run_work_dir("post-dream", work_dir=work) / "recall_counts.json"
    counts = json.loads(counts_path.read_text(encoding="utf-8"))
    assert counts["bench"]["completed"] == first
    assert counts["bench"]["skipped"] == 0


def test_resume_within_one_label_still_skips(stubbed_run):
    """Scoping must not cost resume: re-running the SAME label repeats nothing
    (a 4-hour leg has to survive an interruption)."""
    work, calls = stubbed_run

    assert runmod.run(["--skip-qa", "--config", "bench", "--run-label", "pre-dream"]) == 0
    first = len(calls)
    assert runmod.run(["--skip-qa", "--config", "bench", "--run-label", "pre-dream"]) == 0
    assert len(calls) == first, "same label must resume, not re-recall"

    counts_path = run_work_dir("pre-dream", work_dir=work) / "recall_counts.json"
    counts = json.loads(counts_path.read_text(encoding="utf-8"))
    # completed accumulates over the label's invocations; skipped is the last
    # invocation's only (summing skips would double-count the same questions).
    assert counts["bench"]["completed"] == first
    assert counts["bench"]["skipped"] == first
    assert counts["bench"]["invocations"] == 2


def test_run_record_carries_recall_counts_per_config(stubbed_run):
    work, calls = stubbed_run
    assert runmod.run(["--skip-qa", "--config", "bench", "--run-label", "pre-dream"]) == 0

    counts = _record(work, "pre-dream")["retrieval"]["bench"]["recall_counts"]
    assert counts["completed"] == len(FIXTURE_ROWS)
    assert counts["skipped"] == 0
    assert counts["errored"] == 0
    assert counts["completed_last_invocation"] == len(FIXTURE_ROWS)


def test_qa_artefacts_are_label_scoped_too(monkeypatch, stubbed_run):
    # QA answers are derived from one label's recall rows and resume by
    # question id — the same cross-label leak applies.
    work, _ = stubbed_run
    captured = {}

    class FakeQAStats:
        answered = 0
        skipped = 0
        judge_errors = 0

    async def fake_run_qa(rows, recall_path, out_path, **kwargs):
        captured["recall_path"] = recall_path
        captured["out_path"] = out_path
        return FakeQAStats()

    monkeypatch.setattr(runmod.qa, "run_qa", fake_run_qa)
    assert runmod.run(["--config", "bench", "--run-label", "pre-dream"]) == 0
    label_dir = run_work_dir("pre-dream", work_dir=work)
    assert captured["out_path"] == label_dir / "qa_bench.jsonl"
    assert captured["recall_path"] == label_dir / "recall_bench.jsonl"


def test_legacy_unscoped_artefacts_are_reported_and_never_read(stubbed_run, capsys):
    work, calls = stubbed_run
    stale = work / "recall_bench.jsonl"
    stale.write_text(
        json.dumps({"question_id": FIXTURE_ROWS[0]["question_id"], "config": "bench",
                    "hits": [], "context_block": "", "latency_ms": 1, "error": None}) + "\n",
        encoding="utf-8",
    )

    assert runmod.run(["--skip-qa", "--config", "bench", "--run-label", "post-dream"]) == 0

    out = capsys.readouterr().out
    assert "unscoped" in out and "recall_bench.jsonl" in out
    # Reported, never adopted: the question in the stale file was recalled again.
    assert len(calls) == len(FIXTURE_ROWS)
    # Reported, never moved or deleted — another run may hold it open.
    assert stale.exists()


def test_load_recall_counts_degrades_on_malformed_file(tmp_path):
    (tmp_path / "recall_counts.json").write_text("not json", encoding="utf-8")
    assert report.load_recall_counts(tmp_path) == {}
    (tmp_path / "recall_counts.json").write_text('["a list"]', encoding="utf-8")
    assert report.load_recall_counts(tmp_path) == {}


def test_record_recall_counts_ignores_corrupt_previous_totals(tmp_path):
    (tmp_path / "recall_counts.json").write_text(
        json.dumps({"bench": {"completed": "many", "errored": None, "invocations": True}}),
        encoding="utf-8",
    )

    class Stats:
        completed = 3
        skipped = 1
        errored = 2

    entry = runmod._record_recall_counts(tmp_path, "bench", Stats())
    assert entry == {"completed": 3, "errored": 2, "skipped": 1, "invocations": 1,
                     "completed_last_invocation": 3}


# --- finding 1: the positive control must survive a same-label second leg ----

def test_a_same_label_no_op_rerun_cannot_certify_the_store(stubbed_run):
    """The reachable sequence, with the SHIPPED defaults: run the leg, enable
    dreaming, run the identical command again. The second invocation recalls
    nothing — but the label's cumulative `completed` still reports the first
    invocation's work, so a gate reading that figure certifies a no-op."""
    work, calls = stubbed_run

    assert runmod.run(["--skip-qa", "--config", "bench", "--run-label", "pre-dream"]) == 0
    before = _record(work, "pre-dream")
    assert len(calls) == len(FIXTURE_ROWS)

    assert runmod.run(["--skip-qa", "--config", "bench", "--run-label", "pre-dream"]) == 0
    assert len(calls) == len(FIXTURE_ROWS), "the re-run recalled nothing — that is the point"

    counts = _record(work, "pre-dream")["retrieval"]["bench"]["recall_counts"]
    # Cumulative provenance is kept — and is exactly what must not gate.
    assert counts["completed"] == len(FIXTURE_ROWS)
    assert counts["invocations"] == 2
    assert counts["completed_last_invocation"] == 0

    out = dream_ab.compare_runs(
        before["retrieval"]["bench"],
        _record(work, "pre-dream")["retrieval"]["bench"],
    )
    assert out["regressed"] is True
    assert "completed=0" in out["verdict"]


def test_an_interrupted_leg_resumed_under_the_same_label_still_passes(stubbed_run):
    """The case the accumulation exists to protect: a leg interrupted partway
    finishes its remaining questions in a second invocation. That invocation
    completed > 0, so it is evidence and must clear the gate."""
    work, calls = stubbed_run
    label_dir = run_work_dir("pre-dream", work_dir=work)
    label_dir.mkdir(parents=True)
    # Pretend a first invocation died after the first question.
    (label_dir / "recall_bench.jsonl").write_text(
        json.dumps({"question_id": FIXTURE_ROWS[0]["question_id"], "config": "bench",
                    "hits": [], "context_block": "", "latency_ms": 1, "error": None}) + "\n",
        encoding="utf-8",
    )
    (label_dir / "recall_counts.json").write_text(
        json.dumps({"bench": {"completed": 1, "errored": 0, "skipped": 0,
                              "invocations": 1, "completed_last_invocation": 1}}),
        encoding="utf-8",
    )

    assert runmod.run(["--skip-qa", "--config", "bench", "--run-label", "pre-dream"]) == 0
    counts = _record(work, "pre-dream")["retrieval"]["bench"]["recall_counts"]
    assert counts["completed_last_invocation"] == len(FIXTURE_ROWS) - 1
    assert counts["skipped"] == 1

    out = dream_ab.compare_runs(
        {"k": 10, "overall": {"recall_at_k": 0.5, "coverage_at_k": 0.5, "ndcg_at_k": 0.5}},
        _record(work, "pre-dream")["retrieval"]["bench"],
    )
    assert out["regressed"] is False
    assert out["warnings"] == []


# --- finding 3: a path-hostile label must not kill a finished run -----------

@pytest.mark.parametrize("hostile", ["a/b", "..", "../../etc"])
def test_a_path_hostile_label_still_produces_a_report(monkeypatch, tmp_path, hostile):
    """`--run-label a/b` used to run preflight, ingest, the whole recall stage
    and scoring — hours — and then die building the results filename. The
    cheapest possible failure at the most expensive possible moment."""
    monkeypatch.setattr(
        runmod.report, "_load_meta",
        lambda *a, **kw: {"dataset": {}, "cortex_version": {}, "models": {}, "configs": {}},
    )
    monkeypatch.setattr(runmod, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(runmod, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(runmod.report, "WORK_DIR", tmp_path / "work")

    runmod._assemble_report(hostile, "http://c", "http://o", "reader")

    written = list((tmp_path / "results").iterdir())
    assert len(written) == 2  # the run record + METHODOLOGY.md
    record = next(p for p in written if p.suffix == ".json")
    assert record.parent == tmp_path / "results"  # never escaped into a subdir
    assert record.name.endswith(f"-{sanitize_label(hostile)}.json")
