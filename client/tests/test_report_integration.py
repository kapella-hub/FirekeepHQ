"""End-to-end: emit -> spool -> flush -> HTTP -> strict validation, with the
same vocabulary tables the PHP collector (Task 10) enforces."""
import errno
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from firekeep_client import report


@pytest.fixture
def collector():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.headers.get("Content-Type", "").startswith("application/json")
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            accepted, rejected = [], []
            for ev in body["events"]:
                ok = (ev.get("kind") in report.KINDS
                      and ev.get("error") in report.ERRORS
                      and ev.get("os") in report.OS_FAMILIES
                      and ev.get("arch") in report.ARCHES
                      and ev.get("py") in report.PY_BUCKETS
                      and isinstance(ev.get("id"), str) and len(ev["id"]) == 32)
                (accepted if ok else rejected).append(ev["id"])
                if ok:
                    received.append(ev)
            out = json.dumps({"accepted": accepted, "rejected": rejected,
                              "sealed": 0, "active_bytes": 0}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port, received
    server.shutdown()


def test_install_failure_reaches_collector(collector, tmp_path, monkeypatch):
    port, received = collector
    monkeypatch.setenv("FIREKEEP_REPORT_DIR", str(tmp_path))
    monkeypatch.setenv("FIREKEEP_FAILURE_REPORT", "1")
    monkeypatch.setattr(report, "REPORT_URL", f"http://127.0.0.1:{port}/")
    report.emit("install", "create-venv",
                exc=PermissionError(errno.EACCES, "/secret/path"), exit_code=1)
    assert len(received) == 1
    ev = received[0]
    assert ev["stage"] == "create-venv" and ev["error"] == "permission-denied"
    assert "/secret/path" not in json.dumps(ev)
    assert not (tmp_path / "report-spool.jsonl").exists()  # acked -> gone


def test_bypass_sends_nothing(collector, tmp_path, monkeypatch):
    port, received = collector
    monkeypatch.setenv("FIREKEEP_REPORT_DIR", str(tmp_path))
    monkeypatch.setenv("FIREKEEP_FAILURE_REPORT", "1")
    monkeypatch.setenv("FIREKEEP_BYPASS", "1")
    monkeypatch.setattr(report, "REPORT_URL", f"http://127.0.0.1:{port}/")
    report.emit("install", "create-venv", error="other")
    report.flush()
    assert received == [] and not (tmp_path / "report-spool.jsonl").exists()


def test_no_consent_sends_nothing(collector, tmp_path, monkeypatch):
    port, received = collector
    monkeypatch.setenv("FIREKEEP_REPORT_DIR", str(tmp_path))
    monkeypatch.delenv("FIREKEEP_FAILURE_REPORT", raising=False)
    monkeypatch.setattr(report, "REPORT_URL", f"http://127.0.0.1:{port}/")
    report.emit("install", "create-venv", error="other")
    assert received == []
