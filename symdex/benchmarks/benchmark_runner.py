"""Main benchmark runner: FirekeepSymdex vs Raw Claude on Click."""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load .env if present (no extra dependency needed)
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

from anthropic import Anthropic

from benchmarks.context_builders import build_symdex_context, build_raw_context, count_tokens
from benchmarks.judge import judge_answers

client = Anthropic()
QUESTIONS_FILE = Path(__file__).parent / "questions.json"
RESULTS_DIR = Path(__file__).parent / "results"
ANSWER_MODEL = "claude-sonnet-4-20250514"
NUM_RUNS = 3

RAW_ANSWER_PROMPT = """You are answering a question about the Click library (Python CLI framework).
Use ONLY the provided context to answer. Be specific — reference exact class names, function names, and file locations.

Context:
{context}

Question: {question}

Answer concisely and accurately."""

SYMDEX_ANSWER_PROMPT = """You are answering a question about the Click library (Python CLI framework).
Use ONLY the provided context to answer. Be specific — reference exact class names, function names, and file locations.

The context may contain:
- **Key Symbols**: Extracted symbols with full source, signatures, docstrings, and relationship annotations (calls/called-by)
- **Full Source Files**: Complete file contents (for modification questions)
- **File Structure**: Outlines showing all symbols in each file
- **Type Hierarchy**: Class inheritance relationships
- **Pattern Examples**: Existing implementations to follow (for modification questions)
- **Structural Analysis**: Combined intelligence layer with relationships and patterns

Use the provided source code to trace logic and answer precisely.

Context:
{context}

Question: {question}

Answer concisely and accurately."""


def get_answer(context: str, question: str, use_symdex_prompt: bool = False) -> tuple[str, int, float]:
    """Get Claude's answer given context and question.

    Args:
        context: The context string to include
        question: The question to answer
        use_symdex_prompt: If True, use the intelligence-aware prompt

    Returns:
        (answer_text, response_tokens, latency_seconds)
    """
    prompt = SYMDEX_ANSWER_PROMPT if use_symdex_prompt else RAW_ANSWER_PROMPT
    start = time.time()
    response = client.messages.create(
        model=ANSWER_MODEL,
        max_tokens=1024,
        temperature=0.0,
        messages=[{
            "role": "user",
            "content": prompt.format(context=context, question=question),
        }],
    )
    latency = time.time() - start
    answer = response.content[0].text
    resp_tokens = count_tokens(answer)
    return answer, resp_tokens, latency


def run_single_question(q: dict, repo: str, run_id: int) -> dict:
    """Run one question through both paths."""
    qid = q["id"]
    print(f"  Q{qid} (run {run_id}): {q['question'][:60]}...")

    # Build contexts
    t0 = time.time()
    symdex_ctx, symdex_ctx_tokens = build_symdex_context(q, repo)
    symdex_ctx_time = time.time() - t0

    t0 = time.time()
    raw_ctx, raw_ctx_tokens = build_raw_context(q, repo)
    raw_ctx_time = time.time() - t0

    # Get answers (symdex uses intelligence-aware prompt)
    symdex_answer, symdex_resp_tokens, symdex_latency = get_answer(
        symdex_ctx, q["question"], use_symdex_prompt=True)
    raw_answer, raw_resp_tokens, raw_latency = get_answer(
        raw_ctx, q["question"], use_symdex_prompt=False)

    # Judge
    scores = judge_answers(
        question=q["question"],
        answer_key=q["answer_key"],
        symdex_answer=symdex_answer,
        raw_answer=raw_answer,
    )

    token_savings = (1 - symdex_ctx_tokens / raw_ctx_tokens) * 100 if raw_ctx_tokens else 0

    return {
        "question_id": qid,
        "run_id": run_id,
        "category": q["category"],
        "question": q["question"],
        "symdex": {
            "context_tokens": symdex_ctx_tokens,
            "response_tokens": symdex_resp_tokens,
            "context_build_time": round(symdex_ctx_time, 3),
            "answer_latency": round(symdex_latency, 3),
            "scores": scores["symdex"],
            "answer": symdex_answer,
        },
        "raw": {
            "context_tokens": raw_ctx_tokens,
            "response_tokens": raw_resp_tokens,
            "context_build_time": round(raw_ctx_time, 3),
            "answer_latency": round(raw_latency, 3),
            "scores": scores["raw"],
            "answer": raw_answer,
        },
        "token_savings_pct": round(token_savings, 1),
        "judge_order": scores["order"],
    }


