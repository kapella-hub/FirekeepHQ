"""Run benchmark questions through POST /memory/recall for each config row."""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from tqdm import tqdm

from bench.common import DATA_DIR, WORK_DIR, load_dataset, parse_session_tag, sanitize_namespace

CONFIGS: dict[str, dict] = {
    # What a stock install does — the honesty row.
    "defaults": {"top_k": 3, "token_budget": 600, "format": "synthesized"},
    # The comparable row; 10000 is ContextQuery's token_budget cap.
    "bench": {"top_k": 10, "token_budget": 10000, "format": "raw"},
}


def recall_body(row: dict, config: dict) -> dict:
    return {
        "task": row["question"][:2000],
        "namespace": sanitize_namespace(row["question_id"]),
        **config,
    }


def extract_hits(response_json: dict) -> list[dict]:
    hits = []
    for src in response_json.get("sources", []):
        meta = src.get("metadata") or {}
        tags = meta.get("tags") or []
        hits.append({
            "session_id": parse_session_tag(tags),
            "score": src.get("score", 0.0),
            "content": (src.get("content") or "")[:2000],
        })
    return hits


@dataclass
class RecallStats:
    completed: int = 0
    skipped: int = 0
    errored: int = 0


def _already_done(out_path: Path) -> set[str]:
    if not out_path.exists():
        return set()
    done = set()
    for line in out_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            done.add(json.loads(line)["question_id"])
    return done


async def run_recall(rows, base_url, config_name, out_path: Path, *,
                     transport=None, max_retries=3, progress=False) -> RecallStats:
    stats = RecallStats()
    config = CONFIGS[config_name]
    done = _already_done(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"{base_url}/memory/recall"

    async with httpx.AsyncClient(transport=transport) as client:
        iterable = tqdm(rows) if progress else rows
        with out_path.open("a", encoding="utf-8") as out:
            for row in iterable:
                qid = row["question_id"]
                if qid in done:
                    stats.skipped += 1
                    continue
                record = {"question_id": qid, "config": config_name,
                          "hits": [], "context_block": "", "latency_ms": None,
                          "error": None}
                body = recall_body(row, config)
                start = time.perf_counter()
                for attempt in range(max_retries + 1):
                    try:
                        resp = await client.post(url, json=body, timeout=300)
                        resp.raise_for_status()
                        data = resp.json()
                        record["hits"] = extract_hits(data)
                        record["context_block"] = data.get("context_block", "")
                        break
                    except Exception as exc:
                        if attempt == max_retries:
                            record["error"] = str(exc)
                        else:
                            await asyncio.sleep(2 ** attempt)
                record["latency_ms"] = round((time.perf_counter() - start) * 1000, 1)
                out.write(json.dumps(record) + "\n")
                out.flush()
                if record["error"]:
                    stats.errored += 1
                else:
                    stats.completed += 1
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:18100")
    ap.add_argument("--config", choices=[*CONFIGS, "both"], default="both")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    rows = load_dataset(DATA_DIR / "longmemeval_s.json")
    if args.limit:
        rows = rows[: args.limit]
    names = list(CONFIGS) if args.config == "both" else [args.config]
    for name in names:
        out = WORK_DIR / f"recall_{name}.jsonl"
        stats = asyncio.run(run_recall(
            rows, args.base_url, name, out, progress=True))
        print(f"[{name}] completed={stats.completed} skipped={stats.skipped} "
              f"errored={stats.errored} -> {out}")


if __name__ == "__main__":
    main()
