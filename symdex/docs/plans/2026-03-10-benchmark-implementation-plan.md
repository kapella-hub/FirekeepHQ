# Benchmark: FirekeepSymdex vs Raw Claude — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build an automated benchmark that compares Claude's accuracy when given FirekeepSymdex context vs raw file context, measured by LLM-as-judge on 20 questions about the Click library.

**Architecture:** Clone Click, index it with FirekeepSymdex, build context two ways per question (symdex tools vs raw files), send both to Claude Sonnet for answers, score with LLM-as-judge, aggregate into a report. All orchestrated by a Python benchmark runner.

**Tech Stack:** Python, anthropic SDK, FirekeepSymdex (direct imports), tiktoken (token counting), Click repo (target)

---

### Task 1: Set Up Benchmark Directory and Dependencies

**Files:**
- Create: `benchmarks/__init__.py` (empty)
- Modify: `pyproject.toml` (add benchmark optional deps)

**Step 1: Create benchmarks directory**

```bash
mkdir -p benchmarks/results
```

**Step 2: Add benchmark dependencies to pyproject.toml**

In `pyproject.toml`, add a `benchmark` optional dependency group:

```toml
[project.optional-dependencies]
benchmark = ["anthropic>=0.40.0", "tiktoken>=0.7.0"]
```

This goes alongside the existing `test` and `ai` groups.

**Step 3: Create empty __init__.py**

Create `benchmarks/__init__.py` as an empty file.

**Step 4: Install benchmark deps**

Run: `uv sync --extra benchmark`
Expected: dependencies install successfully

**Step 5: Commit**

```bash
git add benchmarks/__init__.py pyproject.toml uv.lock
git commit -m "chore: add benchmarks directory and benchmark deps"
```

---

### Task 2: Clone and Index Click

**Files:**
- Create: `benchmarks/setup_click.py`

**Step 1: Write the setup script**

```python
"""Clone Click repo and index it with FirekeepSymdex."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from firekeep_symdex.tools.index_folder import index_folder


CLICK_REPO = "https://github.com/pallets/click.git"
CLICK_DIR = Path(__file__).parent / "repos" / "click"


def clone_click():
    """Clone Click if not already present."""
    if CLICK_DIR.exists():
        print(f"Click already cloned at {CLICK_DIR}")
        return
    CLICK_DIR.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", CLICK_REPO, str(CLICK_DIR)],
        check=True,
    )
    print(f"Cloned Click to {CLICK_DIR}")


def index_click():
    """Index Click with FirekeepSymdex."""
    result = index_folder(
        path=str(CLICK_DIR / "src" / "click"),
        use_ai_summaries=False,
    )
    if "error" in result:
        print(f"Indexing failed: {result['error']}")
        sys.exit(1)
    print(f"Indexed: {result.get('symbols', '?')} symbols, {result.get('files', '?')} files")
    return result


if __name__ == "__main__":
    clone_click()
    index_click()
```

**Step 2: Run the setup**

Run: `.venv/Scripts/python.exe benchmarks/setup_click.py`
Expected: Click cloned and indexed, prints symbol/file counts

**Step 3: Add repos/ to .gitignore**

Append `benchmarks/repos/` to `.gitignore`.

**Step 4: Commit**

```bash
git add benchmarks/setup_click.py .gitignore
git commit -m "feat(bench): add Click clone and index setup script"
```

---

### Task 3: Write the Question Set

**Files:**
- Create: `benchmarks/questions.json`

**Step 1: Study Click's structure**

After indexing, run a quick exploration to understand Click's key files and symbols. Use `get_architecture_map` and `get_file_outline` on key files. This informs writing accurate answer keys.

**Step 2: Write questions.json**

Create `benchmarks/questions.json` with 20 questions. Each entry:

