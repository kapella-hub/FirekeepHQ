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
- accuracy: correctness of facts, code references, and technical claims. Answers citing exact file names and line numbers should be rewarded if the citations are correct.
- completeness: coverage of all key points in the ground truth
- relevance: does the answer directly address the question? Specific details like file paths, line numbers, and code references are RELEVANT when they support the answer — do not penalize precision. Only penalize truly unrelated tangents.

Important: Score each answer independently against the ground truth. Do not compare them to each other.

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
        max_tokens=512,
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
    try:
        scores = json.loads(text)
    except json.JSONDecodeError:
        # Fallback: neutral scores if judge output is unparseable
        scores = {
            "a": {"accuracy": 3, "completeness": 3, "relevance": 3},
            "b": {"accuracy": 3, "completeness": 3, "relevance": 3},
        }

    # Validate expected keys
    default = {"accuracy": 3, "completeness": 3, "relevance": 3}
    for key in ("a", "b"):
        if key not in scores or not isinstance(scores[key], dict):
            scores[key] = default.copy()
        for metric in ("accuracy", "completeness", "relevance"):
            if metric not in scores[key]:
                scores[key][metric] = 3

    if symdex_first:
        return {"symdex": scores["a"], "raw": scores["b"], "order": "symdex_first"}
    else:
        return {"symdex": scores["b"], "raw": scores["a"], "order": "raw_first"}
