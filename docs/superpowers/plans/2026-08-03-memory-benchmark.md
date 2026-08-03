# LongMemEval Benchmark Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local-only benchmark harness that runs LongMemEval-S through Firekeep Cortex's real REST recall path and produces publishable retrieval metrics plus a clearly-labeled local-reader QA row.

**Architecture:** Plain-Python scripts under `benchmarks/memory/` drive an isolated docker compose stack (stock cortex image, dedicated volumes/collection) via `POST /memory/learn` and `POST /memory/recall`. Evidence-session ids ride in `tags` and are joined back from `sources[].metadata["tags"]` for deterministic Recall@k/Coverage@k/NDCG/MRR scoring; a local Ollama model provides the optional QA/judge pass.

**Tech Stack:** Python 3.11+, httpx, tqdm, huggingface_hub (dataset fetch only), pytest (with `httpx.MockTransport` for HTTP tests), docker compose, Ollama (host GPU: `mxbai-embed-large` embeddings, `qwen3:14b` reader/judge).

## Global Constraints

- No cloud APIs anywhere. Any Ollama model whose tag contains `:cloud` must be refused at pre-flight.
- All benchmark traffic goes through the REST surface of a stock cortex container — never import `app.*` from the harness.
- The bench stack must never share volumes, ports, collection name, or Redis DBs with a dev/prod stack. `QDRANT_COLLECTION=longmemeval`, ports offset by +10000.
- Spec: `docs/superpowers/specs/2026-08-03-memory-benchmark-design.md`. The harness is not part of any shipped wheel or image; nothing under `benchmarks/memory/` may be imported by service code.
- `benchmarks/memory/data/` and `benchmarks/memory/work/` are gitignored; `benchmarks/memory/results/` is committed.
- Per-request recall params are used (no env juggling between rows): defaults row `{top_k: 3, token_budget: 600, format: "synthesized"}`, bench row `{top_k: 10, token_budget: 10000, format: "raw"}` (10000 is `ContextQuery`'s hard cap).
- Errored questions are counted and reported; the denominator (500) never silently shrinks.
- Namespace pattern is `^[a-zA-Z0-9_-]+$` (server normalizes: lowercase, `-`→`_`). Question ids must be sanitized through one shared function.
- Every stage is resumable: re-running skips work recorded in the ledger/output files.

---

## File Structure

```
benchmarks/memory/
├── README.md                  # Task 9
├── .gitignore                 # Task 1 (data/, work/)
├── requirements.txt           # Task 1
├── docker-compose.bench.yml   # Task 2
├── bench/                     # importable package (python -m bench.*)
│   ├── __init__.py
│   ├── common.py              # Task 1: paths, sanitize_namespace, load_dataset, tag helpers
│   ├── download.py            # Task 1: fetch + verify LongMemEval-S
│   ├── ingest.py              # Task 3: haystack → /memory/learn, ledger, resumable
│   ├── recall.py              # Task 4: questions → /memory/recall, both configs
│   ├── score_retrieval.py     # Task 5: Recall@k / Coverage@k / NDCG / MRR
│   ├── qa.py                  # Task 6: local reader + judge via Ollama
│   ├── report.py              # Task 7: results JSON → tables + METHODOLOGY.md
│   └── run.py                 # Task 8: orchestrator + pre-flight
├── tests/
│   ├── conftest.py            # Task 1: tiny 2-question fixture dataset
│   ├── test_common.py         # Task 1
│   ├── test_ingest.py         # Task 3
│   ├── test_recall.py         # Task 4
│   ├── test_score_retrieval.py# Task 5
│   ├── test_qa.py             # Task 6
│   ├── test_report.py         # Task 7
│   └── test_run.py            # Task 8
├── data/                      # gitignored: longmemeval_s.json
├── work/                      # gitignored: ledger, raw recall dumps
└── results/                   # committed: run JSONs + METHODOLOGY.md
```

All commands below run from `benchmarks/memory/` with its own venv:
`python -m venv .venv && .venv/Scripts/pip install -r requirements.txt` (Windows dev box; POSIX equivalent applies). Tests: `.venv/Scripts/python -m pytest tests/ -v`.

---

### Task 1: Scaffold, common helpers, dataset download

**Files:**
- Create: `benchmarks/memory/.gitignore`, `benchmarks/memory/requirements.txt`, `benchmarks/memory/bench/__init__.py`, `benchmarks/memory/bench/common.py`, `benchmarks/memory/bench/download.py`
- Test: `benchmarks/memory/tests/conftest.py`, `benchmarks/memory/tests/test_common.py`

**Interfaces:**
- Produces (in `bench/common.py`, used by every later task):
  - `DATA_DIR`, `WORK_DIR`, `RESULTS_DIR: pathlib.Path` (module constants, relative to `benchmarks/memory/`)
  - `sanitize_namespace(question_id: str) -> str` — lowercase, non-`[a-z0-9_]` → `_`, prefix `lm_`, max 200 chars
  - `session_tag(session_id: str) -> str` — `f"lm_session:{session_id}"[:100]`
  - `date_tag(date: str) -> str` — `f"lm_date:{date}"[:100]`
  - `parse_session_tag(tags: list[str]) -> str | None` — inverse of `session_tag`
  - `load_dataset(path: Path) -> list[dict]` — loads JSON, calls `verify_dataset`
  - `verify_dataset(rows: list[dict]) -> None` — raises `ValueError` naming the first missing key
  - `is_abstention(question_id: str) -> bool` — `question_id.endswith("_abs")`
- Produces (in `bench/download.py`): `main()` CLI that downloads `longmemeval_s.json` from the HuggingFace repo `xiaowu0162/longmemeval` into `data/`, records `{"sha256", "source", "fetched_at"}` into `data/dataset_meta.json`, and runs `verify_dataset`.

**Steps:**

- [ ] **Step 1: Scaffold files**

`benchmarks/memory/.gitignore`:
```
data/
work/
.venv/
__pycache__/
```

`benchmarks/memory/requirements.txt`:
```
httpx>=0.27
tqdm>=4.66
huggingface_hub>=0.23
pytest>=8.0
```

`benchmarks/memory/bench/__init__.py`: empty file.

- [ ] **Step 2: Write failing tests for common helpers**

`benchmarks/memory/tests/conftest.py`:
```python
import json
import pytest

# Two-question miniature of the LongMemEval-S shape. Field names mirror the
# real dataset; verify_dataset enforces them at download time too.
FIXTURE_ROWS = [
    {
        "question_id": "q_multi_1",
        "question_type": "multi-session",
        "question": "What city did I move to?",
        "answer": "Lisbon",
        "question_date": "2023/05/20 (Sat) 10:00",
        "haystack_dates": ["2023/04/01 (Sat) 09:00", "2023/04/10 (Mon) 09:00"],
        "haystack_session_ids": ["s_a", "s_b"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "I'm planning a move to Lisbon."},
                {"role": "assistant", "content": "Exciting! When do you move?"},
                {"role": "user", "content": "Next month."},
                {"role": "assistant", "content": "Good luck with the move."},
            ],
            [
                {"role": "user", "content": "What's a good pasta recipe?"},
                {"role": "assistant", "content": "Try cacio e pepe."},
            ],
        ],
        "answer_session_ids": ["s_a"],
    },
    {
        "question_id": "q_skip_1_abs",
        "question_type": "single-session-user",
        "question": "What is my dog's name?",
        "answer": "N/A (abstention)",
        "question_date": "2023/05/21 (Sun) 10:00",
        "haystack_dates": ["2023/04/02 (Sun) 09:00"],
        "haystack_session_ids": ["s_c"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "I like hiking."},
                {"role": "assistant", "content": "Hiking is great exercise."},
            ]
        ],
        "answer_session_ids": [],
    },
]


@pytest.fixture
def fixture_dataset(tmp_path):
    p = tmp_path / "longmemeval_s.json"
    p.write_text(json.dumps(FIXTURE_ROWS), encoding="utf-8")
    return p
```

`benchmarks/memory/tests/test_common.py`:
```python
import pytest
from bench import common


def test_sanitize_namespace_is_server_legal():
    ns = common.sanitize_namespace("q_Multi.1/weird id")
    assert ns == "lm_q_multi_1_weird_id"
    assert len(ns) <= 200


def test_session_tag_roundtrip():
    tag = common.session_tag("s_abc123")
    assert tag == "lm_session:s_abc123"
    assert common.parse_session_tag(["other", tag]) == "s_abc123"


def test_parse_session_tag_missing_returns_none():
    assert common.parse_session_tag(["lm_date:2023/04/01"]) is None


def test_load_dataset_verifies(fixture_dataset):
    rows = common.load_dataset(fixture_dataset)
    assert len(rows) == 2


def test_verify_dataset_names_missing_key():
    with pytest.raises(ValueError, match="answer_session_ids"):
        common.verify_dataset([{"question_id": "x"}])


def test_is_abstention():
    assert common.is_abstention("q_skip_1_abs")
    assert not common.is_abstention("q_multi_1")
```

- [ ] **Step 3: Run tests, verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_common.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'bench'` — run from `benchmarks/memory/`).

- [ ] **Step 4: Implement `bench/common.py`**

```python
"""Shared helpers for the LongMemEval benchmark harness."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WORK_DIR = ROOT / "work"
RESULTS_DIR = ROOT / "results"

_SESSION_TAG_PREFIX = "lm_session:"
_DATE_TAG_PREFIX = "lm_date:"

# Keys every LongMemEval-S row must carry. Verified at download AND load time
# so a dataset-format drift fails loudly, never as a zero-score run.
REQUIRED_KEYS = frozenset({
    "question_id", "question_type", "question", "answer", "question_date",
    "haystack_dates", "haystack_session_ids", "haystack_sessions",
    "answer_session_ids",
})


def sanitize_namespace(question_id: str) -> str:
    """Map a question id to a server-legal namespace (^[a-zA-Z0-9_-]+$)."""
    cleaned = re.sub(r"[^a-z0-9_]", "_", question_id.lower())
    return f"lm_{cleaned}"[:200]


def session_tag(session_id: str) -> str:
    return f"{_SESSION_TAG_PREFIX}{session_id}"[:100]


def date_tag(date: str) -> str:
    return f"{_DATE_TAG_PREFIX}{date}"[:100]


def parse_session_tag(tags: list[str]) -> str | None:
    for tag in tags:
        if isinstance(tag, str) and tag.startswith(_SESSION_TAG_PREFIX):
            return tag[len(_SESSION_TAG_PREFIX):]
    return None


def is_abstention(question_id: str) -> bool:
    return question_id.endswith("_abs")


def verify_dataset(rows: list[dict]) -> None:
    if not rows:
        raise ValueError("dataset is empty")
    for i, row in enumerate(rows):
        missing = REQUIRED_KEYS - row.keys()
        if missing:
            raise ValueError(
                f"row {i} missing keys: {sorted(missing)} — dataset format drift?"
            )


def load_dataset(path: Path) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    verify_dataset(rows)
    return rows
```

- [ ] **Step 5: Run tests, verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_common.py -v`
Expected: 6 passed.

- [ ] **Step 6: Implement `bench/download.py`** (no unit test — network CLI; verified by the smoke run in Task 9)

```python
"""Fetch LongMemEval-S from HuggingFace into data/, checksum + verify."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from huggingface_hub import hf_hub_download

from bench.common import DATA_DIR, load_dataset

HF_REPO = "xiaowu0162/longmemeval"
HF_FILE = "longmemeval_s.json"


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / HF_FILE
    if target.exists():
        print(f"already present: {target}")
    else:
        fetched = hf_hub_download(
            repo_id=HF_REPO, filename=HF_FILE, repo_type="dataset",
            local_dir=str(DATA_DIR),
        )
        print(f"downloaded: {fetched}")

    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    rows = load_dataset(target)  # raises loudly on format drift
    meta = {
        "sha256": sha,
        "source": f"hf://{HF_REPO}/{HF_FILE}",
        "questions": len(rows),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    (DATA_DIR / "dataset_meta.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
```

Note for the implementer: if the HF repo/file name has drifted, `hf_hub_download` fails loudly — check the LongMemEval GitHub README for the current location and update `HF_REPO`/`HF_FILE`; `verify_dataset` guards the schema either way.

- [ ] **Step 7: Commit**

```bash
git add benchmarks/memory
git commit -m "feat(bench): scaffold LongMemEval harness — common helpers + dataset download"
```

---

### Task 2: Isolated bench compose stack

**Files:**
- Create: `benchmarks/memory/docker-compose.bench.yml`

**Interfaces:**
- Produces: a stack reachable at `http://127.0.0.1:18100` (cortex REST) that every later task's live traffic targets. Project name `firekeep-bench`, dedicated volumes.

**Steps:**

- [ ] **Step 1: Write the compose file**

Copy the pin lines (image tag@digest) for neo4j/qdrant/redis **verbatim from the root `docker-compose.yml`** (image pinning rules apply — do not retype digests from memory). Shape:

```yaml
# Isolated LongMemEval benchmark stack. NEVER share volumes/ports with dev.
# Usage: docker compose -f docker-compose.bench.yml -p firekeep-bench up -d
name: firekeep-bench

services:
  neo4j:
    image: <copy pinned neo4j image ref from root docker-compose.yml>
    environment:
      NEO4J_AUTH: neo4j/benchpassword
    ports: ["127.0.0.1:17687:7687"]
    volumes: [bench_neo4j:/data]

  qdrant:
    image: <copy pinned qdrant image ref from root docker-compose.yml>
    ports: ["127.0.0.1:16333:6333"]
    volumes: [bench_qdrant:/qdrant/storage]

  redis:
    image: <copy pinned redis image ref from root docker-compose.yml>
    ports: ["127.0.0.1:16379:6379"]
    volumes: [bench_redis:/data]

  cortex-api:
    build:
      context: ../..
      dockerfile: cortex/Dockerfile
    ports: ["127.0.0.1:18100:8000"]
    environment:
      NEO4J_URI: bolt://neo4j:7687
      NEO4J_USER: neo4j
      NEO4J_PASSWORD: benchpassword
      QDRANT_HOST: qdrant
      QDRANT_PORT: "6333"
      QDRANT_COLLECTION: longmemeval
      REDIS_URL: redis://redis:6379/0
      # Host GPU Ollama for embeddings + (recall-path) LLM calls.
      LLM_BASE_URL: http://host.docker.internal:11434
      EMBEDDING_MODEL: mxbai-embed-large
      EMBEDDING_DIM: "1024"
      # Benchmark posture: single-user isolated stack, no background mutation.
      AUTH_ENABLED: "false"
      GC_ENABLED: "false"
      AGENT_ENABLED: "false"
      SKILL_SYNTHESIS_ENABLED: "false"
      OWM_ENABLED: "false"
      DEDUP_ENABLED: "false"
      # 60/min would strangle a ~300k-call ingest.
      RATE_LIMIT: "100000/minute"
    extra_hosts: ["host.docker.internal:host-gateway"]
    depends_on: [neo4j, qdrant, redis]

volumes:
  bench_neo4j:
  bench_qdrant:
  bench_redis:
```

Check the root compose for how cortex-api declares its healthcheck and any required env this sketch omits (e.g. `VAULT_KEY` may be mandatory at startup — if `Settings` requires it, set a throwaway `VAULT_KEY` generated per the CLAUDE.md Fernet one-liner, or `VAULT_ENABLED: "false"` if that skips the requirement). The worker/beat/mcp services are deliberately absent — the benchmark uses REST only and no Celery task should mutate memories mid-run.

- [ ] **Step 2: Validate config renders**

Run: `docker compose -f benchmarks/memory/docker-compose.bench.yml -p firekeep-bench config --quiet && echo OK`
Expected: `OK` (no interpolation errors).

- [ ] **Step 3: Bring the stack up and verify health**

Run: `docker compose -f benchmarks/memory/docker-compose.bench.yml -p firekeep-bench up -d --build`, then `curl -s http://127.0.0.1:18100/health`
Expected: JSON with all backends reporting connected. If `ollama list` on the host lacks `mxbai-embed-large`, run `ollama pull mxbai-embed-large` first.

- [ ] **Step 4: Verify learn→recall round-trip carries tags** (the join mechanism the whole benchmark rests on)

```bash
curl -s -X POST http://127.0.0.1:18100/memory/learn -H "Content-Type: application/json" \
  -d '{"action":"probe turn","outcome":"probe reply","tags":["lm_session:probe_s1","lm_date:2023/04/01"],"namespace":"lm_probe","domain":"longmemeval"}'
curl -s -X POST http://127.0.0.1:18100/memory/recall -H "Content-Type: application/json" \
  -d '{"task":"probe turn","namespace":"lm_probe","top_k":3,"format":"raw"}'
```
Expected: the recall response's `sources[0].metadata.tags` contains `lm_session:probe_s1`. **If it does not, STOP — the join mechanism is broken; investigate `_projected_metadata` before any further task.**

- [ ] **Step 5: Commit**

```bash
git add benchmarks/memory/docker-compose.bench.yml
git commit -m "feat(bench): isolated compose stack for LongMemEval runs"
```

---

### Task 3: Ingest with resumable ledger

**Files:**
- Create: `benchmarks/memory/bench/ingest.py`
- Test: `benchmarks/memory/tests/test_ingest.py`

**Interfaces:**
- Consumes: `common.sanitize_namespace/session_tag/date_tag/load_dataset`, the Task 2 stack.
- Produces:
  - `turn_pairs(session: list[dict]) -> list[tuple[str, str]]` — pure: pairs each user turn with the following assistant turn (dangling turns pair with `"(no reply)"` / `"(no prompt)"`); each side truncated to 5000 chars.
  - `learn_payloads(row: dict) -> list[dict]` — pure: full `POST /memory/learn` bodies for one question row (all sessions), with `namespace`, `domain="longmemeval"`, `memory_type="episodic"`, `tags=[session_tag, date_tag]`.
  - `Ledger` — `done(key: str) -> bool`, `mark(key: str, n_memories: int)`, `memories_per_session(namespace: str) -> dict[str, int]`; JSONL-backed at `work/ingest_ledger.jsonl`, append-only, one line per completed *(namespace, session_id)*.
  - `async ingest(rows, base_url, concurrency=8, ledger=None) -> IngestStats` (`IngestStats`: `sessions_done`, `sessions_skipped`, `learn_calls`, `errors: list[str]`).
  - CLI: `python -m bench.ingest --base-url http://127.0.0.1:18100 --limit N`.

**Steps:**

- [ ] **Step 1: Write failing tests**

`benchmarks/memory/tests/test_ingest.py`:
```python
import httpx
import pytest

from bench import common, ingest
from tests.conftest import FIXTURE_ROWS


def test_turn_pairs_pairs_user_with_assistant():
    session = FIXTURE_ROWS[0]["haystack_sessions"][0]
    pairs = ingest.turn_pairs(session)
    assert pairs[0] == ("I'm planning a move to Lisbon.", "Exciting! When do you move?")
    assert len(pairs) == 2


def test_turn_pairs_handles_dangling_user_turn():
    pairs = ingest.turn_pairs([{"role": "user", "content": "hello?"}])
    assert pairs == [("hello?", "(no reply)")]


def test_turn_pairs_truncates_to_api_limit():
    pairs = ingest.turn_pairs([
        {"role": "user", "content": "x" * 9000},
        {"role": "assistant", "content": "y" * 9000},
    ])
    assert len(pairs[0][0]) == 5000 and len(pairs[0][1]) == 5000


def test_learn_payloads_stamps_namespace_and_tags():
    payloads = ingest.learn_payloads(FIXTURE_ROWS[0])
    assert all(p["namespace"] == "lm_q_multi_1" for p in payloads)
    first = payloads[0]
    assert common.session_tag("s_a") in first["tags"]
    assert common.date_tag("2023/04/01 (Sat) 09:00") in first["tags"]
    assert first["memory_type"] == "episodic"
    assert first["domain"] == "longmemeval"
    # 2 pairs from session s_a + 1 pair from s_b
    assert len(payloads) == 3


def test_ledger_roundtrip(tmp_path):
    led = ingest.Ledger(tmp_path / "ledger.jsonl")
    key = "lm_q_multi_1/s_a"
    assert not led.done(key)
    led.mark(key, n_memories=2)
    assert led.done(key)
    # A fresh instance re-reads the file (resume-after-crash behavior).
    led2 = ingest.Ledger(tmp_path / "ledger.jsonl")
    assert led2.done(key)
    assert led2.memories_per_session("lm_q_multi_1") == {"s_a": 2}


@pytest.mark.anyio
async def test_ingest_skips_ledgered_sessions_and_posts_the_rest(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "ok", "vector_id": "v1"})

    led = ingest.Ledger(tmp_path / "ledger.jsonl")
    led.mark("lm_q_multi_1/s_a", n_memories=2)
    transport = httpx.MockTransport(handler)
    stats = await ingest.ingest(
        [FIXTURE_ROWS[0]], "http://bench", ledger=led, transport=transport
    )
    assert stats.sessions_skipped == 1
    assert stats.sessions_done == 1
    assert stats.learn_calls == 1  # only s_b's single pair
    assert not stats.errors


@pytest.mark.anyio
async def test_ingest_records_error_and_continues(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    led = ingest.Ledger(tmp_path / "ledger.jsonl")
    stats = await ingest.ingest(
        [FIXTURE_ROWS[0]], "http://bench", ledger=led,
        transport=httpx.MockTransport(handler), max_retries=1,
    )
    assert stats.errors  # recorded, not raised
    assert not led.done("lm_q_multi_1/s_a")  # failed session NOT marked done
```

Add to `tests/conftest.py`:
```python
@pytest.fixture
def anyio_backend():
    return "asyncio"
```
(and add `anyio>=4` to `requirements.txt` — it ships with httpx anyway).

- [ ] **Step 2: Run tests, verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_ingest.py -v`
Expected: FAIL (`ingest` has no attributes).

- [ ] **Step 3: Implement `bench/ingest.py`**

```python
"""Ingest LongMemEval haystacks through POST /memory/learn. Resumable."""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from tqdm import tqdm

from bench.common import (
    DATA_DIR, WORK_DIR, date_tag, load_dataset, sanitize_namespace, session_tag,
)

_MAX_FIELD = 5000  # ActionLog action/outcome max_length


def turn_pairs(session: list[dict]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    pending_user: str | None = None
    for turn in session:
        role, content = turn.get("role"), (turn.get("content") or "")
        if role == "user":
            if pending_user is not None:
                pairs.append((pending_user[:_MAX_FIELD], "(no reply)"))
            pending_user = content
        elif role == "assistant":
            user = pending_user if pending_user is not None else "(no prompt)"
            pairs.append((user[:_MAX_FIELD], content[:_MAX_FIELD] or "(no reply)"))
            pending_user = None
    if pending_user is not None:
        pairs.append((pending_user[:_MAX_FIELD], "(no reply)"))
    return pairs


def learn_payloads(row: dict) -> list[dict]:
    ns = sanitize_namespace(row["question_id"])
    payloads = []
    for sid, date, session in zip(
        row["haystack_session_ids"], row["haystack_dates"], row["haystack_sessions"]
    ):
        for user, assistant in turn_pairs(session):
            payloads.append({
                "action": user,
                "outcome": assistant,
                "tags": [session_tag(sid), date_tag(date)],
                "namespace": ns,
                "domain": "longmemeval",
                "memory_type": "episodic",
            })
    return payloads


class Ledger:
    """Append-only JSONL of completed (namespace/session) units."""

    def __init__(self, path: Path):
        self._path = path
        self._done: dict[str, int] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
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
                    payload = {
                        "action": user, "outcome": assistant,
                        "tags": [session_tag(sid), date_tag(date)],
                        "namespace": ns, "domain": "longmemeval",
                        "memory_type": "episodic",
                    }
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
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_ingest.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/memory/bench/ingest.py benchmarks/memory/tests/test_ingest.py benchmarks/memory/tests/conftest.py benchmarks/memory/requirements.txt
git commit -m "feat(bench): resumable turn-pair ingest via POST /memory/learn"
```

---

### Task 4: Recall runner (both configs)

**Files:**
- Create: `benchmarks/memory/bench/recall.py`
- Test: `benchmarks/memory/tests/test_recall.py`

**Interfaces:**
- Consumes: `common.sanitize_namespace/parse_session_tag/load_dataset`, Task 2 stack.
- Produces:
  - `CONFIGS: dict[str, dict]` — `{"defaults": {"top_k": 3, "token_budget": 600, "format": "synthesized"}, "bench": {"top_k": 10, "token_budget": 10000, "format": "raw"}}`
  - `recall_body(row: dict, config: dict) -> dict` — full `POST /memory/recall` body; `task` = the question text (truncated to 2000, `ContextQuery.task` cap).
  - `extract_hits(response_json: dict) -> list[dict]` — ordered `[{"session_id", "score", "content"}]` from `sources[]` (vector+both stores; graph-only sources have no tags and yield `session_id=None`, kept for rank accounting).
  - `async run_recall(rows, base_url, config_name, out_path, ...) -> RecallStats` — writes one JSONL line per question: `{"question_id", "config", "hits", "context_block", "latency_ms", "error"}`. Resumable: questions already present in `out_path` are skipped.
  - CLI: `python -m bench.recall --config defaults|bench|both --limit N`.
  - Output paths: `work/recall_<config>.jsonl` — consumed by Tasks 5 and 6.

**Steps:**

- [ ] **Step 1: Write failing tests**

`benchmarks/memory/tests/test_recall.py`:
```python
import json

import httpx
import pytest

from bench import recall
from tests.conftest import FIXTURE_ROWS

RESPONSE = {
    "context_block": "ctx",
    "score": 0.8,
    "sources": [
        {"store": "vector", "content": "m1", "score": 0.9,
         "metadata": {"tags": ["lm_session:s_a", "lm_date:2023/04/01 (Sat) 09:00"]}},
        {"store": "graph", "content": "g1", "score": 0.5, "metadata": {}},
        {"store": "both", "content": "m2", "score": 0.4,
         "metadata": {"tags": ["lm_session:s_b", "lm_date:2023/04/10 (Mon) 09:00"]}},
    ],
}


def test_recall_body_uses_config_and_namespace():
    body = recall.recall_body(FIXTURE_ROWS[0], recall.CONFIGS["bench"])
    assert body["namespace"] == "lm_q_multi_1"
    assert body["top_k"] == 10
    assert body["format"] == "raw"
    assert body["token_budget"] == 10000
    assert body["task"] == FIXTURE_ROWS[0]["question"]


def test_extract_hits_preserves_rank_and_joins_tags():
    hits = recall.extract_hits(RESPONSE)
    assert [h["session_id"] for h in hits] == ["s_a", None, "s_b"]
    assert hits[0]["score"] == 0.9


@pytest.mark.anyio
async def test_run_recall_writes_jsonl_and_resumes(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=RESPONSE)

    out = tmp_path / "recall_bench.jsonl"
    transport = httpx.MockTransport(handler)
    stats = await recall.run_recall(
        FIXTURE_ROWS, "http://bench", "bench", out, transport=transport)
    assert stats.completed == 2 and len(calls) == 2
    # Second run: everything already recorded, no new HTTP calls.
    stats2 = await recall.run_recall(
        FIXTURE_ROWS, "http://bench", "bench", out, transport=transport)
    assert stats2.skipped == 2 and len(calls) == 2


@pytest.mark.anyio
async def test_run_recall_records_error_row(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    out = tmp_path / "recall_bench.jsonl"
    stats = await recall.run_recall(
        FIXTURE_ROWS[:1], "http://bench", "bench", out,
        transport=httpx.MockTransport(handler), max_retries=0)
    assert stats.errored == 1
    row = json.loads(out.read_text().splitlines()[0])
    assert row["error"]
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_recall.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `bench/recall.py`**

```python
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
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_recall.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/memory/bench/recall.py benchmarks/memory/tests/test_recall.py
git commit -m "feat(bench): recall runner for defaults + bench config rows"
```

---

### Task 5: Retrieval scoring

**Files:**
- Create: `benchmarks/memory/bench/score_retrieval.py`
- Test: `benchmarks/memory/tests/test_score_retrieval.py`

**Interfaces:**
- Consumes: `work/recall_<config>.jsonl` rows (Task 4 shape), dataset rows, `Ledger.memories_per_session` (Task 3) for NDCG's ideal ranking.
- Produces:
  - `score_question(hits: list[dict], evidence_ids: set[str], k: int, n_relevant_available: int) -> dict` — pure; returns `{"recall_at_k": 0|1, "coverage_at_k": float, "mrr": float, "ndcg_at_k": float}`.
  - `aggregate(question_scores: list[dict]) -> dict` — means per metric + count.
  - `score_run(dataset_rows, recall_path: Path, ledger: Ledger, k: int) -> dict` — full result block: overall aggregates, per-`question_type` aggregates, `errored_questions`, `abstention_excluded` count.
  - CLI: `python -m bench.score_retrieval --config bench --k 10` → prints JSON, writes `work/scores_<config>.json`.

**Metric definitions (locked here, mirrored in METHODOLOGY.md by Task 7):**
- A hit is *relevant* iff its `session_id` ∈ the question's `answer_session_ids`.
- **Evidence Recall@k** = 1 if any of the first k hits is relevant.
- **Evidence Coverage@k** = |distinct evidence sessions among first k hits| / |evidence sessions|.
- **MRR** = 1/rank of the first relevant hit (0 if none in top k).
- **NDCG@k**: binary gains, `DCG = Σ rel_i / log2(i+1)`; `IDCG` assumes the top `min(k, n_relevant_available)` slots are all relevant, where `n_relevant_available` = total memories ingested for that question's evidence sessions (from the ledger). Graph-only hits (`session_id=None`) count as non-relevant but occupy rank slots — that is deliberate: they consumed a top-k slot the product actually spent.
- Abstention questions (`*_abs`) and errored questions are excluded from aggregates and counted separately.

**Steps:**

- [ ] **Step 1: Write failing tests** (hand-computed expectations — do not derive them by running the code)

`benchmarks/memory/tests/test_score_retrieval.py`:
```python
import math

import pytest

from bench import score_retrieval as sr


def _hits(*session_ids):
    return [{"session_id": s, "score": 1.0, "content": ""} for s in session_ids]


def test_perfect_first_hit():
    s = sr.score_question(_hits("e1", "x", "x"), {"e1"}, k=3, n_relevant_available=5)
    assert s["recall_at_k"] == 1
    assert s["mrr"] == 1.0
    assert s["coverage_at_k"] == 1.0
    # DCG = 1/log2(2) = 1.0; IDCG (3 ideal relevant slots) = 1 + 1/log2(3) + 1/log2(4)
    idcg = 1 + 1 / math.log2(3) + 1 / math.log2(4)
    assert s["ndcg_at_k"] == pytest.approx(1.0 / idcg)


def test_no_relevant_hits():
    s = sr.score_question(_hits("x", None, "y"), {"e1"}, k=3, n_relevant_available=4)
    assert s == {"recall_at_k": 0, "coverage_at_k": 0.0, "mrr": 0.0, "ndcg_at_k": 0.0}


def test_second_rank_hit_mrr_and_ndcg():
    s = sr.score_question(_hits("x", "e1"), {"e1", "e2"}, k=2, n_relevant_available=6)
    assert s["mrr"] == 0.5
    assert s["coverage_at_k"] == 0.5  # one of two evidence sessions found
    dcg = 1 / math.log2(3)          # relevant at rank 2
    idcg = 1 + 1 / math.log2(3)      # 2 ideal slots (k=2 < available)
    assert s["ndcg_at_k"] == pytest.approx(dcg / idcg)


def test_ideal_capped_by_available_relevant():
    # Only 1 relevant memory exists in the namespace: IDCG must use 1 slot, not k.
    s = sr.score_question(_hits("e1", "x", "x"), {"e1"}, k=3, n_relevant_available=1)
    assert s["ndcg_at_k"] == pytest.approx(1.0)


def test_duplicate_session_counts_once_for_coverage():
    s = sr.score_question(_hits("e1", "e1"), {"e1", "e2"}, k=2, n_relevant_available=4)
    assert s["coverage_at_k"] == 0.5


def test_aggregate_means():
    agg = sr.aggregate([
        {"recall_at_k": 1, "coverage_at_k": 1.0, "mrr": 1.0, "ndcg_at_k": 1.0},
        {"recall_at_k": 0, "coverage_at_k": 0.0, "mrr": 0.0, "ndcg_at_k": 0.0},
    ])
    assert agg["n"] == 2
    assert agg["recall_at_k"] == 0.5
    assert agg["mrr"] == 0.5
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_score_retrieval.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `bench/score_retrieval.py`**

```python
"""Reader-independent retrieval metrics from recall JSONL dumps."""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

from bench.common import DATA_DIR, WORK_DIR, is_abstention, load_dataset, sanitize_namespace
from bench.ingest import Ledger

_METRICS = ("recall_at_k", "coverage_at_k", "mrr", "ndcg_at_k")


def score_question(hits, evidence_ids, k, n_relevant_available) -> dict:
    top = hits[:k]
    rels = [1 if h["session_id"] in evidence_ids else 0 for h in top]

    recall = 1 if any(rels) else 0
    found = {h["session_id"] for h, r in zip(top, rels) if r}
    coverage = len(found) / len(evidence_ids) if evidence_ids else 0.0
    mrr = 0.0
    for i, r in enumerate(rels):
        if r:
            mrr = 1.0 / (i + 1)
            break
    dcg = sum(r / math.log2(i + 2) for i, r in enumerate(rels))
    ideal_slots = min(k, max(n_relevant_available, 0))
    idcg = sum(1 / math.log2(i + 2) for i in range(ideal_slots))
    ndcg = (dcg / idcg) if idcg > 0 else 0.0
    return {"recall_at_k": recall, "coverage_at_k": coverage,
            "mrr": mrr, "ndcg_at_k": ndcg}


def aggregate(question_scores: list[dict]) -> dict:
    n = len(question_scores)
    out = {"n": n}
    for m in _METRICS:
        out[m] = (sum(q[m] for q in question_scores) / n) if n else 0.0
    return out


def score_run(dataset_rows, recall_path: Path, ledger: Ledger, k: int) -> dict:
    recalls = {}
    for line in recall_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            recalls[rec["question_id"]] = rec

    per_question, by_type = [], defaultdict(list)
    errored, abstention_excluded, missing = [], 0, []
    for row in dataset_rows:
        qid = row["question_id"]
        if is_abstention(qid):
            abstention_excluded += 1
            continue
        rec = recalls.get(qid)
        if rec is None:
            missing.append(qid)
            continue
        if rec.get("error"):
            errored.append(qid)
            continue
        ns = sanitize_namespace(qid)
        evidence = set(row["answer_session_ids"])
        per_sess = ledger.memories_per_session(ns)
        n_avail = sum(per_sess.get(sid, 0) for sid in evidence)
        s = score_question(rec["hits"], evidence, k, n_avail)
        per_question.append(s)
        by_type[row["question_type"]].append(s)

    return {
        "k": k,
        "overall": aggregate(per_question),
        "by_question_type": {t: aggregate(v) for t, v in sorted(by_type.items())},
        "errored_questions": errored,
        "missing_questions": missing,
        "abstention_excluded": abstention_excluded,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--k", type=int, required=True)
    args = ap.parse_args()
    rows = load_dataset(DATA_DIR / "longmemeval_s.json")
    ledger = Ledger(WORK_DIR / "ingest_ledger.jsonl")
    result = score_run(rows, WORK_DIR / f"recall_{args.config}.jsonl", ledger, args.k)
    out = WORK_DIR / f"scores_{args.config}.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["overall"], indent=2))
    print(f"-> {out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_score_retrieval.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/memory/bench/score_retrieval.py benchmarks/memory/tests/test_score_retrieval.py
git commit -m "feat(bench): retrieval metrics — Recall@k, Coverage@k, MRR, NDCG"
```

---

### Task 6: Local QA + judge

**Files:**
- Create: `benchmarks/memory/bench/qa.py`
- Test: `benchmarks/memory/tests/test_qa.py`

**Interfaces:**
- Consumes: `work/recall_bench.jsonl` (Task 4), dataset rows, host Ollama `POST /api/chat`.
- Produces:
  - `READER_MODEL = "qwen3:14b"` (module constant; overridable via `--model`).
  - `refuse_cloud(model: str) -> None` — raises `ValueError` if `":cloud"` in the model tag.
  - `reader_messages(question: str, question_date: str, context: str) -> list[dict]` — chat messages instructing: answer ONLY from context; if the context doesn't contain the answer, reply exactly `I don't know.`.
  - `judge_messages(question: str, gold: str, answer: str) -> list[dict]` — yes/no equivalence judgment, must end with `VERDICT: CORRECT` or `VERDICT: INCORRECT`.
  - `parse_verdict(text: str) -> bool | None` — last VERDICT token wins; `None` if absent.
  - `score_abstention(answer: str) -> bool` — correct iff the reader declined (contains "don't know"/"do not know"/"no information", case-insensitive).
  - `async run_qa(rows, recall_path, out_path, base_url="http://127.0.0.1:11434", model=READER_MODEL, ...) -> QAStats` — JSONL out: `{"question_id", "answer", "verdict", "judge_error"}`; resumable like Task 4; temperature 0.
  - CLI: `python -m bench.qa --limit N --model qwen3:14b`.
- Context for the reader = the recall row's `hits` contents concatenated (bench config row), each prefixed by its `lm_date` tag date when present — NOT the raw `context_block` (raw format's block and hits carry the same content; hits keep the date join explicit).

**Steps:**

- [ ] **Step 1: Write failing tests**

`benchmarks/memory/tests/test_qa.py`:
```python
import pytest

from bench import qa


def test_refuse_cloud():
    with pytest.raises(ValueError, match="cloud"):
        qa.refuse_cloud("minimax-m2:cloud")
    qa.refuse_cloud("qwen3:14b")  # no raise


def test_parse_verdict():
    assert qa.parse_verdict("blah\nVERDICT: CORRECT") is True
    assert qa.parse_verdict("VERDICT: INCORRECT") is False
    assert qa.parse_verdict("no verdict here") is None
    # Last token wins when the model narrates both.
    assert qa.parse_verdict("VERDICT: CORRECT ... VERDICT: INCORRECT") is False


def test_score_abstention():
    assert qa.score_abstention("I don't know.")
    assert qa.score_abstention("There is no information about that.")
    assert not qa.score_abstention("Your dog's name is Rex.")


def test_reader_messages_pin_the_contract():
    msgs = qa.reader_messages("Q?", "2023/05/20", "CTX")
    joined = " ".join(m["content"] for m in msgs)
    assert "CTX" in joined and "Q?" in joined
    assert "I don't know." in joined  # abstention contract is in the prompt
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_qa.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `bench/qa.py`**

```python
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
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_qa.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/memory/bench/qa.py benchmarks/memory/tests/test_qa.py
git commit -m "feat(bench): local reader QA + judge (non-comparable row, clearly labeled)"
```

---

### Task 7: Report + METHODOLOGY generation

**Files:**
- Create: `benchmarks/memory/bench/report.py`
- Test: `benchmarks/memory/tests/test_report.py`

**Interfaces:**
- Consumes: `work/scores_defaults.json`, `work/scores_bench.json` (Task 5 shape), `work/qa_bench.jsonl` (Task 6 shape), `data/dataset_meta.json` (Task 1), cortex `GET /version` JSON, host `GET /api/tags` (model inventory).
- Produces:
  - `qa_accuracy(qa_rows: list[dict]) -> dict` — `{"n", "correct", "accuracy", "judge_errors"}`; `verdict=None` rows count in `judge_errors`, excluded from accuracy denominator.
  - `build_result(scores: dict[str, dict], qa: dict | None, meta: dict) -> dict` — the full run-record dict (spec's `results/<timestamp>-<git_sha>.json` shape).
  - `render_markdown(result: dict) -> str` — the results table.
  - `render_methodology(result: dict) -> str` — full METHODOLOGY.md text; MUST contain, verbatim: the metric definitions from Task 5, the sentence `The local-reader QA row is NOT comparable to published GPT-4o-reader numbers.`, the defaults-vs-bench explanation, and the five Known Limitations from the spec (copy them from `docs/superpowers/specs/2026-08-03-memory-benchmark-design.md` §Known limitations).
  - CLI: `python -m bench.report --run-label <label>` → writes `results/<UTC yyyymmdd-HHMMSS>-<label>.json` and `results/METHODOLOGY.md`.

**Steps:**

- [ ] **Step 1: Write failing tests**

`benchmarks/memory/tests/test_report.py`:
```python
from bench import report


def test_qa_accuracy_excludes_judge_errors_from_denominator():
    rows = [
        {"question_id": "a", "verdict": True, "judge_error": None},
        {"question_id": "b", "verdict": False, "judge_error": None},
        {"question_id": "c", "verdict": None, "judge_error": "unparseable"},
    ]
    acc = report.qa_accuracy(rows)
    assert acc == {"n": 2, "correct": 1, "accuracy": 0.5, "judge_errors": 1}


def _fake_scores():
    agg = {"n": 2, "recall_at_k": 0.5, "coverage_at_k": 0.5, "mrr": 0.5,
           "ndcg_at_k": 0.5}
    return {"k": 10, "overall": agg, "by_question_type": {"multi-session": agg},
            "errored_questions": [], "missing_questions": [],
            "abstention_excluded": 1}


def test_render_methodology_carries_mandatory_caveats():
    result = report.build_result(
        {"defaults": _fake_scores(), "bench": _fake_scores()},
        {"n": 2, "correct": 1, "accuracy": 0.5, "judge_errors": 0},
        {"dataset": {"sha256": "abc"}, "cortex_version": {"git_sha": "deadbeef"},
         "models": {"reader": "qwen3:14b", "embed": "mxbai-embed-large"}},
    )
    text = report.render_methodology(result)
    assert "NOT comparable to published GPT-4o-reader numbers" in text
    assert "Evidence Recall@k" in text
    assert "deadbeef" in text
    assert "floor, not the ceiling" in text  # known-limitations block present


def test_render_markdown_has_both_config_rows():
    result = report.build_result(
        {"defaults": _fake_scores(), "bench": _fake_scores()}, None,
        {"dataset": {}, "cortex_version": {}, "models": {}},
    )
    md = report.render_markdown(result)
    assert "defaults" in md and "bench" in md and "0.500" in md
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_report.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `bench/report.py`**

Implementation notes (write real code, this is the shape):
- `qa_accuracy`: filter `verdict is not None` for the denominator, count `judge_error` rows.
- `build_result`: `{"generated_at": UTC iso, "meta": meta, "retrieval": scores, "qa_local": qa}`.
- `render_markdown`: one table row per config: `| config | k | n | Recall@k | Coverage@k | MRR | NDCG@k |` with 3-decimal formatting, plus a QA line when present.
- `render_methodology`: an f-string document with sections: What was run (dataset sha, cortex git_sha/version, exact model tags, both config dicts verbatim); Metric definitions (copy the Task 5 "Metric definitions" block); The two rows explained (defaults = stock install honesty row; bench = comparable row, competitors also tune retrieval); QA caveat sentence (verbatim, above); Known limitations (the spec's five, copied verbatim); Reproduction (the exact command sequence from Task 9's README section).
- `main()`: fetch `GET {cortex}/version` and `GET {ollama}/api/tags` best-effort (record `"unavailable"` on failure — never crash reporting), assemble, write both files.

- [ ] **Step 4: Run tests, verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_report.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/memory/bench/report.py benchmarks/memory/tests/test_report.py
git commit -m "feat(bench): run-record JSON + generated METHODOLOGY.md"
```

---

### Task 8: Orchestrator + pre-flight

**Files:**
- Create: `benchmarks/memory/bench/run.py`
- Test: `benchmarks/memory/tests/test_run.py`

**Interfaces:**
- Consumes: every prior module's CLI-equivalent entry function.
- Produces:
  - `preflight(cortex_url, ollama_url, reader_model, skip_qa) -> list[str]` — returns a list of human-readable failures (empty = go): cortex `/health` reachable; dataset file + `dataset_meta.json` present; `mxbai-embed-large` in Ollama tags; reader model present (only when QA will run); reader model refused if `:cloud`; ≥5 GB free disk on the drive holding `work/`.
  - `run(argv) -> int` — stages in order: preflight → ingest → recall(both) → score(both, k=top_k per config) → qa (unless `--skip-qa`) → report. Each stage prints a one-line summary; a stage failure stops the run with a non-zero exit and leaves everything resumable.
  - CLI: `python -m bench.run --limit N --config both --skip-qa --run-label smoke`.

**Steps:**

- [ ] **Step 1: Write failing tests**

`benchmarks/memory/tests/test_run.py`:
```python
from bench import run as runmod


def test_preflight_flags_cloud_reader(monkeypatch):
    # Isolate from live services: health/dataset/model checks all pass.
    monkeypatch.setattr(runmod, "_check_health", lambda url: None)
    monkeypatch.setattr(runmod, "_check_dataset", lambda: None)
    monkeypatch.setattr(runmod, "_ollama_models", lambda url: ["qwen3:14b", "mxbai-embed-large"])
    monkeypatch.setattr(runmod, "_free_gb", lambda: 100.0)
    fails = runmod.preflight("http://c", "http://o", "minimax-m2:cloud", skip_qa=False)
    assert any("cloud" in f for f in fails)


def test_preflight_skips_reader_check_when_qa_skipped(monkeypatch):
    monkeypatch.setattr(runmod, "_check_health", lambda url: None)
    monkeypatch.setattr(runmod, "_check_dataset", lambda: None)
    monkeypatch.setattr(runmod, "_ollama_models", lambda url: ["mxbai-embed-large"])
    monkeypatch.setattr(runmod, "_free_gb", lambda: 100.0)
    fails = runmod.preflight("http://c", "http://o", "qwen3:14b", skip_qa=True)
    assert fails == []


def test_preflight_requires_embed_model(monkeypatch):
    monkeypatch.setattr(runmod, "_check_health", lambda url: None)
    monkeypatch.setattr(runmod, "_check_dataset", lambda: None)
    monkeypatch.setattr(runmod, "_ollama_models", lambda url: [])
    monkeypatch.setattr(runmod, "_free_gb", lambda: 100.0)
    fails = runmod.preflight("http://c", "http://o", "qwen3:14b", skip_qa=True)
    assert any("mxbai-embed-large" in f for f in fails)
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_run.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `bench/run.py`**

Real code required; the structure:
- `_check_health(url)`: `httpx.get(f"{url}/health", timeout=10).raise_for_status()`; raise `RuntimeError` with a "is the bench stack up? docker compose -f docker-compose.bench.yml -p firekeep-bench up -d" hint on failure.
- `_check_dataset()`: both `data/longmemeval_s.json` and `data/dataset_meta.json` exist, else raise with "run: python -m bench.download".
- `_ollama_models(url)`: `GET {url}/api/tags` → `[m["name"] for m in resp["models"]]`; on failure return `[]` (preflight then reports both models missing with a "is Ollama running?" hint).
- `_free_gb()`: `shutil.disk_usage(WORK_DIR.anchor).free / 2**30`.
- `preflight(...)`: assemble the failure list per the interface above; `refuse_cloud` from `bench.qa` for the cloud check (report as a failure string, not an exception).
- `run(argv)`: argparse mirroring the earlier CLIs (`--base-url`, `--ollama-url`, `--limit`, `--config`, `--skip-qa`, `--run-label`, `--concurrency`); load rows once; call `ingest.ingest`, `recall.run_recall` per config, `score_retrieval.score_run` per config (k = that config's `top_k`), `qa.run_qa` when not skipped, then `report`'s assembly with the run label. Print each stage's stats line. Return 0 on success, 1 if preflight failed or any stage raised.

- [ ] **Step 4: Run tests, verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_run.py -v` then the whole suite `.venv/Scripts/python -m pytest tests/ -v`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/memory/bench/run.py benchmarks/memory/tests/test_run.py
git commit -m "feat(bench): orchestrator with pre-flight checks and stage resume"
```

---

### Task 9: Smoke run, README, wire-up verification

**Files:**
- Create: `benchmarks/memory/README.md`
- Modify: none (this task verifies; fixes discovered here become their own commits)

**Steps:**

- [ ] **Step 1: Download the dataset**

Run: `.venv/Scripts/python -m bench.download`
Expected: `dataset_meta.json` printed with a question count near 500. If the HF path drifted, fix `HF_REPO`/`HF_FILE` per the LongMemEval GitHub README and commit that fix.

- [ ] **Step 2: Pull models on the host**

Run: `ollama pull mxbai-embed-large` and `ollama pull qwen3:14b`
(If `qwen3:14b` does not exist in the Ollama library, pick the strongest available ~14B text model that fits 16 GB VRAM — check `ollama list`/library — and pass it via `--model`; record whatever was used, the report captures it from `/api/tags`.)

- [ ] **Step 3: Bring up the bench stack** (Task 2 commands) and re-run the Task 2 Step 4 tag round-trip probe.

- [ ] **Step 4: End-to-end smoke**

Run: `.venv/Scripts/python -m bench.run --limit 2 --config both --run-label smoke`
Expected: every stage prints a summary; `results/` gains a smoke JSON + METHODOLOGY.md; retrieval scores are nonzero for at least the non-abstention question. Inspect `work/recall_bench.jsonl` by eye: hits must carry real `session_id` values (the join is alive on real data, not just in the probe).

- [ ] **Step 5: Write `README.md`**

Contents (real prose, not an outline): hardware prerequisites (GPU Ollama, ~10 GB disk, models to pull); the exact five-command reproduction sequence (venv install → `bench.download` → compose up → `bench.run --limit 2` smoke → full `bench.run --run-label full`); expected full-run duration (state the measured smoke per-question ingest time × 500 as the estimate); where results land; the resumability story (re-run the same command after any interruption); and a pointer to the spec + generated METHODOLOGY.md for metric definitions.

- [ ] **Step 6: Commit**

```bash
git add benchmarks/memory/README.md
git commit -m "docs(bench): README with reproduction sequence and smoke-run gate"
```

- [ ] **Step 7: Full run (operator action, not CI)**

Run: `.venv/Scripts/python -m bench.run --config both --run-label full-v1`
This is hours-scale (ingest dominates). It is resumable; run overnight. Commit the `results/` artifacts when done:
```bash
git add benchmarks/memory/results/
git commit -m "results(bench): first full LongMemEval-S run"
```

---

## Self-Review (performed at plan time)

- **Spec coverage:** dataset fetch+checksum (T1), isolated stack + flags + RATE_LIMIT (T2), turn-pair ingest + tags join + ledger (T3), two config rows per-request (T4), all four retrieval metrics + per-type breakdown + abstention/error accounting (T5), local QA + judge + abstention scoring + cloud refusal (T6), results JSON + METHODOLOGY with mandated caveats (T7), orchestrator + pre-flight (T8), smoke gate + README + full run (T9). Spec's "fallback in-text header" contingency was retired at plan time: Task 2 Step 4 proves the tags mechanism against the live stack before any dependent code runs, and hard-stops if it fails.
- **Type consistency:** `Ledger` (T3) is consumed by T5's `score_run` with the same `memories_per_session(namespace) -> dict[str, int]` signature; recall JSONL rows (T4) are read by T5 (`hits`, `error`) and T6 (`hits`, `error`) with matching keys; `CONFIGS` names (`defaults`/`bench`) are the file-suffix vocabulary everywhere.
- **Placeholder scan:** T7 Step 3 and T8 Step 3 specify structure-with-notes rather than full listings — deliberate: both are mechanical assembly of interfaces fully defined in this plan (every function name, key, and mandated string is stated). No TBDs remain.
