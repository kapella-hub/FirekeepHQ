"""Sign Studio's update manifest with Firekeep's pinned release identity."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from client.firekeep_client.signing import PINNED_PUBLIC_KEY, sign, verify  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: sign_update_manifest.py <studio-update.json>")
    manifest_path = Path(sys.argv[1]).resolve()
    data = manifest_path.read_bytes()
    manifest = json.loads(data)
    version = manifest.get("version")
    published_at = manifest.get("publishedAt")
    if not isinstance(version, str) or not isinstance(published_at, str):
        raise SystemExit("update manifest is missing version or publishedAt")
    secret_key = os.environ.get("FIREKEEP_SIGNING_KEY")
    if not secret_key:
        raise SystemExit("FIREKEEP_SIGNING_KEY is required; Studio never publishes an unsigned update channel")
    timestamp = int(datetime.fromisoformat(published_at.replace("Z", "+00:00")).timestamp())
    signature = sign(data, secret_key, trusted_comment=f"timestamp:{timestamp} version:{version}")
    verify(data, signature, PINNED_PUBLIC_KEY)
    manifest_path.with_suffix(f"{manifest_path.suffix}.minisig").write_text(signature, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
