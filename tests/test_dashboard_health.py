"""The dashboard health check must not report a broken service as healthy.

Why this file exists
--------------------
`renderHealthGrid` contained, for months:

    if (r.ok || r.status === 405 || r.status === 404) -> paint GREEN

which made it a port-liveness check wearing the costume of a health check. A
misrouted nginx route, a container that boots but cannot reach Qdrant, and a
half-finished customer install all answer 404 and all rendered GREEN. Observed
live: a dashboard served against a host with no API showed all four services
green at 31-34ms while every request 404'd.

That is the worst failure mode a status indicator has -- not "no signal" but
"confident wrong signal" -- and it sat on the first screen a buyer sees.

It survived because the decision was made inline inside a fetch callback,
interleaved with DOM writes, so nothing could assert on it without a browser.
The fix extracts a pure `healthVerdict(httpStatus, bodyText)`; this file runs
that exact function -- the shipped source, not a copy -- under node.

THE CASE THAT MATTERS is `test_404_is_down`. It is the bug, restated. If it
ever passes against a build that still tolerates 404, this file is decoration.
`test_the_old_tolerance_would_fail_these_cases` guards that by running the
ORIGINAL predicate against the same table and asserting it gets them wrong --
so the suite proves it can discriminate, rather than merely being green.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard" / "index.html"
START, END = ">>> healthVerdict", "<<< healthVerdict"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _extract() -> str:
    src = DASHBOARD.read_text(encoding="utf-8")
    try:
        body = src.split(START, 1)[1].split(END, 1)[0]
    except IndexError:  # pragma: no cover - only when someone deletes the markers
        pytest.fail(
            f"sentinels {START!r}/{END!r} missing from dashboard/index.html. "
            "They are load-bearing: this test executes the shipped function."
        )
    # Both sentinels live INSIDE /* */ comments, so a raw split leaves comment
    # fragments at each end: prose plus a dangling `*/` at the top, and a bare
    # `/*` at the bottom. Either is a syntax error before one assertion can run.
    # Cut at the close of the opening comment and the open of the closing one.
    return body.split("*/", 1)[1].rsplit("/*", 1)[0]


def _verdict(http_status: int, body: str) -> dict:
    js = _extract() + (
        "\nconst out = healthVerdict(%s, %s);\n"
        "process.stdout.write(JSON.stringify(out));\n"
    ) % (json.dumps(http_status), json.dumps(body))
    p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, f"node failed: {p.stderr[:400]}"
    return json.loads(p.stdout)


# Bodies copied verbatim from the live deployment, probed through the tunnel.
# Using invented shapes here would test a fiction; the whole point of the
# original bug is that nobody looked at what the services actually return.
CORTEX_OK = '{"status":"ok","services":{"redis":{"status":"connected","detail":null},"graph":{"status":"connected"}}}'
CORTEX_SICK = '{"status":"ok","services":{"redis":{"status":"connected"},"graph":{"status":"error"}}}'
BRIDGE_OK = '{"status":"ok","service":"bridge"}'
SENTINEL_OK = '{"status":"ok","service":"sentinel","redis":"connected","collectors":{"docker":true,"git":true}}'
SENTINEL_COLLECTOR_OFF = '{"status":"ok","service":"sentinel","redis":"connected","collectors":{"docker":false,"git":true}}'
SENTINEL_REDIS_DOWN = '{"status":"ok","service":"sentinel","redis":"disconnected","collectors":{"docker":true}}'


class TestTheBug:
    def test_404_is_down(self):
        """THE regression. 404 meant 'green' for months."""
        v = _verdict(404, "<html>404 Not Found</html>")
        assert v["state"] == "down", "a 404 must never read as healthy"

    def test_405_is_down(self):
        assert _verdict(405, "")["state"] == "down"

    def test_401_is_down(self):
        """An auth-enabled deployment answers 401 on many paths. Reachable is
        not healthy."""
        assert _verdict(401, '{"detail":"unauthorized"}')["state"] == "down"

    def test_200_with_html_is_down(self):
        """A proxy serving its own error page with a 200 is the subtlest case:
        status looks fine, content is not the service."""
        assert _verdict(200, "<html><body>gateway</body></html>")["state"] == "down"

    def test_200_with_empty_body_is_down(self):
        assert _verdict(200, "")["state"] == "down"


class TestHealthy:
    """The other half of the trap: a fix that turns healthy services red is the
    same defect reversed. Every body here is real."""

    @pytest.mark.parametrize(
        "body", [CORTEX_OK, BRIDGE_OK, SENTINEL_OK], ids=["cortex", "bridge", "sentinel"]
    )
    def test_real_healthy_bodies_are_up(self, body):
        assert _verdict(200, body)["state"] == "up"

    def test_a_disabled_collector_is_not_a_sick_service(self):
        """sentinel's `collectors` are feature switches. Reading booleans as
        health would report a false DEGRADED every time someone turns one off."""
        assert _verdict(200, SENTINEL_COLLECTOR_OFF)["state"] == "up"

    def test_the_service_name_is_not_a_verdict(self):
        """`"service":"sentinel"` is an identifier. Treating every non-healthy
        string as a fault would flag every service by its own name."""
        assert _verdict(200, BRIDGE_OK)["state"] == "up"


class TestDegraded:
    """The state the old binary up/down could not express at all."""

    def test_a_failed_nested_dependency_is_degraded_not_down(self):
        v = _verdict(200, CORTEX_SICK)
        assert v["state"] == "degraded"
        assert "graph" in v["detail"], "the failing dependency must be named"

    def test_a_disconnected_top_level_dependency_is_degraded(self):
        v = _verdict(200, SENTINEL_REDIS_DOWN)
        assert v["state"] == "degraded"
        assert "redis" in v["detail"]

    def test_a_non_ok_status_is_degraded(self):
        assert _verdict(200, '{"status":"starting"}')["state"] == "degraded"


class TestTheTestCanFail:
    def test_the_old_tolerance_would_fail_these_cases(self):
        """Proves discrimination. Runs the ORIGINAL predicate over the same
        table; it must get the 404/405 cases wrong. A test suite that would
        pass against the broken code is not a regression test."""
        old = (
            "function old(status){ return (status>=200&&status<300)||status===405||status===404"
            " ? 'up' : 'down'; }\n"
            "process.stdout.write(JSON.stringify([old(404), old(405), old(401)]));"
        )
        p = subprocess.run(["node", "-e", old], capture_output=True, text=True, timeout=30)
        assert p.returncode == 0, p.stderr
        got = json.loads(p.stdout)
        assert got[0] == "up" and got[1] == "up", (
            "the old predicate should call 404/405 healthy -- if it does not, this "
            "file is no longer testing the bug it was written for"
        )
        assert got[2] == "down"

    def test_the_tolerance_is_gone_from_the_source(self):
        """Cheap tripwire, independent of node: the literal predicate must not
        return. Complements the behavioural tests rather than replacing them."""
        src = DASHBOARD.read_text(encoding="utf-8")
        code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)  # ignore the comment that documents it
        assert "status === 404" not in code, "the 404-as-healthy tolerance is back"
        assert "status === 405" not in code, "the 405-as-healthy tolerance is back"