```json
{
  "id": 1,
  "question": "How does Click's parameter type system work? Describe the base class, how custom types are created, and the type resolution flow.",
  "category": "comprehension",
  "answer_key": "Click uses ParamType as the base class in types.py. Custom types subclass ParamType and implement convert(value, param, ctx). Built-in types include STRING, INT, FLOAT, BOOL, UUID, Path, Choice, IntRange, FloatRange, Tuple. Type resolution happens in Parameter.type_cast_value() which calls ParamType.convert(). Failed conversions raise BadParameter.",
  "relevant_files": ["types.py", "core.py"],
  "relevant_symbols": ["ParamType", "Parameter.type_cast_value"],
  "search_hints": ["parameter type", "ParamType", "convert"]
}
```

Categories:
- **Comprehension (7):** Questions about how subsystems work (type system, context, decorators, testing, formatting, shell completion, exception handling)
- **Navigation (7):** Questions asking to find specific functionality (group invocation, option parsing, prompt handling, file path validation, lazy loading, color output, pagination)
- **Modification (6):** Questions about what to change for hypothetical features (async support, custom help formatting, middleware, new parameter type, plugin system, streaming output)

Each question must have:
- Accurate `answer_key` verified against Click source
- `relevant_files` listing the files that contain the answer
- `relevant_symbols` listing key symbols
- `search_hints` listing terms FirekeepSymdex would search for

**Important:** Answer keys must be verified against the actual Click source code after indexing. Do not guess.

**Step 3: Commit**

```bash
git add benchmarks/questions.json
git commit -m "feat(bench): add 20 benchmark questions with answer keys"
```

---

### Task 4: Build Context Builders

**Files:**
- Create: `benchmarks/context_builders.py`
- Test: `benchmarks/test_context_builders.py`

**Step 1: Write the failing test**

```python
"""Tests for context_builders."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from benchmarks.context_builders import build_symdex_context, build_raw_context


def test_symdex_context_returns_string_and_token_count():
    question = {
        "search_hints": ["ParamType"],
        "relevant_files": ["types.py"],
    }
    ctx, tokens = build_symdex_context(question, repo="local/click")
    assert isinstance(ctx, str)
    assert len(ctx) > 0
    assert isinstance(tokens, int)
    assert tokens > 0


def test_raw_context_returns_string_and_token_count():
    question = {
        "relevant_files": ["types.py"],
    }
    ctx, tokens = build_raw_context(question, repo="local/click")
    assert isinstance(ctx, str)
    assert len(ctx) > 0
    assert isinstance(tokens, int)
    assert tokens > 0


def test_symdex_context_smaller_than_raw():
    question = {
        "search_hints": ["ParamType"],
        "relevant_files": ["types.py"],
    }
    symdex_ctx, symdex_tokens = build_symdex_context(question, repo="local/click")
    raw_ctx, raw_tokens = build_raw_context(question, repo="local/click")
    assert symdex_tokens < raw_tokens, "FirekeepSymdex context should be smaller than raw"
```

**Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest benchmarks/test_context_builders.py -v`
Expected: FAIL (module not found)

**Step 3: Write context_builders.py**

```python
"""Build context strings for FirekeepSymdex vs raw file approaches."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import tiktoken

from firekeep_symdex.storage import IndexStore
from firekeep_symdex.tools.search_symbols import search_symbols
from firekeep_symdex.tools.get_symbol import get_symbol
from firekeep_symdex.tools.get_context import get_context
from firekeep_symdex.tools.get_architecture_map import get_architecture_map

_enc = tiktoken.encoding_for_model("gpt-4")  # cl100k_base, close enough for Claude


def count_tokens(text: str) -> int:
    """Count tokens using tiktoken."""
    return len(_enc.encode(text))


def build_symdex_context(question: dict, repo: str) -> tuple[str, int]:
    """Build context using FirekeepSymdex tools.

    Strategy:
    1. Search for symbols using search_hints
    2. Get full source for top matches
    3. Get smart context with focus query
    4. Combine into a single context string

    Returns:
        (context_string, token_count)
    """
    parts = []

    # 1. Architecture overview (cheap, gives file-level context)
    arch = get_architecture_map(repo=repo)
    if "error" not in arch:
        parts.append("## Architecture Overview")
        for layer in arch.get("layers", []):
            parts.append(f"### {layer['name']}")
            for f in layer.get("files", [])[:5]:
                parts.append(f"- {f['file']}: {f.get('role', '')}")

    # 2. Search for relevant symbols
    seen_ids = set()
    for hint in question.get("search_hints", []):
        results = search_symbols(repo=repo, query=hint, max_results=5)
        for sym in results.get("symbols", []):
            sid = sym["id"]
            if sid not in seen_ids:
                seen_ids.add(sid)
                full = get_symbol(repo=repo, symbol_id=sid)
                if "error" not in full:
                    source = full.get("source", full.get("signature", ""))
                    parts.append(f"## {full.get('name', sid)}")
                    parts.append(f"File: {full.get('file', '?')}")
                    parts.append(f"```python\n{source}\n```")

    # 3. Smart context for broader understanding
    focus = question.get("search_hints", [""])[0]
    if focus:
        ctx = get_context(repo=repo, budget_tokens=4000, focus=focus, include_deps=True)
        if "error" not in ctx:
            for sym in ctx.get("symbols", []):
                sid = sym.get("id", "")
                if sid and sid not in seen_ids:
                    seen_ids.add(sid)
                    parts.append(f"## {sym.get('name', sid)}")
                    parts.append(f"```python\n{sym.get('source', sym.get('signature', ''))}\n```")

    context = "\n\n".join(parts)
    return context, count_tokens(context)


def build_raw_context(question: dict, repo: str) -> tuple[str, int]:
    """Build context by reading full raw files.

    Reads all files listed in relevant_files from the index store content dir.

    Returns:
        (context_string, token_count)
    """
    parts = repo.split("/")
    owner, name = parts[0], parts[1]
    store = IndexStore()
    index = store.load_index(owner, name)

    if not index:
        return f"Error: repo {repo} not indexed", 0

    context_parts = []
    content_dir = store._content_dir(owner, name)

    for target_file in question.get("relevant_files", []):
        # Find matching source file (relevant_files may be basenames)
        for source_file in index.source_files:
            if source_file.endswith(target_file) or target_file in source_file:
                safe_path = store._safe_content_path(content_dir, source_file)
                if safe_path and safe_path.exists():
                    content = safe_path.read_text(encoding="utf-8", errors="replace")
                    context_parts.append(f"## File: {source_file}")
                    context_parts.append(f"```python\n{content}\n```")
                break

    context = "\n\n".join(context_parts)
    return context, count_tokens(context)
```

**Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest benchmarks/test_context_builders.py -v`
Expected: 3 PASSED

**Step 5: Commit**

```bash
git add benchmarks/context_builders.py benchmarks/test_context_builders.py
git commit -m "feat(bench): context builders for symdex vs raw file comparison"
```

---

### Task 5: Build the LLM Judge

**Files:**
- Create: `benchmarks/judge.py`

**Step 1: Write judge.py**

```python
"""LLM-as-judge scoring for benchmark answers."""

import json
import random
from anthropic import Anthropic

client = Anthropic()

JUDGE_PROMPT = """You are evaluating two answers to a coding question about the Click library.

Question: {question}

Ground Truth (the correct answer):
{answer_key}

Answer A:
{answer_a}

Answer B:
{answer_b}

Score each answer on a 1-5 scale for:
- accuracy: correctness of facts, code references, and technical claims
- completeness: coverage of all key points in the ground truth
- relevance: focus on what was asked, without unnecessary tangents or filler

