"""Orchestrator: pre-flight checks, then ingest -> recall -> score -> qa -> report.

Each stage is resumable on its own (ledger for ingest, done-sets for recall/qa,
idempotent score/report writes), so a failed stage can be re-run with the same
command once the underlying problem is fixed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

import httpx

from bench import ingest, qa, recall, report, score_retrieval
from bench.common import (
    DATA_DIR,
    RESULTS_DIR,
    WORK_DIR,
    legacy_unscoped_artefacts,
    load_dataset,
    run_work_dir,
)

_COMPOSE_HINT = (
    "is the bench stack up? "
    "docker compose -f docker-compose.bench.yml -p firekeep-bench up -d"
)


def _check_health(url: str) -> None:
    try:
        httpx.get(f"{url}/health", timeout=10).raise_for_status()
    except Exception as exc:
        raise RuntimeError(f"cortex health check failed ({exc}) — {_COMPOSE_HINT}") from exc


def _check_dataset() -> None:
    dataset = DATA_DIR / "longmemeval_s.json"
    meta = DATA_DIR / "dataset_meta.json"
    if not dataset.exists() or not meta.exists():
        raise RuntimeError(
            "dataset not found in data/ (longmemeval_s.json + dataset_meta.json) — "
            "run: python -m bench.download"
        )


def _ollama_models(url: str) -> list[str]:
    try:
        resp = httpx.get(f"{url}/api/tags", timeout=10)
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]
    except Exception:
        return []


def _free_gb() -> float:
    return shutil.disk_usage(WORK_DIR.anchor).free / 2**30


def _model_available(name: str, models: list[str]) -> bool:
    """`ollama pull <name>` with no explicit tag lists as '<name>:latest' in
    /api/tags — a bare requested name (no ':') must also match that implicit
    tag, or every untagged pull fails preflight despite being fully usable."""
    if name in models:
        return True
    return ":" not in name and f"{name}:latest" in models


def preflight(cortex_url: str, ollama_url: str, reader_model: str, skip_qa: bool) -> list[str]:
    """Return human-readable failures; empty list means go."""
    fails: list[str] = []

    try:
        _check_health(cortex_url)
    except RuntimeError as exc:
        fails.append(str(exc))

    try:
        _check_dataset()
    except RuntimeError as exc:
        fails.append(str(exc))

    models = _ollama_models(ollama_url)
    if not _model_available("mxbai-embed-large", models):
        fails.append(
            "embedding model 'mxbai-embed-large' not found in Ollama tags "
            f"({ollama_url}/api/tags) — is Ollama running? "
            "(ollama pull mxbai-embed-large)"
        )

    if not skip_qa:
        if not _model_available(reader_model, models):
            fails.append(
                f"reader model {reader_model!r} not found in Ollama tags "
                f"({ollama_url}/api/tags) — is Ollama running? "
                f"(ollama pull {reader_model})"
            )
        try:
            qa.refuse_cloud(reader_model)
        except ValueError as exc:
            fails.append(str(exc))

    free_gb = _free_gb()
    if free_gb < 5:
        fails.append(
            f"only {free_gb:.1f} GB free on the drive holding work/ — need >= 5 GB"
        )

    return fails


def _persist_ingest_errors(errors: list[str]) -> None:
    """Ingest failures must reach the published record, not just stdout (I3)
    — report._load_ingest_errors reads this file back at report time."""
    (WORK_DIR / "ingest_errors.json").write_text(
        json.dumps(errors, indent=2), encoding="utf-8")


def _int(value, default: int = 0) -> int:
    """Read a count off disk without trusting it. A hand-edited or truncated
    counts file must degrade to 0, never crash the run that is writing it."""
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _record_recall_counts(work: Path, config_name: str, stats) -> dict:
    """Persist what the recall stage actually DID, per config, per run label.

    Two figures, because they answer two different questions and only one of
    them can gate:

    - `completed`/`errored` ACCUMULATE across invocations of the same label. A
      4-hour leg is expected to be resumed, and the cumulative figure is what
      answers "did this label ever recall anything" — useful provenance.
    - `completed_last_invocation` is what the FINAL invocation executed, and it
      is what `bench.dream_ab` gates on. The cumulative figure cannot: run a
      leg, enable dreaming, re-run the identical command, and the second
      invocation skips all 500 questions while still reporting the first
      invocation's large `completed` — a no-op certified as evidence. The two
      cases separate mechanically here: a genuine resume's final invocation
      completes the remaining questions (> 0), a no-op re-run completes 0.

    `skipped` is already the last invocation's only: skips are per-invocation
    by nature and summing them would double-count the same questions.
    """
    counts = report.load_recall_counts(work)
    prev = counts.get(config_name)
    prev = prev if isinstance(prev, dict) else {}
    entry = {
        "completed": _int(prev.get("completed")) + stats.completed,
        "errored": _int(prev.get("errored")) + stats.errored,
        "skipped": stats.skipped,
        "invocations": _int(prev.get("invocations")) + 1,
        "completed_last_invocation": stats.completed,
    }
    counts[config_name] = entry
    work.mkdir(parents=True, exist_ok=True)
    (work / "recall_counts.json").write_text(
        json.dumps(counts, indent=2), encoding="utf-8")
    return entry


def _warn_legacy_artefacts() -> None:
    """Pre-scoping artefacts sit directly in `work/` and belong to no label.

    They are never adopted — adopting them silently is precisely the defect
    label-scoping fixes (a leg that recalls nothing and re-scores an older
    leg's rows). They are also never moved or deleted, because another run may
    hold them open. The operator is told they exist and how to claim them.
    """
    stale = legacy_unscoped_artefacts(WORK_DIR)
    if not stale:
        return
    print(
        "[work] NOTE: unscoped (pre-label) artefacts found in work/ — they are "
        "NOT read, because which run produced them is unknowable:"
    )
    for path in stale:
        print(f"  - {path.name}")
    print(
        "[work]   To resume one of them under a label, move it yourself into "
        "work/<run-label>/ (and only if you are certain that label produced it)."
    )


def _assemble_report(run_label: str, cortex_url: str, ollama_url: str, reader_model: str) -> None:
    work = run_work_dir(run_label, work_dir=WORK_DIR)

    scores = {}
    for name in recall.CONFIGS:
        path = work / f"scores_{name}.json"
        if path.exists():
            scores[name] = json.loads(path.read_text(encoding="utf-8"))
    # The positive control: what the recall stage actually executed for each
    # config, carried inside the same dict `bench.dream_ab` compares — so a leg
    # that recalled nothing cannot pass the gate. Shared with the standalone
    # `python -m bench.report` path, which must stamp the identical block.
    report.attach_recall_counts(scores, work)

    qa_result = None
    qa_path = work / "qa_bench.jsonl"
    if qa_path.exists():
        qa_rows = [
            json.loads(line) for line in
            qa_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        if qa_rows:
            qa_result = report.qa_accuracy(qa_rows)

    meta = report._load_meta(cortex_url, ollama_url, reader_model)
    ingest_errors = report._load_ingest_errors()
    per_question = report._load_per_question(list(scores), work_dir=work)
    result = report.build_result(
        scores, qa_result, meta,
        ingest_errors=ingest_errors, per_question=per_question,
    )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    # Sanitized, not raw: `--run-label a/b` used to reach this line after
    # ingest, a full recall stage and scoring — hours on the real dataset —
    # and die on `No such file or directory`. See `report.results_path`.
    out_json = report.results_path(run_label, RESULTS_DIR)
    out_json.write_text(json.dumps(result, indent=2), encoding="utf-8")

    out_md = RESULTS_DIR / "METHODOLOGY.md"
    out_md.write_text(report.render_methodology(result), encoding="utf-8")

    print(report.render_markdown(result))
    print(f"[report] -> {out_json}")
    print(f"[report] -> {out_md}")


def _build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m bench.run")
    ap.add_argument("--base-url", default="http://127.0.0.1:18100")
    ap.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--config", choices=[*recall.CONFIGS, "both"], default="both")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--skip-qa", action="store_true")
    ap.add_argument("--run-label", default="bench")
    ap.add_argument("--reader-model", default=qa.READER_MODEL)
    return ap


def run(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)

    names = list(recall.CONFIGS) if args.config == "both" else [args.config]

    # QA always reads the bench config's recall output (raw, untrimmed context);
    # the report unconditionally labels that row "bench config" QA, so running
    # QA against a different config's recall would mislabel a published
    # artifact. Reject before preflight, before anything is touched.
    if not args.skip_qa and "bench" not in names:
        print(
            "QA requires the bench config: use --config bench/both, or pass --skip-qa"
        )
        return 1

    fails = preflight(args.base_url, args.ollama_url, args.reader_model, args.skip_qa)
    if fails:
        print("PRE-FLIGHT FAILED:")
        for f in fails:
            print(f" - {f}")
        return 1
    print("[preflight] ok")

    rows = load_dataset(DATA_DIR / "longmemeval_s.json")
    if args.limit:
        rows = rows[: args.limit]

    work = run_work_dir(args.run_label, work_dir=WORK_DIR)
    work.mkdir(parents=True, exist_ok=True)
    print(f"[work] run artefacts -> {work}")
    _warn_legacy_artefacts()

    # The ingest ledger is deliberately SHARED and unscoped, unlike every other
    # artefact below. The store is the fixture under test: both legs of an A/B
    # must run against the SAME ingested corpus, and ingest is idempotent by
    # ledger. Scoping it per label would re-ingest ~10 minutes of haystacks and,
    # far worse, mean the two legs no longer measured the same store — which
    # destroys the comparison that label-scoping the recall artefacts exists to
    # protect.
    ledger = ingest.Ledger(WORK_DIR / "ingest_ledger.jsonl")
    try:
        ingest_stats = asyncio.run(ingest.ingest(
            rows, args.base_url, concurrency=args.concurrency, ledger=ledger,
            progress=True,
        ))
    except Exception as exc:
        print(f"[ingest] FAILED: {exc}")
        return 1
    print(
        f"[ingest] done={ingest_stats.sessions_done} "
        f"skipped={ingest_stats.sessions_skipped} calls={ingest_stats.learn_calls} "
        f"errors={len(ingest_stats.errors)}"
    )
    for e in ingest_stats.errors[:20]:
        print("  ingest ERROR:", e)
    _persist_ingest_errors(ingest_stats.errors)

    recall_paths: dict[str, Path] = {}
    for name in names:
        out = work / f"recall_{name}.jsonl"
        try:
            stats = asyncio.run(recall.run_recall(
                rows, args.base_url, name, out, progress=True,
            ))
        except Exception as exc:
            print(f"[recall:{name}] FAILED: {exc}")
            return 1
        recall_paths[name] = out
        entry = _record_recall_counts(work, name, stats)
        print(
            f"[recall:{name}] completed={stats.completed} skipped={stats.skipped} "
            f"errored={stats.errored} -> {out}"
        )
        print(
            f"[recall:{name}] label totals: completed={entry['completed']} "
            f"errored={entry['errored']} invocations={entry['invocations']} "
            f"(this invocation completed={entry['completed_last_invocation']}"
            f" — the figure bench.dream_ab gates on)"
        )

    for name in names:
        k = recall.CONFIGS[name]["top_k"]
        try:
            result = score_retrieval.score_run(rows, recall_paths[name], ledger, k)
        except Exception as exc:
            print(f"[score:{name}] FAILED: {exc}")
            return 1
        out = work / f"scores_{name}.json"
        out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        overall = result["overall"]
        print(
            f"[score:{name}] n={overall['n']} recall@k={overall['recall_at_k']:.3f} "
            f"mrr={overall['mrr']:.3f} -> {out}"
        )

    if not args.skip_qa:
        # Guarded above: skip_qa=False implies "bench" is in names/recall_paths.
        qa_out = work / "qa_bench.jsonl"
        try:
            qa_stats = asyncio.run(qa.run_qa(
                rows, recall_paths["bench"], qa_out,
                base_url=args.ollama_url, model=args.reader_model, progress=True,
            ))
        except Exception as exc:
            print(f"[qa] FAILED: {exc}")
            return 1
        print(
            f"[qa] answered={qa_stats.answered} skipped={qa_stats.skipped} "
            f"judge_errors={qa_stats.judge_errors} -> {qa_out}"
        )

    try:
        _assemble_report(args.run_label, args.base_url, args.ollama_url, args.reader_model)
    except Exception as exc:
        print(f"[report] FAILED: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
