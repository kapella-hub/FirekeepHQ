"""Fetch LongMemEval-S from HuggingFace into data/, checksum + verify."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import hf_hub_download

from bench.common import DATA_DIR, load_dataset

# xiaowu0162/longmemeval (file longmemeval_s.json) 404s as of 2026-08-03 — the
# dataset was moved to the "-cleaned" repo/filename below (confirmed against
# the LongMemEval GitHub README's own wget instructions). LOCAL_FILE is the
# canonical on-disk name every other bench module (ingest/recall/qa/score/run)
# reads via `DATA_DIR / "longmemeval_s.json"`; keep it stable even if the
# upstream repo/filename drifts again — rename after fetch instead of
# threading a new filename through the rest of the harness.
HF_REPO = "xiaowu0162/longmemeval-cleaned"
HF_FILE = "longmemeval_s_cleaned.json"
LOCAL_FILE = "longmemeval_s.json"


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / LOCAL_FILE
    if target.exists():
        print(f"already present: {target}")
    else:
        fetched = hf_hub_download(
            repo_id=HF_REPO, filename=HF_FILE, repo_type="dataset",
            local_dir=str(DATA_DIR),
        )
        fetched_path = Path(fetched)
        if fetched_path.name != LOCAL_FILE:
            fetched_path.rename(target)
        print(f"downloaded: {fetched} -> {target}")

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
