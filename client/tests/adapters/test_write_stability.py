"""Cache integrity: a re-render that changes nothing must touch nothing.

`firekeep update` re-execs `firekeep install`, and background auto-update is ON
by default — so a customer's `~/.claude/CLAUDE.md` and `~/.claude/settings.json`
get re-rendered MID-SESSION. Those files sit in the prompt prefix. If a host
re-reads a rendered instruction file because its mtime moved, the prefix is
rebuilt and the customer's prompt cache is invalidated, re-billing the whole
conversation at full rate — for a zero-byte change.

Whether any given host re-reads on mtime cannot be determined from this repo.
That is precisely why touching mtime for no content change is indefensible.

These tests also serve as the byte-stability guard on the rendered surface: two
independent renders must produce identical bytes (no timestamps, uuids, or
unsorted iteration creeping into an adapter).
"""

from __future__ import annotations

import json
import os

import pytest

from firekeep_client.adapters import get_adapter
from firekeep_client.adapters.base import write_json, write_text_if_changed

_OLD = 1_600_000_000  # a fixed mtime in the past; no sleep needed


def _age(path):
    """Backdate a file so any rewrite is unmistakable."""
    os.utime(path, (_OLD, _OLD))


def _mtime(path):
    return path.stat().st_mtime


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# --- the primitive ---------------------------------------------------------


def test_write_text_if_changed_creates_a_missing_file(tmp_path):
    p = tmp_path / "nested" / "f.txt"
    assert write_text_if_changed(p, "hello") is True
    assert p.read_text(encoding="utf-8") == "hello"


def test_write_text_if_changed_skips_identical_content(tmp_path):
    p = tmp_path / "f.txt"
    write_text_if_changed(p, "hello")
    _age(p)
    assert write_text_if_changed(p, "hello") is False
    assert _mtime(p) == _OLD          # not rewritten
    assert p.read_text(encoding="utf-8") == "hello"


def test_write_text_if_changed_writes_when_content_differs(tmp_path):
    p = tmp_path / "f.txt"
    write_text_if_changed(p, "hello")
    _age(p)
    assert write_text_if_changed(p, "goodbye") is True
    assert p.read_text(encoding="utf-8") == "goodbye"
    assert _mtime(p) != _OLD


def test_write_json_skips_an_identical_dict(tmp_path):
    p = tmp_path / "c.json"
    write_json(p, {"a": 1, "b": [2, 3]})
    _age(p)
    write_json(p, {"a": 1, "b": [2, 3]})
    assert _mtime(p) == _OLD


def test_write_json_still_writes_a_changed_dict(tmp_path):
    p = tmp_path / "c.json"
    write_json(p, {"a": 1})
    _age(p)
    write_json(p, {"a": 2})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 2}
    assert _mtime(p) != _OLD


def test_write_text_if_changed_overwrites_an_undecodable_file(tmp_path):
    """A file we cannot read must be written, not skipped: failing to compare is
    not evidence the content matches."""
    p = tmp_path / "f.txt"
    p.write_bytes(b"\xff\xfe\x00binary")
    assert write_text_if_changed(p, "text") is True
    assert p.read_text(encoding="utf-8") == "text"


# --- the property that actually bills the customer -------------------------


def test_second_identical_claude_render_touches_no_rendered_file(fake_home, tmp_path):
    venv_bin = tmp_path / "venv" / "Scripts"
    adapter = get_adapter("claude")
    adapter.render(venv_bin=venv_bin)

    rendered = [p for p in (
        fake_home / ".claude.json",
        fake_home / ".claude" / "settings.json",
        fake_home / ".claude" / "CLAUDE.md",
        fake_home / ".claude" / "commands" / "personal.md",
    ) if p.exists()]
    assert rendered, "render() produced no files — the fixture is wrong, not the code"

    before = {p: p.read_text(encoding="utf-8") for p in rendered}
    for p in rendered:
        _age(p)

    adapter.render(venv_bin=venv_bin)

    for p in rendered:
        assert p.read_text(encoding="utf-8") == before[p], f"{p.name} content drifted"
        assert _mtime(p) == _OLD, (
            f"{p.name} was rewritten with identical content — this invalidates the "
            "customer's prompt cache for nothing"
        )
