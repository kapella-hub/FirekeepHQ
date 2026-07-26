"""Skill staleness sweep: active skills unrecalled beyond SKILL_STALE_AFTER_DAYS
get flagged stale=True for human review; a re-recalled skill un-stales. Never
touches skill_status, never deletes — staleness is a review signal, deletion
stays human-only (mirrors reconcile.py's 'active skills are never deleted')."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


from app.skills.staleness import skill_staleness_pass


class _Point:
    def __init__(self, pid, payload):
        self.id = pid
        self.payload = payload


class _FakeQdrant:
    """Minimal scroll+set_payload+close stub."""

    def __init__(self, points):
        self._points = points
        self.set_calls = []  # (point_id, payload_dict)
        self.closed = False

    def scroll(self, collection_name, scroll_filter=None, limit=1000,
               with_payload=True, with_vectors=False, offset=None):
        return list(self._points), None

    def set_payload(self, collection_name, payload, points):
        for pid in points:
            self.set_calls.append((pid, payload))
            for p in self._points:
                if p.id == pid:
                    p.payload.update(payload)

    def close(self):
        self.closed = True


class _Settings:
    QDRANT_COLLECTION = "test"
    SKILL_STALE_AFTER_DAYS = 90


def _iso(dt):
    return dt.isoformat()


NOW = datetime(2026, 7, 16, tzinfo=timezone.utc)


def test_flags_active_skill_unrecalled_past_threshold():
    old = _iso(NOW - timedelta(days=120))
    pts = [_Point("s1", {"skill_status": "active", "last_recalled_at": old})]
    q = _FakeQdrant(pts)

    result = skill_staleness_pass(client=q, settings=_Settings(), now=NOW)

    assert result["flagged"] == 1
    assert pts[0].payload["stale"] is True
    assert "stale_detected_at" in pts[0].payload


def test_fresh_skill_not_flagged():
    recent = _iso(NOW - timedelta(days=10))
    pts = [_Point("s1", {"skill_status": "active", "last_recalled_at": recent})]
    q = _FakeQdrant(pts)

    result = skill_staleness_pass(client=q, settings=_Settings(), now=NOW)

    assert result["flagged"] == 0
    assert q.set_calls == []  # no write for an already-fresh, un-stale skill


def test_falls_back_to_timestamp_when_never_recalled():
    """A skill with no last_recalled_at uses its creation timestamp — a skill
    created 200d ago and never recalled is stale."""
    created = _iso(NOW - timedelta(days=200))
    pts = [_Point("s1", {"skill_status": "active", "timestamp": created})]
    q = _FakeQdrant(pts)

    result = skill_staleness_pass(client=q, settings=_Settings(), now=NOW)

    assert result["flagged"] == 1
    assert pts[0].payload["stale"] is True


def test_re_recalled_skill_un_stales():
    """Self-healing: a skill marked stale that was recalled recently clears."""
    recent = _iso(NOW - timedelta(days=2))
    pts = [_Point("s1", {"skill_status": "active", "stale": True,
                         "last_recalled_at": recent})]
    q = _FakeQdrant(pts)

    result = skill_staleness_pass(client=q, settings=_Settings(), now=NOW)

    assert result["unstaled"] == 1
    assert pts[0].payload["stale"] is False


def test_already_stale_old_skill_not_rewritten():
    """Idempotent: an old skill already flagged stale is not written again."""
    old = _iso(NOW - timedelta(days=120))
    pts = [_Point("s1", {"skill_status": "active", "stale": True,
                         "last_recalled_at": old})]
    q = _FakeQdrant(pts)

    result = skill_staleness_pass(client=q, settings=_Settings(), now=NOW)

    assert result["flagged"] == 0
    assert q.set_calls == []


def test_never_touches_skill_status_or_deletes():
    old = _iso(NOW - timedelta(days=120))
    pts = [_Point("s1", {"skill_status": "active", "last_recalled_at": old})]
    q = _FakeQdrant(pts)

    skill_staleness_pass(client=q, settings=_Settings(), now=NOW)

    assert pts[0].payload["skill_status"] == "active"  # untouched
    for _pid, payload in q.set_calls:
        assert "skill_status" not in payload


def test_malformed_timestamp_skipped_not_flagged():
    pts = [_Point("s1", {"skill_status": "active", "last_recalled_at": "garbage"})]
    q = _FakeQdrant(pts)

    result = skill_staleness_pass(client=q, settings=_Settings(), now=NOW)

    # Undated skill is neither flagged nor crashes the pass.
    assert result["status"] == "ok"
    assert result["flagged"] == 0


def test_pass_reports_ok_status_and_closes_client():
    q = _FakeQdrant([])
    result = skill_staleness_pass(client=q, settings=_Settings(), now=NOW)
    assert result["status"] == "ok"


def test_stale_reviewed_at_buys_a_fresh_window():
    """A human 'Still valid' review (stale_reviewed_at=now) must keep an old,
    unrecalled skill OUT of the stale set — otherwise the next sweep re-flags it
    and the acknowledgment is undone (adversarial-review finding)."""
    old = _iso(NOW - timedelta(days=200))
    reviewed = _iso(NOW - timedelta(days=1))
    pts = [_Point("s1", {"skill_status": "active", "timestamp": old,
                         "last_recalled_at": old, "stale_reviewed_at": reviewed})]
    q = _FakeQdrant(pts)

    result = skill_staleness_pass(client=q, settings=_Settings(), now=NOW)

    assert result["flagged"] == 0
    assert q.set_calls == []  # not re-flagged; the review holds


def test_stale_reviewed_at_expires_after_window():
    """The review buys ONE window — an old review no longer protects the skill."""
    old = _iso(NOW - timedelta(days=200))
    stale_review = _iso(NOW - timedelta(days=120))  # reviewed, but >90d ago
    pts = [_Point("s1", {"skill_status": "active", "timestamp": old,
                         "last_recalled_at": old, "stale_reviewed_at": stale_review})]
    q = _FakeQdrant(pts)

    result = skill_staleness_pass(client=q, settings=_Settings(), now=NOW)

    assert result["flagged"] == 1
    assert pts[0].payload["stale"] is True
