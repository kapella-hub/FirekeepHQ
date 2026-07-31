from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from auth.entitlements import verify_licence


ROOT = Path(__file__).resolve().parents[2]


def test_offline_tool_generates_key_and_mints_verifiable_team_licence(tmp_path):
    private = tmp_path / "signing.key"
    generated = subprocess.run(
        [
            sys.executable,
            "-m",
            "deploy.licence_tool",
            "keygen",
            "--private-key",
            str(private),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    public = generated.stdout.split("FIREKEEP_LICENCE_PUBLIC_KEY=", 1)[1].splitlines()[0]
    minted = subprocess.run(
        [
            sys.executable,
            "-m",
            "deploy.licence_tool",
            "mint",
            "--private-key",
            str(private),
            "--workspace-id",
            "workspace-test",
            "--customer",
            "Acme",
            "--plan",
            "team",
            "--max-members",
            "5",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    entitlement = verify_licence(
        minted.stdout.strip(),
        "workspace-test",
        public_key=public,
    )
    assert entitlement.verified is True
    assert entitlement.max_members == 5
    if os.name != "nt":
        assert private.stat().st_mode & 0o077 == 0
