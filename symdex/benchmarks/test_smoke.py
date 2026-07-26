"""Smoke test: run 1 question through the full pipeline."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent))

from benchmarks.context_builders import build_symdex_context, build_raw_context
from benchmarks.judge import judge_answers
from benchmarks.benchmark_runner import get_answer


def test_smoke():
    """Run one question through the full pipeline."""
    with open(Path(__file__).parent / "questions.json") as f:
        questions = json.load(f)

    q = questions[0]
    repo = "local/click"

    # Build contexts
    symdex_ctx, symdex_tokens = build_symdex_context(q, repo)
    raw_ctx, raw_tokens = build_raw_context(q, repo)

    print(f"\nContext tokens — symdex: {symdex_tokens}, raw: {raw_tokens}")
    assert symdex_tokens > 0, "Symdex context should have tokens"
    assert raw_tokens > 0, "Raw context should have tokens"
    assert symdex_tokens < raw_tokens, f"Symdex ({symdex_tokens}) should be smaller than raw ({raw_tokens})"

    # Get answers (requires ANTHROPIC_API_KEY)
    symdex_answer, symdex_resp_tokens, symdex_latency = get_answer(symdex_ctx, q["question"])
    raw_answer, raw_resp_tokens, raw_latency = get_answer(raw_ctx, q["question"])

    print(f"Symdex answer ({symdex_resp_tokens} tokens, {symdex_latency:.1f}s): {symdex_answer[:100]}...")
    print(f"Raw answer ({raw_resp_tokens} tokens, {raw_latency:.1f}s): {raw_answer[:100]}...")

    assert len(symdex_answer) > 50, "Symdex answer too short"
    assert len(raw_answer) > 50, "Raw answer too short"

    # Judge
    scores = judge_answers(q["question"], q["answer_key"], symdex_answer, raw_answer)

    print("\nScores:")
    print(f"  Symdex: accuracy={scores['symdex']['accuracy']}, completeness={scores['symdex']['completeness']}, relevance={scores['symdex']['relevance']}")
    print(f"  Raw:    accuracy={scores['raw']['accuracy']}, completeness={scores['raw']['completeness']}, relevance={scores['raw']['relevance']}")
    print(f"  Order: {scores['order']}")
    print(f"  Token savings: {(1 - symdex_tokens / raw_tokens) * 100:.1f}%")

    assert "symdex" in scores
    assert "raw" in scores
    for path in ["symdex", "raw"]:
        for metric in ["accuracy", "completeness", "relevance"]:
            assert 1 <= scores[path][metric] <= 5, f"{path}.{metric} out of range: {scores[path][metric]}"

    print("\nSmoke test PASSED!")


if __name__ == "__main__":
    test_smoke()
