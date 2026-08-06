#!/usr/bin/env python3
"""Mint the Firekeep release signing identity (run OFFLINE — docs/RELEASE-SIGNING.md).

Writes two minisign-format files into the target directory (refusing to overwrite):

  firekeep-signing.pub  — the public key. Goes into firekeep_client/signing.py's
                          PINNED_PUBLIC_KEY (commit it) and is published by CI as
                          latest/signing.pub for transparency.
  firekeep-signing.key  — the UNENCRYPTED secret key. Its file content becomes the
                          FIREKEEP_SIGNING_KEY CI secret. Keep the original offline
                          (password manager / offline media); it is unencrypted
                          because the CI secret store is the encryption layer.

Equivalent standard tooling: `minisign -G -W -p firekeep-signing.pub -s firekeep-signing.key`
produces interchangeable files. This script exists so keygen needs nothing but the
checkout's own stdlib-only code, and so the formats are exercised by the same module
that verifies them on every developer machine.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from firekeep_client import signing  # noqa: E402


def _write_secret(path: Path, text: str) -> None:
    """Create the secret key file 0600 ATOMICALLY — mode at open, not chmod after.

    write_text-then-chmod leaves a window where the file exists with the umask's
    default (often world-readable) holding the signing secret; O_EXCL also makes
    the pre-existence refusal race-free rather than a check-then-act. On Windows
    the mode bits are largely inert — the directory choice is the protection
    there, as before."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def main(argv: list[str]) -> int:
    out_dir = Path(argv[1]) if len(argv) > 1 else Path(".")
    out_dir.mkdir(parents=True, exist_ok=True)
    pub_path = out_dir / "firekeep-signing.pub"
    key_path = out_dir / "firekeep-signing.key"
    for path in (pub_path, key_path):
        if path.exists():
            raise SystemExit(f"{path} already exists — refusing to overwrite a signing key")
    pub_text, secret_text = signing.generate_keypair()
    pub_path.write_text(pub_text, encoding="utf-8", newline="\n")
    try:
        _write_secret(key_path, secret_text)
    except FileExistsError:
        raise SystemExit(f"{key_path} already exists — refusing to overwrite a signing key")
    key = signing.parse_secret_key(secret_text)
    print(f"minted signing key {signing.key_id_hex(key.key_id)}")
    print(f"  public : {pub_path}")
    print(f"  secret : {key_path}  (UNENCRYPTED — handle per docs/RELEASE-SIGNING.md)")
    print("next steps: docs/RELEASE-SIGNING.md §'Enabling signing'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
