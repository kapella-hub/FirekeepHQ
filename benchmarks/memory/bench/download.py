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
