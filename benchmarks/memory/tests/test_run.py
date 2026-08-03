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