Return ONLY valid JSON (no markdown):
{{"a": {{"accuracy": N, "completeness": N, "relevance": N}}, "b": {{"accuracy": N, "completeness": N, "relevance": N}}}}"""


def judge_answers(
    question: str,
    answer_key: str,
    symdex_answer: str,
    raw_answer: str,
    model: str = "claude-sonnet-4-20250514",
) -> dict:
    """Score both answers using LLM-as-judge.

    Randomizes A/B assignment to avoid position bias.

    Returns:
        {"symdex": {"accuracy": N, "completeness": N, "relevance": N},
         "raw": {"accuracy": N, "completeness": N, "relevance": N},
         "order": "symdex_first" | "raw_first"}
    """
    # Randomize order to prevent position bias
    symdex_first = random.random() < 0.5
    if symdex_first:
        answer_a, answer_b = symdex_answer, raw_answer
    else:
        answer_a, answer_b = raw_answer, symdex_answer

    response = client.messages.create(
        model=model,
        max_tokens=256,
        temperature=0.0,
        messages=[{
            "role": "user",
            "content": JUDGE_PROMPT.format(
                question=question,
                answer_key=answer_key,
                answer_a=answer_a,
                answer_b=answer_b,
            ),
        }],
    )

    text = response.content[0].text.strip()
    scores = json.loads(text)

    if symdex_first:
        return {"symdex": scores["a"], "raw": scores["b"], "order": "symdex_first"}
    else:
        return {"symdex": scores["b"], "raw": scores["a"], "order": "raw_first"}
```

**Step 2: Commit**

```bash
git add benchmarks/judge.py
git commit -m "feat(bench): LLM-as-judge scoring with position bias randomization"
```

---

### Task 6: Build the Benchmark Runner

**Files:**
- Create: `benchmarks/benchmark_runner.py`

**Step 1: Write benchmark_runner.py**

```python
"""Main benchmark runner: FirekeepSymdex vs Raw Claude on Click."""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from anthropic import Anthropic
import tiktoken

from benchmarks.context_builders import build_symdex_context, build_raw_context, count_tokens
from benchmarks.judge import judge_answers

client = Anthropic()
QUESTIONS_FILE = Path(__file__).parent / "questions.json"
RESULTS_DIR = Path(__file__).parent / "results"
ANSWER_MODEL = "claude-sonnet-4-20250514"
NUM_RUNS = 3

ANSWER_PROMPT = """You are answering a question about the Click library (Python CLI framework).
Use ONLY the provided context to answer. Be specific — reference exact class names, function names, and file locations.

Context:
{context}

Question: {question}

