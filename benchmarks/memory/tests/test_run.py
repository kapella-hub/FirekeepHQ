import json

from bench import run as runmod


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

    (tmp_path / "ingest_errors.json").write_text(json.dumps(["lm_q1/s_a: boom"]), encoding="utf-8")
    (tmp_path / "recall_bench.jsonl").write_text(
        json.dumps({"question_id": "q1", "hits": [{"session_id": "s_a"}], "error": None}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "scores_bench.json").write_text(
        json.dumps({"k": 10, "overall": {"n": 1, "recall_at_k": 1, "coverage_at_k": 1,
                                          "mrr": 1, "ndcg_at_k": 1},
                    "by_question_type": {}, "errored_questions": [], "missing_questions": [],
                    "abstention_excluded": 0}),
        encoding="utf-8",
    )

    runmod._assemble_report("test-label", "http://c", "http://o", "my-reader-model")

    out_files = list(tmp_path.glob("*-test-label.json"))
    assert len(out_files) == 1
    result = json.loads(out_files[0].read_text(encoding="utf-8"))
    assert result["ingest_errors"] == 1
    assert result["ingest_error_keys"] == ["lm_q1/s_a: boom"]
    assert result["per_question"]["bench"][0]["question_id"] == "q1"
