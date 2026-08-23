"""Spool protocol (spec, 'Spool concurrency — claim by rename, with crash
recovery'): no rewrite anywhere, stale claims adopted, at-least-once with
ack-based truncation."""
import json
import os
import time

import pytest

from firekeep_client import report
from firekeep_client.transport import TransportError


@pytest.fixture(autouse=True)
def report_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_REPORT_DIR", str(tmp_path))
    monkeypatch.setenv("FIREKEEP_FAILURE_REPORT", "1")  # consent for tests
    monkeypatch.delenv("FIREKEEP_NO_FAILURE_REPORT", raising=False)
    return tmp_path


def _spool(report_dir):
    return report_dir / "report-spool.jsonl"


def _events(report_dir):
    p = _spool(report_dir)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_emit_spools_when_collector_down(report_dir, monkeypatch):
    def refuse(*a, **k):
        raise TransportError("refused", category="connection-refused")
    monkeypatch.setattr(report, "_post", refuse)
    report.emit("install", "create-venv", error="disk-full", exit_code=1)
    evs = _events(report_dir)
    assert len(evs) == 1 and evs[0]["error"] == "disk-full"


def test_emit_never_raises_on_garbage_collector(report_dir, monkeypatch):
    monkeypatch.setattr(report, "_post", lambda *a, **k: "not a dict")
    report.emit("install", "create-venv", error="other")  # must not raise
    assert len(_events(report_dir)) == 1  # unacked -> merged back


def test_flush_truncates_only_acked(report_dir, monkeypatch):
    for stage in ("create-venv", "pip-install-client", "add-to-path"):
        report._append_spool(report.build_event("install", stage, error="other"))
    ids = [e["id"] for e in _events(report_dir)]
    monkeypatch.setattr(report, "_post",
                        lambda url, body, timeout: {"accepted": ids[:2], "rejected": []})
    report.flush()
    left = _events(report_dir)
    assert [e["id"] for e in left] == [ids[2]]


def test_rejected_ids_are_dropped_not_retried(report_dir, monkeypatch):
    report._append_spool(report.build_event("install", "create-venv", error="other"))
    the_id = _events(report_dir)[0]["id"]
    monkeypatch.setattr(report, "_post",
                        lambda url, body, timeout: {"accepted": [], "rejected": [the_id]})
    report.flush()
    assert _events(report_dir) == []


def test_stale_claim_is_adopted(report_dir, monkeypatch):
    stale = report_dir / "report-spool.sending.99999"
    ev = report.build_event("install", "create-venv", error="other")
    stale.write_text(json.dumps(ev) + "\n")
    old = time.time() - report.STALE_CLAIM_SECONDS - 5
    os.utime(stale, (old, old))
    sent = []

    def ack_all(url, body, timeout):
        sent.extend(body["events"])
        return {"accepted": [e["id"] for e in body["events"]], "rejected": []}

    monkeypatch.setattr(report, "_post", ack_all)
    report.flush()
    assert [e["id"] for e in sent] == [ev["id"]]
    assert not stale.exists() and _events(report_dir) == []


def test_fresh_claim_not_adopted(report_dir, monkeypatch):
    fresh = report_dir / "report-spool.sending.88888"
    fresh.write_text(json.dumps(report.build_event("install", "create-venv", error="other")) + "\n")
    monkeypatch.setattr(report, "_post", lambda *a, **k: {"accepted": [], "rejected": []})
    report.flush()
    assert fresh.exists()  # another live flusher owns it


def test_spool_capped_oldest_dropped(report_dir, monkeypatch):
    def refuse(*a, **k):
        raise TransportError("down", category="connection-refused")
    monkeypatch.setattr(report, "_post", refuse)
    for _ in range(report.SPOOL_MAX_EVENTS + 10):
        report._append_spool(report.build_event("install", "create-venv", error="other"))
    report.flush()  # claim -> fail -> merge back trims to cap
    assert len(_events(report_dir)) == report.SPOOL_MAX_EVENTS


def test_local_dedup_24h(report_dir, monkeypatch):
    def refuse(*a, **k):
        raise TransportError("down", category="connection-refused")
    monkeypatch.setattr(report, "_post", refuse)
    report.emit("runtime", "gateway-dispatch", error="other")
    report.emit("runtime", "gateway-dispatch", error="other")  # identical enums
    assert len(_events(report_dir)) == 1


def test_emit_disabled_writes_nothing(report_dir, monkeypatch):
    monkeypatch.delenv("FIREKEEP_FAILURE_REPORT", raising=False)
    report.emit("install", "create-venv", error="other")
    assert not _spool(report_dir).exists()


def test_two_process_flush_empties_spool_exactly(report_dir):
    """Racing flushers (spec Testing, 'Spool'): claim-by-rename means at most
    one sender; the spool ends empty with no stranded claim files."""
    import subprocess
    import sys
    for _ in range(10):
        report._append_spool(report.build_event("install", "create-venv", error="other"))
    worker = (
        "from firekeep_client import report\n"
        "report._post = lambda url, body, timeout: "
        "{'accepted': [e['id'] for e in body['events']], 'rejected': []}\n"
        "report.flush()\n"
    )
    env = dict(os.environ, FIREKEEP_REPORT_DIR=str(report_dir),
               FIREKEEP_FAILURE_REPORT="1")
    procs = [subprocess.Popen([sys.executable, "-c", worker], env=env)
             for _ in range(2)]
    for p in procs:
        assert p.wait(30) == 0
    assert _events(report_dir) == []
    assert list(report_dir.glob("report-spool.sending.*")) == []


def test_concurrent_append_no_lost_lines(report_dir):
    """Windows CRT's O_APPEND is a seek-then-write, not one atomic syscall
    (unlike POSIX): two processes racing _append_spool can each seek to a
    stale end-of-file and clobber each other's line. This must be serialized
    (msvcrt byte-range lock on Windows) so every appended line survives.
    Per-process count kept comfortably under the 2xSPOOL_MAX_BYTES circuit
    breaker (~417 lines total at this event's ~157 bytes/line) so a
    legitimate cap-drop can't be mistaken for the race this guards against."""
    import subprocess
    import sys
    per_proc = 175
    worker = (
        "from firekeep_client import report\n"
        f"for _ in range({per_proc}):\n"
        "    report._append_spool(report.build_event('install', 'create-venv', error='other'))\n"
    )
    env = dict(os.environ, FIREKEEP_REPORT_DIR=str(report_dir),
               FIREKEEP_FAILURE_REPORT="1")
    procs = [subprocess.Popen([sys.executable, "-c", worker], env=env)
             for _ in range(2)]
    for p in procs:
        assert p.wait(30) == 0
    assert len(_events(report_dir)) == per_proc * 2


def test_append_spool_circuit_breaker_drops_when_oversized(report_dir):
    """A spool already past 2xSPOOL_MAX_BYTES means flush is broken entirely
    (spec 'never grow unbounded'); _append_spool must refuse to make it
    worse rather than append on top of it."""
    spool = _spool(report_dir)
    spool.parent.mkdir(parents=True, exist_ok=True)
    spool.write_bytes(b"x" * (2 * report.SPOOL_MAX_BYTES + 1))
    before = spool.stat().st_size
    report._append_spool(report.build_event("install", "create-venv", error="other"))
    assert spool.stat().st_size == before
