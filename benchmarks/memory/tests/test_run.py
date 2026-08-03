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
