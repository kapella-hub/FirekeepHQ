"""Where Hands keeps its state, all under one directory the kit resolver
locates for us: `resolver._config_path().parent / "hands"`.

Riding the kit's own config-path resolution (rather than reading
`FIREKEEP_CONFIG` or `~/.firekeep` ourselves) means Hands automatically lives
next to whatever `~/.firekeep` the kit resolved for this process — including
under `FIREKEEP_CONFIG` overrides in tests — with no separate override to keep
in sync.
"""
from __future__ import annotations

from pathlib import Path

from firekeep_client import resolver


def hands_home() -> Path:
    home = resolver._config_path().parent / "hands"
    home.mkdir(parents=True, exist_ok=True)
    return home


def config_path() -> Path:
    return hands_home() / "config.json"


def policy_path() -> Path:
    return hands_home() / "policy.json"


def broker_info_path() -> Path:
    return hands_home() / "broker.json"


def machine_id_path() -> Path:
    return hands_home() / "machine_id"


def evidence_root() -> Path:
    return hands_home() / "evidence"


def chrome_profile_dir() -> Path:
    return hands_home() / "chrome-profile"
