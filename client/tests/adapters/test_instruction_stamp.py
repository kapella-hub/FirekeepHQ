"""Round-2 measurement contract (client 0.1.41): the stamped instruction block.

Two instruction artifacts exist per session — the rendered block (per-runtime
file; what is on disk is the truth) and the gateway handshake text (served fresh
from the running wheel). This suite pins their versioning machinery:

  - RENDERED_INSTRUCTIONS_HASH / GATEWAY_INSTRUCTIONS_HASH = sha256[:12] of the
    exact artifact text.
  - The BEGIN marker is stamped (`h=<hash>` — deliberately NO v=: a version
    field would rewrite the file every release even with unchanged content,
    invalidating the customer's prompt cache), matched everywhere by
    LINE-ANCHORED PREFIX, so stamped and legacy unstamped blocks upsert/strip
    identically — the migration path is the ordinary render.
  - The hash covers ONLY the content BETWEEN the markers, so the stamp never
    hashes itself and re-rendering from the same wheel is byte-identical
    (test_write_stability.py's prompt-cache contract survives the stamp).
  - read_rendered_instructions_hash re-hashes what is actually ON DISK — the
    honest half of the X-Firekeep-Instr-Rendered header — including kiro's
    whole-file steering shape, which must hash the same content basis.
"""
from __future__ import annotations

import hashlib

import pytest

from firekeep_client.adapters import get_adapter
from firekeep_client.adapters.base import (
    FIREKEEP_INSTRUCTIONS,
    GATEWAY_INSTRUCTIONS,
    GATEWAY_INSTRUCTIONS_HASH,
    INSTRUCTIONS_BEGIN,
    INSTRUCTIONS_BEGIN_PREFIX,
    INSTRUCTIONS_END,
    RENDERED_INSTRUCTIONS_HASH,
    read_rendered_instructions_hash,
    rendered_block_stamp,
    rendered_instructions_path,
    strip_marked_block,
    upsert_marked_block,
)

# The exact pre-0.1.41 begin line, verbatim. DO NOT "fix" this string to match
# the current marker: it pins the migration input — a file rendered by an old
# wheel — forever (the LEGACY_HOOK_MARKERS rename lesson).
LEGACY_BEGIN = (
    "<!-- firekeep:instructions:begin — firekeep-owned block, do not edit; "
    "re-rendered by `firekeep install` -->"
)

RUNTIMES = ("claude", "codex", "kiro", "opencode")


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)  # opencode: default ~/.config
    return tmp_path


# --- the hash and stamp definitions ------------------------------------------


def test_hashes_are_sha256_12_of_the_exact_artifacts():
    assert RENDERED_INSTRUCTIONS_HASH == hashlib.sha256(
        FIREKEEP_INSTRUCTIONS.encode("utf-8")).hexdigest()[:12]
    assert GATEWAY_INSTRUCTIONS_HASH == hashlib.sha256(
        GATEWAY_INSTRUCTIONS.encode("utf-8")).hexdigest()[:12]


def test_begin_marker_is_stamped_with_content_hash_only():
    """h= yes, v= NO: the stamp must be a pure function of the CONTENT. A
    wheel-version field would make every release rewrite the rendered files
    even when the instruction text is unchanged — moving mtime on files in
    the customer's prompt prefix, the exact cost write_text_if_changed's
    docstring calls indefensible (external review 2026-08-12)."""
    assert INSTRUCTIONS_BEGIN.startswith(INSTRUCTIONS_BEGIN_PREFIX)
    assert f"h={RENDERED_INSTRUCTIONS_HASH}" in INSTRUCTIONS_BEGIN
    assert " v=" not in INSTRUCTIONS_BEGIN
    assert INSTRUCTIONS_BEGIN.endswith("-->")


def test_rendered_block_stamp_reads_the_h_claim():
    stamped = upsert_marked_block("", FIREKEEP_INSTRUCTIONS)
    assert rendered_block_stamp(stamped) == RENDERED_INSTRUCTIONS_HASH
    legacy = f"{LEGACY_BEGIN}\nx\n{INSTRUCTIONS_END}\n"
    assert rendered_block_stamp(legacy) is None  # unstamped: no claim to read
    assert rendered_block_stamp("no block at all") is None


# --- prefix matching: stamped and legacy blocks behave identically ------------