Answer concisely and accurately."""


def get_answer(context: str, question: str) -> tuple[str, int, float]:
    """Get Claude's answer given context and question.

    Returns:
        (answer_text, response_tokens, latency_seconds)
    """
    start = time.time()
    response = client.messages.create(
        model=ANSWER_MODEL,
        max_tokens=1024,
        temperature=0.0,
        messages=[{
            "role": "user",
            "content": ANSWER_PROMPT.format(context=context, question=question),
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

    # Get answers
    symdex_answer, symdex_resp_tokens, symdex_latency = get_answer(symdex_ctx, q["question"])
    raw_answer, raw_resp_tokens, raw_latency = get_answer(raw_ctx, q["question"])

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

    print(f"\n  Questions: {summary['total_questions']}  |  Runs per question: {summary['total_runs'] // summary['total_questions']}")

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

    print(f"\n  By Category:")
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
            result = run_single_question(q, repo, run_id)
            all_results.append(result)

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
```

**Step 2: Commit**

```bash
git add benchmarks/benchmark_runner.py
git commit -m "feat(bench): main benchmark runner with aggregation and reporting"
```

---

### Task 7: Write Questions (Requires Click Index)

**Files:**
- Create: `benchmarks/questions.json`

**Depends on:** Task 2 (Click must be cloned and indexed)

**Step 1: Explore Click's structure**

Run these FirekeepSymdex commands against the Click index to understand the codebase:

```python
from firekeep_symdex.tools.get_architecture_map import get_architecture_map
from firekeep_symdex.tools.get_file_outline import get_file_outline

arch = get_architecture_map(repo="local/click")
# Review output to understand Click's layers and key files

outline = get_file_outline(repo="local/click", file_path="core.py")
# Review to see key classes: BaseCommand, Command, MultiCommand, Group, Context, Parameter, Option, Argument
```

**Step 2: Write 20 questions with verified answer keys**

Write questions that test real knowledge of Click internals. Each answer key must be verified against actual source code. Use `get_symbol` to check specific implementations.

Structure the JSON as an array of objects matching the format from the design doc.

**Step 3: Validate questions**

Run a quick smoke test: for each question, verify `search_hints` return relevant results:

```python
for q in questions:
    for hint in q["search_hints"]:
        results = search_symbols(repo="local/click", query=hint)
        assert len(results.get("symbols", [])) > 0, f"No results for hint '{hint}' in Q{q['id']}"
```

**Step 4: Commit**

```bash
git add benchmarks/questions.json
git commit -m "feat(bench): 20 verified benchmark questions for Click"
```

---

### Task 8: End-to-End Smoke Test

**Files:**
- Create: `benchmarks/test_smoke.py`

**Step 1: Write smoke test**

```python
"""Smoke test: run 1 question, 1 run to verify the full pipeline."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

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

    assert symdex_tokens > 0
    assert raw_tokens > 0
    assert symdex_tokens < raw_tokens

    # Get answers (requires ANTHROPIC_API_KEY)
    symdex_answer, _, _ = get_answer(symdex_ctx, q["question"])
    raw_answer, _, _ = get_answer(raw_ctx, q["question"])

    assert len(symdex_answer) > 50
    assert len(raw_answer) > 50

    # Judge
    scores = judge_answers(q["question"], q["answer_key"], symdex_answer, raw_answer)
    assert "symdex" in scores
    assert "raw" in scores
    assert 1 <= scores["symdex"]["accuracy"] <= 5

    print(f"Smoke test passed!")
    print(f"  Symdex: {symdex_tokens} tokens -> accuracy={scores['symdex']['accuracy']}")
    print(f"  Raw:    {raw_tokens} tokens -> accuracy={scores['raw']['accuracy']}")
    print(f"  Savings: {(1 - symdex_tokens/raw_tokens)*100:.1f}%")


if __name__ == "__main__":
    test_smoke()
```

**Step 2: Run smoke test**

Run: `ANTHROPIC_API_KEY=<key> .venv/Scripts/python.exe benchmarks/test_smoke.py`
Expected: Smoke test passes, prints scores for one question

**Step 3: Commit**

```bash
git add benchmarks/test_smoke.py
git commit -m "feat(bench): end-to-end smoke test for benchmark pipeline"
```

---

### Task 9: Run Full Benchmark and Save Results

**Step 1: Run the benchmark**

Run: `ANTHROPIC_API_KEY=<key> .venv/Scripts/python.exe benchmarks/benchmark_runner.py`
Expected: ~180 API calls, takes 5-15 minutes, prints summary report

**Step 2: Review results**

Check `benchmarks/results/benchmark_<timestamp>.json` for the full report. Verify:
- All 20 questions completed across 3 runs
- Token savings are > 70%
- Scores are reasonable (no 0s or obviously broken judging)

**Step 3: Commit results**

```bash
git add benchmarks/results/
git commit -m "feat(bench): initial benchmark results — FirekeepSymdex vs raw Claude on Click"
```

---

### Task 10: Add Benchmark README

**Files:**
- Create: `benchmarks/README.md`

**Step 1: Write README with results summary**

Include:
- What the benchmark measures
- How to run it (`setup_click.py` then `benchmark_runner.py`)
- Prerequisites (ANTHROPIC_API_KEY, uv sync --extra benchmark)
- Results table from latest run
- How to interpret scores

**Step 2: Commit**

```bash
git add benchmarks/README.md
git commit -m "docs: add benchmark README with results summary"
```
