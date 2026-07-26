# Benchmark Design: FirekeepSymdex vs Raw Claude on Click

**Date**: 2026-03-10
**Status**: Approved

## Goal

Prove FirekeepSymdex's value by running a head-to-head comparison against raw file reading on the Click library (~10K lines Python). Measure accuracy, completeness, token savings, and cost.

## Approach

**Approach B: Simulated Benchmark** — call FirekeepSymdex tools directly via Python imports (no MCP), compare against raw file reads, send both contexts to Claude for answers, score with LLM-as-judge.

## Architecture

```
benchmarks/
├── benchmark_runner.py      # Main orchestrator
├── questions.json           # 20 questions with answer keys
├── judge.py                 # LLM-as-judge scoring
├── context_builders.py      # FirekeepSymdex vs raw file context assembly
├── results/                 # JSON reports per run
└── README.md                # How to run, interpret results
```

## Flow

Per question:
1. `context_builders.py` builds two context strings:
   - **FirekeepSymdex path**: calls `search_symbols`, `get_symbol`, `get_context`, `get_architecture_map`, etc. directly via Python imports against the Click index
   - **Raw path**: reads the full file(s) that contain the answer
2. `benchmark_runner.py` sends `context + question` to Claude API (Sonnet) for both paths
3. `judge.py` sends `question + answer_key + both_answers` to Claude, scores each 1-5 on accuracy, completeness, relevance
4. Token counts recorded for both context and response

## Question Categories (20 total)

| Category | Count | Examples |
|----------|-------|---------|
| Comprehension | 7 | "How does Click's parameter type system work?" |
| Navigation | 7 | "What function handles command group invocation?" |
| Modification | 6 | "What would you change to add async command support?" |

## Answer Key Format

```json
{
  "question": "How does Click resolve parameter types?",
  "category": "comprehension",
  "answer_key": "Click uses a ParamType base class in types.py...",
  "relevant_files": ["src/click/types.py", "src/click/core.py"],
  "relevant_symbols": ["ParamType", "Parameter.type_cast_value"]
}
```

## Metrics

| Metric | How Measured |
|--------|-------------|
| Accuracy (1-5) | LLM judge scores answer correctness |
| Completeness (1-5) | LLM judge scores coverage of key points |
| Context tokens | tiktoken count of context sent to Claude |
| Response tokens | tiktoken count of Claude's answer |
| Token savings % | `1 - (symdex_tokens / raw_tokens)` |
| Cost estimate | Based on Sonnet pricing |
| Latency | Wall-clock time per question |

## Judge Prompt

```
You are evaluating two answers to a coding question.

Question: {question}
Ground Truth: {answer_key}

Answer A: {answer_a}
Answer B: {answer_b}

Score each answer 1-5 on:
- accuracy: correctness of facts and code references
- completeness: coverage of all key points in the ground truth
- relevance: focus on what was asked without unnecessary tangents

Return JSON: {"a": {"accuracy": N, "completeness": N, "relevance": N}, "b": {...}}
```

Note: answers are presented without labels to avoid bias. Assignment of FirekeepSymdex vs Raw to A/B is randomized per question.

## Run Configuration

- **Target repo**: Click (~10K lines Python)
- **Model**: Claude Sonnet (answers + judging)
- **Runs**: 3 per question (variance measurement)
- **Temperature**: 0.0 for both answers and judging
- **API calls**: ~180 total (120 answer + 60 judge)
- **Estimated cost**: ~$5-8

## Success Criteria

- FirekeepSymdex achieves higher average accuracy/completeness scores
- Token savings >= 70% vs raw file reading
- Results reproducible across 3 runs (low variance)
