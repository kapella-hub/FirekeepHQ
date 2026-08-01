"""The leak guard must actually be able to fail.

This is not ceremony. The first version of this scan was a shell loop whose
failure flag was set inside a `$(...)` subshell; it printed PASS while a
forbidden token sat in the tree. A check that cannot report failure is worse
than no check, because it manufactures confidence.

Every test here plants a token and asserts the scanner reports it.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from check_forbidden_tokens import FORBIDDEN, scan  # noqa: E402


@pytest.mark.parametrize("token", sorted(FORBIDDEN))
def test_every_forbidden_token_is_actually_detected(tmp_path, token):
    """The load-bearing test: each token, planted, must be found."""
    (tmp_path / "seeded.py").write_text(
        f'URL = "https://{token}/whatever"\n', encoding="utf-8"
    )
    hits = scan(tmp_path)
    assert hits, f"scanner did not detect the planted token {token!r}"
    assert any(h[2] == token for h in hits), f"wrong token reported for {token!r}"


@pytest.mark.parametrize("token", sorted(FORBIDDEN))
def test_detection_is_case_insensitive(tmp_path, token):
    (tmp_path / "seeded.md").write_text(f"See {token.upper()} for details\n", encoding="utf-8")
    assert scan(tmp_path), f"{token!r} missed when upper-cased"


def test_clean_tree_reports_no_hits(tmp_path):
    """The other half: it must not fire on innocent content."""
    (tmp_path / "ok.py").write_text(
        'NAME = "Firekeep"\nHOST = "firekeep.office.example"\nX = "203.0.113.10"\n',
        encoding="utf-8",
    )
    assert scan(tmp_path) == []


def test_rfc2606_example_hostnames_are_not_flagged(tmp_path):
    """.example is reserved for documentation — a legitimate placeholder, and a
    false positive here would train people to ignore the guard."""
    (tmp_path / "cfg.ini").write_text(
        "base_url = https://firekeep.office.example/mcp/cortex\n", encoding="utf-8"
    )
    assert scan(tmp_path) == []


def test_reports_file_and_line(tmp_path):
    (tmp_path / "a.txt").write_text("clean\nclean\nnexusstack here\n", encoding="utf-8")
    hits = scan(tmp_path)
    assert hits[0][0] == "a.txt"
    assert hits[0][1] == 3, "must report the real line number for a usable error"


def test_binary_files_do_not_crash_the_scan(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02\xff\xfe")
    (tmp_path / "ok.py").write_text("clean\n", encoding="utf-8")
    assert scan(tmp_path) == []


def test_nested_agent_worktrees_are_not_release_material(tmp_path):
    worktree = tmp_path / ".claude" / "worktrees" / "feature" / "legacy.md"
    worktree.parent.mkdir(parents=True)
    worktree.write_text("nexusstack\n", encoding="utf-8")
    assert scan(tmp_path) == []


def test_the_scanner_source_itself_is_skipped(tmp_path):
    """It names every token by definition; scanning it would always fail."""
    from check_forbidden_tokens import SKIP_FILES

    assert "scripts/check_forbidden_tokens.py" in SKIP_FILES
    assert "tests/test_forbidden_tokens.py" in SKIP_FILES


def test_the_real_repository_is_clean():
    """The actual assertion this exists for."""
    repo = Path(__file__).resolve().parents[1]
    hits = scan(repo)
    assert hits == [], "forbidden material in the tree:\n" + "\n".join(
        f"  {r}:{ln} '{t}' — {why}" for r, ln, t, why in hits[:20]
    )
