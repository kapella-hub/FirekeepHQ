"""FIREKEEP_SESSION_REPORT_FILE (Studio 0.5, spec §7.2): the bridge tap appends
every Keep session id it captures so Studio can find the agent's sessions."""
import pytest

from firekeep_client import state


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv(state.SESSION_REPORT_ENV, raising=False)
    return tmp_path


def test_append_and_read_back(cache, monkeypatch):
    report = cache / "s1.keep"
    monkeypatch.setenv(state.SESSION_REPORT_ENV, str(report))
    state.append_session_report("sess-a")
    state.append_session_report("sess-b")
    assert report.read_text(encoding="utf-8") == "sess-a\nsess-b\n"
    assert state.reported_session_id() == "sess-b"


def test_no_report_env_means_no_report_file(cache):
    state.append_session_report("sess-a")
    assert state.reported_session_id() is None
    assert not list(cache.glob("**/*.keep"))


@pytest.mark.parametrize("bad", ["sess\nX-Evil: 1", "", " ", "a" * 200, "-leading-dash"])
def test_report_rejects_ids_that_could_split_a_header(cache, monkeypatch, bad):
    report = cache / "s1.keep"
    monkeypatch.setenv(state.SESSION_REPORT_ENV, str(report))
    state.append_session_report(bad)
    assert not report.exists() or report.read_text(encoding="utf-8") == ""


def test_reader_skips_garbage_lines(cache, monkeypatch):
    report = cache / "s1.keep"
    report.write_text("sess-a\nnot valid id!\n\n", encoding="utf-8")
    monkeypatch.setenv(state.SESSION_REPORT_ENV, str(report))
    assert state.reported_session_id() == "sess-a"


def test_unwritable_report_path_never_raises(cache, monkeypatch):
    monkeypatch.setenv(state.SESSION_REPORT_ENV, str(cache / "no" / "such" / "dir" / "x.keep"))
    state.append_session_report("sess-a")  # must not raise
    assert state.reported_session_id() is None


def test_current_session_prefers_fresh_stash(cache, monkeypatch):
    report = cache / "s1.keep"
    report.write_text("sess-reported\n", encoding="utf-8")
    monkeypatch.setenv(state.SESSION_REPORT_ENV, str(report))
    state.write_session_stash("tester", session_id="sess-stash")
    assert state.current_session_id("tester") == "sess-stash"


def test_current_session_falls_back_to_report(cache, monkeypatch):
    report = cache / "s1.keep"
    report.write_text("sess-reported\n", encoding="utf-8")
    monkeypatch.setenv(state.SESSION_REPORT_ENV, str(report))
    state.clear_session_stash("tester")
    assert state.current_session_id("tester") == "sess-reported"


def test_current_session_none_without_either(cache):
    state.clear_session_stash("tester")
    assert state.current_session_id("tester") is None


def test_end_session_removes_from_report(cache, monkeypatch):
    report = cache / "s1.keep"
    monkeypatch.setenv(state.SESSION_REPORT_ENV, str(report))
    state.append_session_report("sess-a")
    assert state.reported_session_id() == "sess-a"
    state.end_session_report("sess-a")
    assert state.reported_session_id() is None


def test_end_session_preserves_later_ids(cache, monkeypatch):
    report = cache / "s1.keep"
    monkeypatch.setenv(state.SESSION_REPORT_ENV, str(report))
    state.append_session_report("sess-a")
    state.append_session_report("sess-b")
    state.end_session_report("sess-a")
    assert state.reported_session_id() == "sess-b"


def test_end_session_does_not_affect_other_ids(cache, monkeypatch):
    report = cache / "s1.keep"
    monkeypatch.setenv(state.SESSION_REPORT_ENV, str(report))
    state.append_session_report("sess-a")
    state.append_session_report("sess-b")
    state.end_session_report("sess-a")
    assert report.read_text(encoding="utf-8") == "sess-a\nsess-b\n#ended sess-a\n"
    assert state.reported_session_id() == "sess-b"


def test_reader_handles_non_utf8_bytes(cache, monkeypatch):
    report = cache / "s1.keep"
    # Write a file with a non-UTF-8 byte
    with open(report, "wb") as fh:
        fh.write(b"sess-a\n\xff\nsess-b\n")
    monkeypatch.setenv(state.SESSION_REPORT_ENV, str(report))
    assert state.reported_session_id() == "sess-b"