def test_legacy_unstamped_block_is_migrated_to_the_stamped_form():
    """An old file with the unstamped begin line gets replaced by the stamped
    block on its next render — no separate migration code, just the prefix
    match. User content on both sides survives byte-for-byte."""
    legacy = f"mine\n\n{LEGACY_BEGIN}\nOLD CONTENT\n{INSTRUCTIONS_END}\nafter\n"
    out = upsert_marked_block(legacy, FIREKEEP_INSTRUCTIONS)
    assert LEGACY_BEGIN not in out
    assert INSTRUCTIONS_BEGIN in out
    assert "OLD CONTENT" not in out
    assert out.startswith("mine")
    assert "after" in out
    # And the migration settles in one step: the next render is a no-op.
    assert upsert_marked_block(out, FIREKEEP_INSTRUCTIONS) == out


def test_strip_removes_a_stamped_block():
    text = upsert_marked_block("mine\n", FIREKEEP_INSTRUCTIONS)
    stripped = strip_marked_block(text)
    assert "memory_recall" not in stripped
    assert "mine" in stripped


def test_strip_removes_a_legacy_unstamped_block_identically():
    legacy = f"mine\n\n{LEGACY_BEGIN}\nold content\n{INSTRUCTIONS_END}\nafter\n"
    stripped = strip_marked_block(legacy)
    assert "old content" not in stripped
    assert "mine" in stripped and "after" in stripped


def test_prefix_matching_tolerates_any_future_begin_line_tail():
    """The find_legacy_block_bounds precedent: the begin line was always allowed
    a variable tail. A block stamped by a FUTURE wheel must still be found."""
    future = f"{INSTRUCTIONS_BEGIN_PREFIX} v=9.9.9 h=abcdefabcdef (future tail) -->"
    text = f"{future}\nfuture content\n{INSTRUCTIONS_END}\n"
    out = upsert_marked_block(text, FIREKEEP_INSTRUCTIONS)
    assert "future content" not in out
    assert INSTRUCTIONS_BEGIN in out
    assert out.count(INSTRUCTIONS_BEGIN_PREFIX) == 1  # replaced, not accumulated


def test_a_stray_end_marker_before_the_block_does_not_invert_the_span():
    """END is searched AFTER the begin match; a stray END earlier in the user's
    own prose must not make the span negative (the old two-independent-finds
    shape had to guard end > begin explicitly)."""
    text = (f"user prose mentioning {INSTRUCTIONS_END} verbatim\n\n"
            f"{INSTRUCTIONS_BEGIN}\ncontent\n{INSTRUCTIONS_END}\n")
    out = upsert_marked_block(text, FIREKEEP_INSTRUCTIONS)
    assert out.startswith("user prose")
    assert "memory_recall" in out


# --- the two damage cases the 2026-08-12 review demonstrated destroying data --


def test_mid_line_mention_of_the_begin_prefix_is_never_matched():
    """Line anchoring: prose that MENTIONS the marker prefix mid-sentence must
    not be mistaken for the block — the unanchored find made one render swallow
    every user line between the mention and the real block's END."""
    text = (f"my notes mention {INSTRUCTIONS_BEGIN_PREFIX} in passing\n"
            "MORE-USER\n\n"
            f"{INSTRUCTIONS_BEGIN}\nold content\n{INSTRUCTIONS_END}\nafter\n")
    out = upsert_marked_block(text, FIREKEEP_INSTRUCTIONS)
    assert "MORE-USER" in out
    assert out.startswith("my notes mention")
    assert "old content" not in out  # the REAL block was the one replaced
    assert "after" in out
    # Same rule for a mention with no real block: nothing is destroyed, the
    # block is appended below.
    mention_only = f"prose citing {INSTRUCTIONS_BEGIN_PREFIX} mid-line\nKEEP\n"
    out2 = upsert_marked_block(mention_only, FIREKEEP_INSTRUCTIONS)
    assert out2.startswith("prose citing")
    assert "KEEP" in out2
    assert INSTRUCTIONS_BEGIN in out2


def test_orphaned_begin_line_heals_without_eating_user_content():
    """BEGIN present, END deleted by hand. The old append shape left the orphan
    in place and appended a second block; the NEXT render's span then ran from
    the orphan to the appended block's END, swallowing every user line between
    them. The heal path replaces exactly the orphaned marker line — user
    content below survives BOTH renders and the file settles to one block."""
    damaged = (f"TOP\n{INSTRUCTIONS_BEGIN}\nleftover body line\n"
               "USER-NOTES-AFTER-DELETED-END\n")
    once = upsert_marked_block(damaged, FIREKEEP_INSTRUCTIONS)
    assert "USER-NOTES-AFTER-DELETED-END" in once
    assert once.startswith("TOP")
    assert once.count(INSTRUCTIONS_BEGIN_PREFIX) == 1  # healed, not doubled
    twice = upsert_marked_block(once, FIREKEEP_INSTRUCTIONS)
    assert "USER-NOTES-AFTER-DELETED-END" in twice
    assert twice == once  # settles: the second render is a no-op
    # The leftover body below the orphan is indistinguishable from user
    # content without an END marker, so it is preserved as residue — visible
    # beats silently deleted.
    assert "leftover body line" in once


