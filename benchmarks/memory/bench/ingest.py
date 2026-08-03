"""Ingest LongMemEval haystacks through POST /memory/learn. Resumable."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from tqdm import tqdm

from bench.common import (
    DATA_DIR, WORK_DIR, date_tag, load_dataset, sanitize_namespace, session_tag,
)

_log = logging.getLogger(__name__)

_MAX_FIELD = 5000  # ActionLog action/outcome max_length


def _clip_or_fallback(text: str | None, fallback: str) -> str:
    """Truncate to the API field limit, falling back on empty/missing content.

    Real LongMemEval-S rows contain sessions with an empty-string (or absent)
    user turn. Left as "", it flows through as `action=""`, which the server
    rejects (ActionLog.action min_length=1) — the session fails ingest
    permanently and every resume re-attempts (and re-fails) it. Both sides
    (user -> "(no prompt)", assistant -> "(no reply)") get the same
    treatment, applied at every site that can emit a final pair.
    """
    return (text or "")[:_MAX_FIELD] or fallback


def turn_pairs(session: list[dict]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    pending_user: str | None = None
    for turn in session:
        role, content = turn.get("role"), (turn.get("content") or "")
        if role == "user":
            if pending_user is not None:
                pairs.append((_clip_or_fallback(pending_user, "(no prompt)"), "(no reply)"))
            pending_user = content
        elif role == "assistant":
            pairs.append((
                _clip_or_fallback(pending_user, "(no prompt)"),
                _clip_or_fallback(content, "(no reply)"),
            ))
            pending_user = None
    if pending_user is not None:
        pairs.append((_clip_or_fallback(pending_user, "(no prompt)"), "(no reply)"))
    return pairs


def _build_payload(ns: str, sid: str, date: str, user: str, assistant: str) -> dict:
    """The one place a /memory/learn payload is assembled from a turn pair.

    Shared by learn_payloads (dry-run / test inspection) and ingest()'s
    do_unit (the actual network path) so the two can never drift apart.
    """
    return {
        "action": user,
        "outcome": assistant,
        "tags": [session_tag(sid), date_tag(date)],
        "namespace": ns,
        "domain": "longmemeval",
        "memory_type": "episodic",
    }


def learn_payloads(row: dict) -> list[dict]:
    ns = sanitize_namespace(row["question_id"])
    payloads = []
    for sid, date, session in zip(
        row["haystack_session_ids"], row["haystack_dates"], row["haystack_sessions"]
    ):
        for user, assistant in turn_pairs(session):
            payloads.append(_build_payload(ns, sid, date, user, assistant))
    return payloads


class Ledger:
    """Append-only JSONL of completed (namespace/session) units."""

    def __init__(self, path: Path):
        self._path = path
        self._done: dict[str, int] = {}
        if path.exists():
            lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
            for i, line in enumerate(lines):
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    if i == len(lines) - 1:
                        # A process killed mid-append tears only the LAST
                        # line; skip it and keep every entry that's intact.
                        # A torn line anywhere else is real corruption and
                        # must still raise.
                        _log.warning(
                            "Ledger: skipping torn final line in %s", path)
                        continue
                    raise
                self._done[rec["key"]] = rec["n_memories"]

    def done(self, key: str) -> bool:
        return key in self._done

    def mark(self, key: str, n_memories: int) -> None:
        self._done[key] = n_memories
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"key": key, "n_memories": n_memories}) + "\n")

    def memories_per_session(self, namespace: str) -> dict[str, int]:
        prefix = namespace + "/"
        return {
            k[len(prefix):]: n for k, n in self._done.items()
            if k.startswith(prefix)
        }


@dataclass
class IngestStats:
    sessions_done: int = 0
    sessions_skipped: int = 0
    learn_calls: int = 0
    errors: list[str] = field(default_factory=list)


async def _post_with_retry(client, url, payload, max_retries):
    for attempt in range(max_retries + 1):
        try:
            resp = await client.post(url, json=payload, timeout=120)
            if resp.status_code < 500:
                resp.raise_for_status()
                return
        except httpx.HTTPStatusError:
            raise
        except httpx.HTTPError:
            pass
        if attempt < max_retries:
            await asyncio.sleep(2 ** attempt)
    raise RuntimeError(f"learn failed after {max_retries + 1} attempts")


async def ingest(rows, base_url, *, concurrency=8, ledger=None,
                 transport=None, max_retries=3, progress=False) -> IngestStats:
    stats = IngestStats()
    ledger = ledger or Ledger(WORK_DIR / "ingest_ledger.jsonl")
    sem = asyncio.Semaphore(concurrency)
    url = f"{base_url}/memory/learn"

    # One unit of resumable work = one (question, session).
    units = []
    for row in rows:
        ns = sanitize_namespace(row["question_id"])
        for sid, date, session in zip(
            row["haystack_session_ids"], row["haystack_dates"],
            row["haystack_sessions"],
        ):
            units.append((ns, sid, date, session))

    async with httpx.AsyncClient(transport=transport) as client:
        async def do_unit(ns, sid, date, session):
            key = f"{ns}/{sid}"
            if ledger.done(key):
                stats.sessions_skipped += 1
                return
            pairs = turn_pairs(session)
            try:
                for user, assistant in pairs:
                    payload = _build_payload(ns, sid, date, user, assistant)
                    async with sem:
                        await _post_with_retry(client, url, payload, max_retries)
                    stats.learn_calls += 1
                ledger.mark(key, n_memories=len(pairs))
                stats.sessions_done += 1
            except Exception as exc:  # session stays un-ledgered -> retried next run
                stats.errors.append(f"{key}: {exc}")

        iterator = [do_unit(*u) for u in units]
        if progress:
            for coro in tqdm(asyncio.as_completed(iterator), total=len(iterator)):
                await coro
        else:
            await asyncio.gather(*iterator)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:18100")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()
    rows = load_dataset(DATA_DIR / "longmemeval_s.json")
    if args.limit:
        rows = rows[: args.limit]
    stats = asyncio.run(ingest(
        rows, args.base_url, concurrency=args.concurrency, progress=True))
    print(f"done={stats.sessions_done} skipped={stats.sessions_skipped} "
          f"calls={stats.learn_calls} errors={len(stats.errors)}")
    for e in stats.errors[:20]:
        print("ERROR:", e)


if __name__ == "__main__":
    main()
