import os
import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own ~/.firekeep: paths.py resolves through the kit's
    resolver, which honours FIREKEEP_CONFIG."""
    cfg = tmp_path / "firekeep" / "config"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("[server]\nurl = http://127.0.0.1:1\napi_key = test\n", encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    monkeypatch.setenv("FIREKEEP_HANDS_OFFLINE", "1")
    yield tmp_path / "firekeep"
