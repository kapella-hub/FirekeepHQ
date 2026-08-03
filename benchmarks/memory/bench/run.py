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
from datetime import datetime, timezone
from pathlib import Path

import httpx

from bench import ingest, qa, recall, report, score_retrieval
from bench.common import DATA_DIR, RESULTS_DIR, WORK_DIR, load_dataset

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
    if "mxbai-embed-large" not in models:
        fails.append(
            "embedding model 'mxbai-embed-large' not found in Ollama tags "
            f"({ollama_url}/api/tags) — is Ollama running? "
            "(ollama pull mxbai-embed-large)"
        )

    if not skip_qa:
        if reader_model not in models:
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


def _assemble_report(run_label: str, cortex_url: str, ollama_url: str) -> None:
    scores = {}
    for name in recall.CONFIGS:
        path = WORK_DIR / f"scores_{name}.json"
        if path.exists():
            scores[name] = json.loads(path.read_text(encoding="utf-8"))

    qa_result = None
    qa_path = WORK_DIR / "qa_bench.jsonl"
    if qa_path.exists():
        qa_rows = [
            json.loads(line) for line in
            qa_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        if qa_rows:
            qa_result = report.qa_accuracy(qa_rows)

    meta = report._load_meta(cortex_url, ollama_url)
    result = report.build_result(scores, qa_result, meta)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_json = RESULTS_DIR / f"{timestamp}-{run_label}.json"
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

    recall_paths: dict[str, Path] = {}
    for name in names:
        out = WORK_DIR / f"recall_{name}.jsonl"
        try:
            stats = asyncio.run(recall.run_recall(
                rows, args.base_url, name, out, progress=True,
            ))
        except Exception as exc:
            print(f"[recall:{name}] FAILED: {exc}")
            return 1
        recall_paths[name] = out
        print(
            f"[recall:{name}] completed={stats.completed} skipped={stats.skipped} "
            f"errored={stats.errored} -> {out}"
        )

    for name in names:
        k = recall.CONFIGS[name]["top_k"]
        try:
            result = score_retrieval.score_run(rows, recall_paths[name], ledger, k)
        except Exception as exc:
            print(f"[score:{name}] FAILED: {exc}")
            return 1
        out = WORK_DIR / f"scores_{name}.json"
        out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        overall = result["overall"]
        print(
            f"[score:{name}] n={overall['n']} recall@k={overall['recall_at_k']:.3f} "
            f"mrr={overall['mrr']:.3f} -> {out}"
        )

    if not args.skip_qa:
        # Guarded above: skip_qa=False implies "bench" is in names/recall_paths.
        qa_out = WORK_DIR / "qa_bench.jsonl"
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
        _assemble_report(args.run_label, args.base_url, args.ollama_url)
    except Exception as exc:
        print(f"[report] FAILED: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
