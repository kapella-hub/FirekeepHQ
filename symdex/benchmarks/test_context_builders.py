"""Tests for benchmark context builders."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


from benchmarks.context_builders import (
    build_symdex_context,
    build_raw_context,
    count_tokens,
)

SAMPLE_QUESTION = {
    "id": 1,
    "question": "How does Click's parameter type system work?",
    "category": "comprehension",
    "answer_key": "ParamType is the base class for all parameter types.",
    "relevant_files": ["types.py", "core.py"],
    "relevant_symbols": ["ParamType", "Parameter.type_cast_value"],
    "search_hints": ["parameter type", "ParamType", "convert"],
}


class TestCountTokens:
    def test_empty_string(self):
        assert count_tokens("") == 0

    def test_nonempty_string(self):
        tokens = count_tokens("Hello, world!")
        assert tokens > 0

    def test_longer_text_more_tokens(self):
        short = count_tokens("hello")
        long = count_tokens("hello world this is a longer sentence with more tokens")
        assert long > short


class TestBuildSymdexContext:
    def test_returns_nonempty_string(self):
        context, tokens = build_symdex_context(SAMPLE_QUESTION)
        assert isinstance(context, str)
        assert len(context) > 0

    def test_returns_positive_token_count(self):
        context, tokens = build_symdex_context(SAMPLE_QUESTION)
        assert tokens > 0

    def test_token_count_matches_context(self):
        context, tokens = build_symdex_context(SAMPLE_QUESTION)
        assert tokens == count_tokens(context)

    def test_contains_relevant_content(self):
        context, _ = build_symdex_context(SAMPLE_QUESTION)
        # Should contain at least one of the search hints or symbol names
        context_lower = context.lower()
        assert any(
            hint.lower() in context_lower
            for hint in ["ParamType", "convert", "parameter"]
        )


class TestBuildRawContext:
    def test_returns_nonempty_string(self):
        context, tokens = build_raw_context(SAMPLE_QUESTION)
        assert isinstance(context, str)
        assert len(context) > 0

    def test_returns_positive_token_count(self):
        context, tokens = build_raw_context(SAMPLE_QUESTION)
        assert tokens > 0

    def test_token_count_matches_context(self):
        context, tokens = build_raw_context(SAMPLE_QUESTION)
        assert tokens == count_tokens(context)

    def test_contains_file_names(self):
        context, _ = build_raw_context(SAMPLE_QUESTION)
        assert "types.py" in context
        assert "core.py" in context


class TestSymdexVsRaw:
    def test_symdex_uses_fewer_tokens_for_comprehension(self):
        """V16: Selective extraction uses fewer tokens for comprehension."""
        ctx, symdex_tokens = build_symdex_context(SAMPLE_QUESTION)
        _, raw_tokens = build_raw_context(SAMPLE_QUESTION)
        # Comprehension uses selective extraction = fewer tokens
        assert symdex_tokens < raw_tokens

    def test_symdex_contains_source_code(self):
        """V10: Context should contain full source code."""
        ctx, _ = build_symdex_context(SAMPLE_QUESTION)
        assert "ParamType" in ctx
        assert "```python" in ctx

    def test_symdex_contains_symbol_extractions(self):
        """V15: Context should contain symbol extractions with source."""
        ctx, _ = build_symdex_context(SAMPLE_QUESTION)
        assert "```python" in ctx
        assert "###" in ctx  # Symbol headers