def test_strip_removes_an_orphaned_begin_line_but_keeps_user_content():
    damaged = f"mine\n{INSTRUCTIONS_BEGIN}\nUSER-KEEPS-THIS\n"
    stripped = strip_marked_block(damaged)
    assert INSTRUCTIONS_BEGIN_PREFIX not in stripped
    assert "mine" in stripped
    assert "USER-KEEPS-THIS" in stripped


# --- byte stability: the stamp is content-derived, never a timestamp ----------


def test_upsert_is_byte_stable_under_re_render():
    once = upsert_marked_block("user text\n", FIREKEEP_INSTRUCTIONS)
    assert upsert_marked_block(once, FIREKEEP_INSTRUCTIONS) == once


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_double_render_leaves_the_instruction_file_byte_identical(
        fake_home, tmp_path, runtime):
    """The full-file version of the property test_write_stability.py pins for
    claude: a second render from the same wheel changes nothing, stamp included."""
    adapter = get_adapter(runtime)
    adapter.render(venv_bin=tmp_path / "venv" / "Scripts")
    path = rendered_instructions_path(runtime)
    assert path is not None and path.is_file()
    before = path.read_text(encoding="utf-8")
    adapter.render(venv_bin=tmp_path / "venv" / "Scripts")
    assert path.read_text(encoding="utf-8") == before


# --- on-disk re-hash: the X-Firekeep-Instr-Rendered basis ---------------------


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_render_then_rehash_round_trips_the_wheel_hash(fake_home, tmp_path, runtime):
    """A current file hashes equal to RENDERED_INSTRUCTIONS_HASH on EVERY
    runtime — including kiro, whose steering doc is whole-file (frontmatter +
    marker line + instructions) rather than a marker-delimited block. The hash
    basis is the same content either way, which is what lets one Expected value
    serve all four runtimes."""
    get_adapter(runtime).render(venv_bin=tmp_path / "venv" / "Scripts")
    assert read_rendered_instructions_hash(runtime) == RENDERED_INSTRUCTIONS_HASH


def test_pure_upsert_then_rehash_round_trips_too(fake_home):
    """Same property without an adapter in the loop: the hash covers exactly the
    `content` string upsert_marked_block received — never the stamp."""
    path = fake_home / ".claude" / "CLAUDE.md"
    path.parent.mkdir(parents=True)
    path.write_text(upsert_marked_block("# mine\n", FIREKEEP_INSTRUCTIONS),
                    encoding="utf-8")
    assert read_rendered_instructions_hash("claude") == RENDERED_INSTRUCTIONS_HASH


def test_hand_edited_block_reports_its_true_hash(fake_home):
    """The client re-hashes what is on disk rather than trusting its own stamp —
    a hand-edited block must NOT report the wheel hash."""
    path = fake_home / ".claude" / "CLAUDE.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        upsert_marked_block("", FIREKEEP_INSTRUCTIONS).replace(
            "memory_recall", "memory_recal", 1),
        encoding="utf-8",
    )
    on_disk = read_rendered_instructions_hash("claude")
    assert on_disk is not None
    assert on_disk != RENDERED_INSTRUCTIONS_HASH
    # The stamp still claims the wheel hash — the contradiction doctor names 'edited'.
    assert rendered_block_stamp(path.read_text(encoding="utf-8")) == RENDERED_INSTRUCTIONS_HASH


def test_absent_file_or_block_reads_none(fake_home):
    assert read_rendered_instructions_hash("claude") is None  # no file at all
    md = fake_home / ".claude" / "CLAUDE.md"
    md.parent.mkdir(parents=True)
    md.write_text("user prose, no firekeep block\n", encoding="utf-8")
    assert read_rendered_instructions_hash("claude") is None  # file, no block
    assert read_rendered_instructions_hash("not-a-runtime") is None


def test_legacy_unstamped_block_still_rehashes_its_content(fake_home):
    """Attribution must work for a file rendered by a pre-stamp wheel: the
    content basis is unchanged, so a legacy block whose text happens to match
    this wheel's hashes as current."""
    path = fake_home / ".claude" / "CLAUDE.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"{LEGACY_BEGIN}\n{FIREKEEP_INSTRUCTIONS}{INSTRUCTIONS_END}\n",
                    encoding="utf-8")
    assert read_rendered_instructions_hash("claude") == RENDERED_INSTRUCTIONS_HASH
