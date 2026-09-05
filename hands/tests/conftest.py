import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own ~/.firekeep: paths.py resolves through the kit's
    resolver, which honours FIREKEEP_CONFIG."""
    cfg = tmp_path / "firekeep" / "config"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("[server]\nurl = http://127.0.0.1:1\napi_key = test\n", encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    # hooklog resolves its own path from FIREKEEP_LOG_DIR, NOT from
    # FIREKEEP_CONFIG — without this every `log_failure` a test provokes
    # appends to the developer's real `~/.firekeep/logs/hooks.log`. Found by
    # reading that file to confirm a shutdown log line and finding the suite's
    # own noise in it.
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(tmp_path / "firekeep" / "logs"))
    monkeypatch.setenv("FIREKEEP_HANDS_OFFLINE", "1")
    yield tmp_path / "firekeep"
