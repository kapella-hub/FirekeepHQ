"""ollama-pull must download the models the config actually asks for.

Hardcoding them means EMBEDDING_MODEL is documented as configurable but
silently ignored: cortex is configured for a model that was never pulled,
health checks pass, and every write fails at embedding time.
"""
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"
ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def _ollama_pull_command() -> str:
    text = COMPOSE.read_text(encoding="utf-8")
    start = text.index("  ollama-pull:")
    end = text.index("\n  cortex-api:", start)
    return text[start:end]


def test_pull_command_has_no_hardcoded_model_names():
    block = _ollama_pull_command()
    for hardcoded in ("qwen3:4b", "mxbai-embed-large"):
        assert f"pull {hardcoded}" not in block, (
            f"{hardcoded} is hardcoded; it must come from EMBEDDING_MODEL/LLM_MODEL"
        )


def test_pull_command_interpolates_both_model_vars():
    block = _ollama_pull_command()
    assert "EMBEDDING_MODEL" in block
    assert "LLM_MODEL" in block


def test_pull_command_keeps_working_defaults():
    """Interpolation must carry the current defaults, so an existing .env
    without these keys pulls exactly what it pulls today."""
    block = _ollama_pull_command()
    assert "mxbai-embed-large" in block, "default embedding model must be preserved"
    assert "qwen3:4b" in block, "default generation model must be preserved"


def test_env_example_documents_both_models():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "EMBEDDING_MODEL=" in text
    assert "LLM_MODEL=" in text
