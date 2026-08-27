# cortex/tests/test_identity_guard.py
"""No bare-text uuid5 derivation may exist outside the helper + migration
tooling (identity-v2 D2). A re-derivation is a silent identity fork."""
import re
from pathlib import Path

ALLOWED = {"app/db/vector.py", "app/workers/memory_identity_migration.py"}

def test_no_stray_uuid5_over_memory_text():
    root = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for py in root.rglob("*.py"):
        rel = py.relative_to(root.parent).as_posix()
        if rel in ALLOWED:
            continue
        src = py.read_text(encoding="utf-8")
        if re.search(r"uuid5\(\s*FIREKEEP_UUID_NAMESPACE", src):
            offenders.append(rel)
    assert not offenders, f"identity forked outside the helper: {offenders}"
