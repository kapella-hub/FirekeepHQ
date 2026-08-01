"""Tests for firekeep_client.hooklog — fail-loud failure logging (SP0 D6).

log_failure must be best-effort and NEVER raise, even when the target
directory cannot be created/written to. FIREKEEP_LOG_DIR is read dynamically
(per-call), not cached at import time, so tests can retarget it via
monkeypatch without reimporting the module.
"""
from __future__ import annotations

import re

from firekeep_client import hooklog

_ISO_LINE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \| pre_tool \| something failed$"
)


def test_log_failure_writes_iso8601_line(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(tmp_path))

    hooklog.log_failure("pre_tool", "something failed")

    log_file = tmp_path / "hooks.log"
    assert log_file.exists()
    line = log_file.read_text(encoding="utf-8").strip()
    assert _ISO_LINE_RE.match(line), line


def test_log_failure_appends_multiple_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(tmp_path))

    hooklog.log_failure("pre_tool", "first")
    hooklog.log_failure("post_tool", "second")

    lines = (tmp_path / "hooks.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("| pre_tool | first")
    assert lines[1].endswith("| post_tool | second")


def test_log_failure_reads_env_dynamically_not_at_import(tmp_path, monkeypatch):
    # No env set at import time (module already imported above); setting it
    # now, right before the call, must still redirect the write target.
    monkeypatch.delenv("FIREKEEP_LOG_DIR", raising=False)
    target = tmp_path / "late-dir"
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(target))

    hooklog.log_failure("hook", "msg")

    assert (target / "hooks.log").exists()


def test_log_failure_strips_newlines_from_hook_and_message(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(tmp_path))

    hooklog.log_failure("pre_tool\ninjected", "line1\nline2\r\nline3")

    line = (tmp_path / "hooks.log").read_text(encoding="utf-8").strip()
    assert "\n" not in line
    assert "\r" not in line
    assert line.count("hooks.log") == 0  # sanity: no accidental self-reference
    assert "pre_tool injected" in line
    assert "line1 line2  line3" in line


def test_log_failure_caps_length(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(tmp_path))

    hooklog.log_failure("h" * 1000, "m" * 5000)

    line = (tmp_path / "hooks.log").read_text(encoding="utf-8").strip()
    # Line must be capped well below the uncapped 1000+5000+separators length.
    assert len(line) < 1000 + 5000


def test_log_failure_never_raises_on_unwritable_dir(tmp_path, monkeypatch):
    # Point FIREKEEP_LOG_DIR at a path that can never become a directory: a file
    # sitting where a directory needs to be created.
    blocker = tmp_path / "blocker-file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(blocker / "nested"))

    # Must not raise.
    hooklog.log_failure("pre_tool", "should not blow up")


def test_log_failure_never_raises_on_non_string_inputs(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(tmp_path))

    # Must not raise even with unexpected types.
    hooklog.log_failure(None, {"not": "a string"})  # type: ignore[arg-type]


def test_log_path_default_is_under_home_dot_firekeep_logs():
    assert hooklog.LOG_PATH.name == "hooks.log"
    assert hooklog.LOG_PATH.parent.name == "logs"
    assert hooklog.LOG_PATH.parent.parent.name == ".firekeep"


def test_log_failure_ignores_retired_profile_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("FIREKEEP_PROFILE", "office")
    from firekeep_client import hooklog
    hooklog.log_failure("prompt", "boom")
    line = (tmp_path / "hooks.log").read_text().strip()
    assert line.endswith("| prompt | boom")
    assert "profile=" not in line


def test_log_failure_untagged_without_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("FIREKEEP_PROFILE", raising=False)
    from firekeep_client import hooklog
    hooklog.log_failure("prompt", "boom")
    assert "profile=" not in (tmp_path / "hooks.log").read_text()
