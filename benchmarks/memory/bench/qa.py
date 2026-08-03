"""Local-reader QA + local judge over recalled context. NOT comparable to
published GPT-4o rows — the report labels this row accordingly."""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import httpx
from tqdm import tqdm

from bench.common import DATA_DIR, WORK_DIR, is_abstention, load_dataset

READER_MODEL = "qwen3:14b"
_ABSTAIN_MARKERS = ("don't know", "do not know", "no information")


def refuse_cloud(model: str) -> None:
    if ":cloud" in model:
        raise ValueError(
            f"{model!r} routes to a third-party cloud service — refused "
            "(benchmark is local-only)")


def reader_messages(question: str, question_date: str, context: str) -> list[dict]:
    system = (
        "You answer questions about a user using ONLY the conversation "
        "memory excerpts provided. If the excerpts do not contain the "
        "answer, reply exactly: I don't know."
    )
    user = (
        f"Memory excerpts:\n{context}\n\n"
        f"Today's date: {question_date}\n"
        f"Question: {question}\n"
        "Answer briefly."
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def judge_messages(question: str, gold: str, answer: str) -> list[dict]:
    user = (
        "Judge whether the model answer is factually equivalent to the gold "
        "answer for this question. Minor wording differences are fine.\n"
        f"Question: {question}\nGold answer: {gold}\nModel answer: {answer}\n"
        "Reply with exactly one line: VERDICT: CORRECT or VERDICT: INCORRECT."
    )
    return [{"role": "user", "content": user}]


def parse_verdict(text: str) -> bool | None:
    verdict = None
    for token in text.replace("VERDICT:", "\nVERDICT:").splitlines():
        t = token.strip().upper()
        if t.startswith("VERDICT:"):
            v = t[len("VERDICT:"):].strip()
            if v.startswith("CORRECT"):
                verdict = True
            elif v.startswith("INCORRECT"):
                verdict = False
    return verdict


def score_abstention(answer: str) -> bool:
    low = answer.lower()
    return any(m in low for m in _ABSTAIN_MARKERS)


def _context_from_hits(hits: list[dict]) -> str:
    return "\n---\n".join(h["content"] for h in hits if h.get("content"))


@dataclass
class QAStats:
    answered: int = 0
    skipped: int = 0
    judge_errors: int = 0


async def _chat(client, base_url, model, messages, max_retries=1):
    body = {"model": model, "messages": messages, "stream": False,
            "options": {"temperature": 0}}
    for attempt in range(max_retries + 1):
        try:
            resp = await client.post(f"{base_url}/api/chat", json=body, timeout=600)
            resp.raise_for_status()
            return resp.json()["message"]["content"]
        except Exception:
            if attempt == max_retries:
                raise
            await asyncio.sleep(2)


async def run_qa(rows, recall_path: Path, out_path: Path, *,
                 base_url="http://127.0.0.1:11434", model=READER_MODEL,
                 transport=None, progress=False) -> QAStats:
    refuse_cloud(model)
    stats = QAStats()
    recalls = {}
    for line in recall_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            recalls[rec["question_id"]] = rec
    done = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                done.add(json.loads(line)["question_id"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(transport=transport) as client:
        iterable = tqdm(rows) if progress else rows
        with out_path.open("a", encoding="utf-8") as out:
            for row in iterable:
                qid = row["question_id"]
                rec = recalls.get(qid)
                if qid in done or rec is None or rec.get("error"):
                    stats.skipped += 1
                    continue
                record = {"question_id": qid, "answer": None,
                          "verdict": None, "judge_error": None}
                try:
                    answer = await _chat(client, base_url, model, reader_messages(
                        row["question"], row["question_date"],
                        _context_from_hits(rec["hits"])))
                    record["answer"] = answer
                    if is_abstention(qid):
                        record["verdict"] = score_abstention(answer)
                    else:
                        verdict_text = await _chat(
                            client, base_url, model,
                            judge_messages(row["question"], row["answer"], answer))
                        verdict = parse_verdict(verdict_text)
                        if verdict is None:
                            record["judge_error"] = "unparseable verdict"
                            stats.judge_errors += 1
                        record["verdict"] = verdict
                    stats.answered += 1
                except Exception as exc:
                    record["judge_error"] = str(exc)
                    stats.judge_errors += 1
                out.write(json.dumps(record) + "\n")
                out.flush()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=READER_MODEL)
    ap.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    rows = load_dataset(DATA_DIR / "longmemeval_s.json")
    if args.limit:
        rows = rows[: args.limit]
    stats = asyncio.run(run_qa(
        rows, WORK_DIR / "recall_bench.jsonl", WORK_DIR / "qa_bench.jsonl",
        base_url=args.ollama_url, model=args.model, progress=True))
    print(f"answered={stats.answered} skipped={stats.skipped} "
          f"judge_errors={stats.judge_errors}")


if __name__ == "__main__":
    main()