def aggregate_results(results: list[dict]) -> dict:
    """Compute aggregate statistics."""
    symdex_scores = {"accuracy": [], "completeness": [], "relevance": []}
    raw_scores = {"accuracy": [], "completeness": [], "relevance": []}
    token_savings = []
    symdex_ctx_tokens = []
    raw_ctx_tokens = []

    for r in results:
        for metric in ["accuracy", "completeness", "relevance"]:
            symdex_scores[metric].append(r["symdex"]["scores"][metric])
            raw_scores[metric].append(r["raw"]["scores"][metric])
        token_savings.append(r["token_savings_pct"])
        symdex_ctx_tokens.append(r["symdex"]["context_tokens"])
        raw_ctx_tokens.append(r["raw"]["context_tokens"])

    def avg(lst):
        return round(sum(lst) / len(lst), 2) if lst else 0

    def stdev(lst):
        if len(lst) < 2:
            return 0
        m = sum(lst) / len(lst)
        return round((sum((x - m) ** 2 for x in lst) / (len(lst) - 1)) ** 0.5, 2)

    # Per-category breakdown
    categories = {}
    for r in results:
        cat = r["category"]
        if cat not in categories:
            categories[cat] = {"symdex_acc": [], "raw_acc": [], "savings": []}
        categories[cat]["symdex_acc"].append(r["symdex"]["scores"]["accuracy"])
        categories[cat]["raw_acc"].append(r["raw"]["scores"]["accuracy"])
        categories[cat]["savings"].append(r["token_savings_pct"])

    cat_summary = {}
    for cat, data in categories.items():
        cat_summary[cat] = {
            "symdex_accuracy_avg": avg(data["symdex_acc"]),
            "raw_accuracy_avg": avg(data["raw_acc"]),
            "token_savings_avg": avg(data["savings"]),
            "count": len(data["symdex_acc"]),
        }

    # Win/loss/tie
    wins = sum(1 for r in results if r["symdex"]["scores"]["accuracy"] > r["raw"]["scores"]["accuracy"])
    losses = sum(1 for r in results if r["symdex"]["scores"]["accuracy"] < r["raw"]["scores"]["accuracy"])
    ties = len(results) - wins - losses

    return {
        "total_questions": len(set(r["question_id"] for r in results)),
        "total_runs": len(results),
        "symdex": {
            "accuracy_avg": avg(symdex_scores["accuracy"]),
            "accuracy_stdev": stdev(symdex_scores["accuracy"]),
            "completeness_avg": avg(symdex_scores["completeness"]),
            "relevance_avg": avg(symdex_scores["relevance"]),
            "avg_context_tokens": avg(symdex_ctx_tokens),
        },
        "raw": {
            "accuracy_avg": avg(raw_scores["accuracy"]),
            "accuracy_stdev": stdev(raw_scores["accuracy"]),
            "completeness_avg": avg(raw_scores["completeness"]),
            "relevance_avg": avg(raw_scores["relevance"]),
            "avg_context_tokens": avg(raw_ctx_tokens),
        },
        "token_savings_avg": avg(token_savings),
        "token_savings_stdev": stdev(token_savings),
        "win_loss_tie": {"wins": wins, "losses": losses, "ties": ties},
        "by_category": cat_summary,
    }


def print_report(summary: dict):
    """Print a formatted summary report."""
    print("\n" + "=" * 70)
    print("  BENCHMARK RESULTS: FirekeepSymdex vs Raw Claude on Click")
    print("=" * 70)

    n_questions = summary["total_questions"]
    n_runs = summary["total_runs"] // n_questions if n_questions else 0
    print(f"\n  Questions: {n_questions}  |  Runs per question: {n_runs}")

    print(f"\n  {'Metric':<25} {'FirekeepSymdex':>12} {'Raw Files':>12} {'Delta':>10}")
    print(f"  {'-'*25} {'-'*12} {'-'*12} {'-'*10}")

    s, r = summary["symdex"], summary["raw"]
    for metric in ["accuracy", "completeness", "relevance"]:
        sv = s[f"{metric}_avg"]
        rv = r[f"{metric}_avg"]
        delta = sv - rv
        sign = "+" if delta > 0 else ""
        print(f"  {metric.capitalize():<25} {sv:>12.2f} {rv:>12.2f} {sign + str(round(delta, 2)):>10}")

    print(f"  {'Avg context tokens':<25} {s['avg_context_tokens']:>12.0f} {r['avg_context_tokens']:>12.0f} {summary['token_savings_avg']:>9.1f}%")

    wlt = summary["win_loss_tie"]
    print(f"\n  Win/Loss/Tie (accuracy): {wlt['wins']}W / {wlt['losses']}L / {wlt['ties']}T")

    print("\n  By Category:")
    for cat, data in summary["by_category"].items():
        print(f"    {cat}: symdex={data['symdex_accuracy_avg']:.2f} vs raw={data['raw_accuracy_avg']:.2f} (savings: {data['token_savings_avg']:.1f}%)")

    print("\n" + "=" * 70)


def main():
    RESULTS_DIR.mkdir(exist_ok=True)

    with open(QUESTIONS_FILE) as f:
        questions = json.load(f)

    repo = "local/click"
    all_results = []

    print(f"Running benchmark: {len(questions)} questions x {NUM_RUNS} runs")
    print(f"Model: {ANSWER_MODEL}")
    print()

    for run_id in range(1, NUM_RUNS + 1):
        print(f"--- Run {run_id}/{NUM_RUNS} ---")
        for q in questions:
            try:
                result = run_single_question(q, repo, run_id)
                all_results.append(result)
            except Exception as e:
                print(f"    ERROR Q{q['id']} run {run_id}: {e}")
                continue

    # Save raw results
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_file = RESULTS_DIR / f"benchmark_{timestamp}.json"
    with open(results_file, "w") as f:
        json.dump({"results": all_results, "summary": aggregate_results(all_results)}, f, indent=2)

    print(f"\nResults saved to {results_file}")

    # Print summary
    summary = aggregate_results(all_results)
    print_report(summary)


if __name__ == "__main__":
    main()
