"""The repair must never corrupt text nobody corrupted.

This script rewrites live customer data, so its detector is the part that has
to be right. The failure that matters is not "missed some mojibake" -- that is
recoverable by running again -- it is "mangled a correctly-stored string",
which is unrecoverable and indistinguishable from the bug it was fixing.

The whole safety argument rests on both halves of the round trip refusing what
they cannot account for:

    _encode_mojibake(s).decode("utf-8", strict)

Non-Latin text (Cyrillic, CJK, emoji, a correct em dash) fails the ENCODE.
Genuine accented text fails the DECODE. ASCII round-trips unchanged and is
therefore not written. These tests are that argument, executed.

They also pin the two bugs the live dry run exposed, both of which made the
script UNDER-report while looking healthy: it originally re-encoded with
latin-1, which cannot represent the cp1252 characters every real sample is
built from; and its Redis scan died with WRONGTYPE on a surface's own index
key, reporting that surface as clean.

The real corrupted strings below are copied verbatim from the live deployment
(relay task context, 2026-08-06) rather than constructed, because a test built
from an invented example only proves the function handles invented examples.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "repair_mojibake",
    Path(__file__).resolve().parents[1] / "scripts" / "repair_mojibake.py",
)
rm = importlib.util.module_from_spec(_SPEC)
sys.modules["repair_mojibake"] = rm
_SPEC.loader.exec_module(rm)


# Verbatim from the live relay queue.
LIVE_BROKEN = "docs(spec): Living Procedures â€” runbooks that harden with use"
LIVE_FIXED = "docs(spec): Living Procedures — runbooks that harden with use"


class TestTheRealCorruption:
    def test_the_live_string_is_recovered_exactly(self):
        out, rounds = rm.repair(LIVE_BROKEN)
        assert out == LIVE_FIXED
        assert rounds == 1

    def test_the_owners_presence_entry_is_recovered(self):
        out, _ = rm.repair("... Firekeep â€” due-diligence ...")
        assert out == "... Firekeep — due-diligence ..."

    @pytest.mark.parametrize("broken,fixed", [
        ("â€™", "’"), ("â€œ", "“"), ("â€\x9d", "”"),
        ("Ã©", "é"), ("Ã¼", "ü"), ("Ã±", "ñ"),
        ("âœ“", "✓"), ("ðŸ”¥", "🔥"),
    ])
    def test_the_common_shapes(self, broken, fixed):
        assert rm.repair(broken)[0] == fixed


class TestItRefusesTextNobodyCorrupted:
    """The unrecoverable failure. Every one of these must be left EXACTLY."""

    @pytest.mark.parametrize("s", [
        "plain ascii text",
        "",
        "café",                      # genuine latin-1 accent: fails the utf-8 decode
        "naïve résumé façade",
        "Привет мир",                # fails the latin-1 encode
        "日本語のテキスト",
        "emoji 🔥 and an em dash —",  # already correct UTF-8
        "→ ← ↑ ↓",
        "mixed café and Привет",
        "100% — done",
    ])
    def test_correct_text_is_returned_byte_identical(self, s):
        out, rounds = rm.repair(s)
        assert out == s and rounds == 0

    def test_repair_once_returns_none_rather_than_the_input(self):
        """None is the 'leave alone' signal; returning the input would make a
        no-op indistinguishable from a repair at the call site."""
        assert rm.repair_once("café") is None
        assert rm.repair_once("plain") is None
        assert rm.repair_once("Привет") is None

    def test_a_change_that_does_not_contract_is_refused(self):
        """Mojibake EXPANDS one char into 2-3, so a real repair contracts the
        high-character count. This guard is what protects a string that is
        genuinely 'Ã©' and means it."""
        assert rm.repair_once("A") is None
        # Construct a round-trippable string whose repair does not contract.
        for s in ("\xc2\x80", "\xc3\xa9"):
            out = rm.repair_once(s)
            if out is not None:
                assert rm._high_count(out) < rm._high_count(s)


class TestConvergence:
    def test_double_corruption_needs_two_rounds_and_gets_them(self):
        once = "—".encode("utf-8").decode("latin-1")
        twice = once.encode("utf-8").decode("latin-1")
        assert twice != once
        out, rounds = rm.repair(twice)
        assert out == "—" and rounds == 2

    def test_re_running_on_repaired_text_changes_nothing(self):
        """Safety of a second invocation: this is what lets an operator re-run
        without reasoning about what the first run did."""
        once, r1 = rm.repair(LIVE_BROKEN)
        twice, r2 = rm.repair(once)
        assert twice == once and r2 == 0

    def test_it_cannot_loop_forever(self):
        assert rm.repair("a" * 10)[1] == 0
        assert rm.MAX_ROUNDS >= 2


class TestNestedStructures:
    def test_it_repairs_inside_dicts_and_lists(self):
        payload = {"text": LIVE_BROKEN, "tags": ["okay", "â€™s"],
                   "meta": {"note": "â€œquotedâ€\x9d"}, "n": 42, "flag": True}
        out, changed, rounds = rm.repair_value(payload)
        assert out["text"] == LIVE_FIXED
        assert out["tags"] == ["okay", "’s"]
        assert out["meta"]["note"] == "“quoted”"
        assert changed == 3 and rounds == 1

    def test_non_string_values_survive_unchanged(self):
        payload = {"n": 42, "f": 1.5, "b": True, "z": None, "v": [1, 2, 3]}
        out, changed, _ = rm.repair_value(payload)
        assert out == payload and changed == 0

    def test_a_corrupted_KEY_is_repaired_too(self):
        """Leaving a corrupted key would strand its value under a name no
        reader can look up."""
        out, changed, _ = rm.repair_value({"nÃ¸te": "fine"})
        assert "nøte" in out and out["nøte"] == "fine" and changed == 1

    def test_an_unaffected_payload_reports_zero_changes(self):
        payload = {"text": "all good — really", "tags": ["café"]}
        out, changed, _ = rm.repair_value(payload)
        assert out == payload and changed == 0


class TestScanningRedisSurvivesTheIndexKey:
    """A record pattern also matches its index, and the index is not a hash.

    Measured live: `nr:presence:*` matches the sorted set
    `nr:presence:__index`, hgetall raised WRONGTYPE, and the whole surface
    aborted reporting 0 affected -- on the one surface known to hold corrupted
    text. A scan that dies on an index looks exactly like a clean surface.
    """

    @pytest.fixture
    def url(self):
        fakeredis = pytest.importorskip("fakeredis")
        server = fakeredis.FakeServer()
        r = fakeredis.FakeStrictRedis(server=server, decode_responses=True)
        r.hset("nr:presence:agent-a", mapping={"goal": LIVE_BROKEN, "status": "active"})
        r.hset("nr:presence:agent-b", mapping={"goal": "already fine — ok", "status": "idle"})
        r.zadd("nr:presence:__index", {"agent-a": 1, "agent-b": 2})  # the landmine
        # The script imports `redis` INSIDE scan_redis_hashes, so each test
        # injects a stand-in module via sys.modules rather than patching here.
        return r

    def test_the_sorted_set_index_does_not_abort_the_surface(self, url, monkeypatch):
        import types
        fake_mod = types.SimpleNamespace(from_url=lambda *_a, **_k: url)
        monkeypatch.setitem(sys.modules, "redis", fake_mod)
        rep = rm.scan_redis_hashes("redis://x/5", "nr:presence:*",
                                   ["goal", "status"], "relay:presence",
                                   apply=False, backup=None)
        assert rep.error is None, f"the index key aborted the surface: {rep.error}"
        assert rep.affected == 1, "the corrupted presence goal must be found"
        assert rep.scanned == 2, "the sorted set must not be counted as scanned"

    def test_correct_entries_are_left_alone(self, url, monkeypatch):
        import types
        monkeypatch.setitem(sys.modules, "redis",
                            types.SimpleNamespace(from_url=lambda *_a, **_k: url))
        rep = rm.scan_redis_hashes("redis://x/5", "nr:presence:*",
                                   ["goal", "status"], "relay:presence",
                                   apply=False, backup=None)
        assert rep.strings_changed == 1

    def test_dry_run_writes_nothing(self, url, monkeypatch):
        import types
        monkeypatch.setitem(sys.modules, "redis",
                            types.SimpleNamespace(from_url=lambda *_a, **_k: url))
        rm.scan_redis_hashes("redis://x/5", "nr:presence:*", ["goal"],
                             "relay:presence", apply=False, backup=None)
        assert url.hget("nr:presence:agent-a", "goal") == LIVE_BROKEN

    def test_apply_writes_the_repair_and_backs_up_the_prior_value(self, url, monkeypatch, tmp_path):
        import types
        monkeypatch.setitem(sys.modules, "redis",
                            types.SimpleNamespace(from_url=lambda *_a, **_k: url))
        backup = tmp_path / "b.jsonl"
        rep = rm.scan_redis_hashes("redis://x/5", "nr:presence:*", ["goal"],
                                   "relay:presence", apply=True, backup=backup)
        assert rep.written == 1
        assert url.hget("nr:presence:agent-a", "goal") == LIVE_FIXED
        saved = [json.loads(x) for x in backup.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert any(e.get("prior", {}).get("goal") == LIVE_BROKEN for e in saved), (
            "the prior value must be recoverable from the backup"
        )


class TestTheReportIsHonestAboutWhatItDid:
    def test_a_dry_run_says_so_and_names_the_flag(self):
        r = rm.SurfaceReport("relay:tasks", scanned=97, affected=32, strings_changed=40)
        out = rm.render([r], apply=False)
        assert "DRY RUN" in out and "nothing was written" in out
        assert "--apply" in out

    def test_an_applied_run_does_not_claim_to_be_a_dry_run(self):
        r = rm.SurfaceReport("relay:tasks", scanned=97, affected=32, written=32)
        out = rm.render([r], apply=True)
        assert "APPLIED" in out and "DRY RUN" not in out

    def test_a_surface_error_is_surfaced_not_swallowed(self):
        r = rm.SurfaceReport("qdrant:memories", error="ConnectionError: refused")
        out = rm.render([r], apply=False)
        assert "ERROR" in out and "refused" in out

    def test_multi_round_records_are_called_out_as_a_finding(self):
        r = rm.SurfaceReport("relay:tasks", scanned=10, affected=3, multi_round=2)
        assert "SECOND corrupting hop" in rm.render([r], apply=False)

    def test_a_zero_scan_is_visible_rather_than_looking_clean(self):
        """A wrong key pattern and a genuinely empty surface render the same
        way, so `scanned` must be shown. The first version scanned
        "nr:bulletin*" while the posts live at "nr:post:*" and reported a
        confident 0/0."""
        out = rm.render([rm.SurfaceReport("relay:bulletin")], apply=False)
        assert "relay:bulletin" in out and "0" in out

    def test_note_records_counts_and_caps_samples(self):
        r = rm.SurfaceReport("s")
        for i in range(9):
            r.note(f"id{i}", "before", "after", 2, 1)
        assert r.affected == 9 and r.strings_changed == 18
        assert len(r.samples) == 5, "samples are for eyeballing, not a full dump"
