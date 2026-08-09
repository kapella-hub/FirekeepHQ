"""Upgrading from the predecessor kit must leave ONE layer, not two.

Why this file exists
--------------------
A machine that ran the predecessor kit carries six `nexus-*` MCP servers and five
`nexus_client.hooks` entries. Firekeep registers its own six and its own hooks. With
no migration the machine ends up with TWELVE servers -- six pointing at a config path
that no longer exists, failing to connect on every session start -- and every
lifecycle event firing twice: doubled presence registration, doubled distill
enqueues.

That hazard was already known and solved once, for the bash->python generation
(base.LEGACY_HOOK_MARKERS, upsert_hook_group's "collapse ALL groups" behaviour).
The predecessor->Firekeep generation was simply never added.

THE ANTI-RENAME GUARD, which is the subtler half:
Legacy tokens name artifacts left by PREVIOUS generations, so they must keep
spelling the OLD thing forever. The predecessor rename mechanically rewrote
LEGACY_ENV_KEYS from NEXUS_*_URL to FIREKEEP_*_URL -- keys no machine has ever
had -- silently disarming that cleanup while every test stayed green. Renaming a
legacy token is not a rename, it is a deletion. These tests fail loudly if it
happens again.
"""
from __future__ import annotations

import json
import os

import pytest

from firekeep_client.adapters import get_adapter
from firekeep_client.adapters.base import (
    LEGACY_ENV_KEYS,
    LEGACY_HOOK_MARKERS,
    LEGACY_INSTRUCTION_MARKERS,
    LEGACY_MCP_KEYS,
)


class TestLegacyTokensStillNameTheOldThing:
    def test_env_keys_name_the_predecessor(self):
        assert LEGACY_ENV_KEYS, "the cleanup list must not be empty"
        for key in LEGACY_ENV_KEYS:
            assert key.startswith("NEXUS_"), (
                f"{key!r} names the CURRENT product. Legacy tokens must spell the OLD "
                f"one -- a rename here silently disarms the migration."
            )

    def test_mcp_keys_name_the_predecessor(self):
        assert LEGACY_MCP_KEYS
        for key in LEGACY_MCP_KEYS:
            assert key.startswith(("nexus-", "firekeep-")), (
                f"{key!r} must name a retired multi-entry generation"
            )
            assert key != "firekeep"

    def test_hook_markers_cover_the_predecessor_python_kit(self):
        assert any("nexus_client" in m for m in LEGACY_HOOK_MARKERS), (
            "the predecessor kit invoked `-m nexus_client.hooks`; without a marker for it "
            "an upgraded machine keeps both hook layers and fires every event twice"
        )

    def test_instruction_markers_name_the_predecessor_and_only_the_duplicate_block(self):
        assert LEGACY_INSTRUCTION_MARKERS
        for begin, end in LEGACY_INSTRUCTION_MARKERS:
            assert begin.startswith("<!-- nexus:instructions:begin"), (
                f"{begin!r} must be the predecessor's begin marker, prefix-matched "
                f"(the live line carries a variable prose tail)"
            )
            assert end == "<!-- nexus:instructions:end -->"
        # The sibling `Agent Guidelines` block (the predecessor's other, distinct
        # marker pair) is 0.03-similar to the firekeep block -- not a duplicate,
        # the user's own content -- and must never be listed here (removing it
        # would be a plain deletion).
        assert not any("Agent Guidelines" in begin for begin, _ in LEGACY_INSTRUCTION_MARKERS)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    # Redirect Path.home() on both Windows (USERPROFILE) and POSIX (HOME) — never real ~.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _machine_with_predecessor_kit(fake_home):
    """A realistic upgraded machine: predecessor servers, hooks and env in place."""
    (fake_home / ".claude").mkdir(parents=True, exist_ok=True)
    (fake_home / ".claude.json").write_text(json.dumps({"mcpServers": {
        "nexus-cortex": {"command": "old"}, "nexus-bridge": {"command": "old"},
        "nexus-sentinel": {"command": "old"}, "nexus-relay": {"command": "old"},
        "nexus-symdex": {"command": "old"}, "nexus-decision": {"command": "old"},
        "someone-elses-server": {"command": "keep me"},
    }}), encoding="utf-8")
    (fake_home / ".claude" / "settings.json").write_text(json.dumps({
        "env": {"NEXUS_CORTEX_URL": "http://old:8100", "FOO": "bar"},
        "hooks": {
            "SessionStart": [{"hooks": [
                {"type": "command", "command": "C:/x/.nexus/venv/Scripts/python.exe -m nexus_client.hooks session_start"}]}],
            "Stop": [{"hooks": [
                {"type": "command", "command": "C:/x/.nexus/venv/Scripts/python.exe -m nexus_client.hooks stop"}]}],
        },
    }), encoding="utf-8")


class TestClaudeUpgrade:
    def test_render_leaves_exactly_one_layer(self, fake_home, tmp_path):
        _machine_with_predecessor_kit(fake_home)
        get_adapter("claude").render(venv_bin=tmp_path / "vbin")

        servers = json.loads((fake_home / ".claude.json").read_text(encoding="utf-8"))["mcpServers"]
        for k in LEGACY_MCP_KEYS:
            assert k not in servers, f"{k} survived the upgrade -- the machine now has two kits"
        assert "someone-elses-server" in servers, "a foreign server must never be touched"
        assert "firekeep" in servers

    def test_no_lifecycle_event_fires_twice(self, fake_home, tmp_path):
        """THE failure this migration exists to prevent."""
        _machine_with_predecessor_kit(fake_home)
        get_adapter("claude").render(venv_bin=tmp_path / "vbin")

        hooks = json.loads((fake_home / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"]
        for event in ("SessionStart", "Stop"):
            commands = [h.get("command", "") for g in hooks.get(event, []) for h in g.get("hooks", [])]
            assert not any("nexus_client" in c for c in commands), (
                f"{event} still invokes the predecessor kit -- it will fire twice"
            )
            assert sum("firekeep_client" in c for c in commands) == 1, (
                f"{event} should invoke exactly one firekeep core, got {commands}"
            )

    def test_retired_env_keys_are_removed_and_foreign_ones_survive(self, fake_home, tmp_path):
        _machine_with_predecessor_kit(fake_home)
        get_adapter("claude").render(venv_bin=tmp_path / "vbin")
        env = json.loads((fake_home / ".claude" / "settings.json").read_text(encoding="utf-8"))["env"]
        assert "NEXUS_CORTEX_URL" not in env
        assert env.get("FOO") == "bar"


def test_legacy_venv_paths_migrate_to_current_on_rerender(fake_home):
    """0.1.34 -> 0.1.35 layout migration: one re-render, zero stale paths.

    Machines that installed before the side-by-side layout carry rendered configs
    whose hook commands and MCP server entry embed the ABSOLUTE legacy path,
    `~/.firekeep/venv/...`. The redesign's whole promise — updates are render-free
    because every surface routes through the `current` alias — only holds once
    those machines converge: the first re-render against a current-based venv_bin
    must remove every old-path entry and leave exactly one current-based entry
    per event, or the machine runs hooks from a venv a later update's GC deletes
    (the exact "No such file or directory on every lifecycle hook" failure the
    layout exists to retire).

    No new machinery should be needed, and this test proves none regressed:
    HOOK_MARKER identifies a firekeep hook by the `firekeep_client.hooks` module
    token, NOT by its path, so upsert_hook_group's collapse-all treats the old
    layout's groups as ours; merge_owned overwrites the `firekeep` MCP key. That
    path-independence is load-bearing — a marker that embedded the venv path
    would silently orphan every pre-0.1.35 machine while staying green here.
    """
    bin_name = "Scripts" if os.name == "nt" else "bin"
    old_bin = fake_home / ".firekeep" / "venv" / bin_name
    exe = ".exe" if os.name == "nt" else ""
    # Exactly as the pre-0.1.35 adapter rendered them: absolute legacy venv path,
    # forward-slashed (hook commands are bash-executed shell strings).
    old_py = str(old_bin / "python").replace("\\", "/") + exe
    (fake_home / ".claude").mkdir(parents=True, exist_ok=True)
    (fake_home / ".claude.json").write_text(json.dumps({"mcpServers": {
        "firekeep": {"type": "stdio", "command": str(old_bin / "firekeep") + exe,
                     "args": ["gateway"]},
        "someone-elses-server": {"command": "keep me"},
    }}), encoding="utf-8")
    (fake_home / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {
            "SessionStart": [{"hooks": [
                {"type": "command",
                 "command": f"{old_py} -m firekeep_client.hooks session_start",
                 "timeout": 15}]}],
            "Stop": [
                # A foreign group ahead of ours: it must survive, in place.
                {"hooks": [{"type": "command", "command": "notify-send bye"}]},
                {"hooks": [
                    {"type": "command",
                     "command": f"{old_py} -m firekeep_client.hooks stop",
                     "timeout": 5}]},
            ],
        },
    }), encoding="utf-8")

    new_bin = fake_home / ".firekeep" / "current" / bin_name
    get_adapter("claude").render(venv_bin=new_bin)

    # Trailing separator on purpose: distinguishes the legacy `venv/` dir from the
    # new `venvs/` dir, which shares it as a prefix.
    old_marker = str(fake_home / ".firekeep" / "venv").replace("\\", "/") + "/"
    new_marker = str(fake_home / ".firekeep" / "current").replace("\\", "/") + "/"

    hooks = json.loads(
        (fake_home / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"]
    for event in ("SessionStart", "Stop"):
        commands = [h.get("command", "")
                    for g in hooks.get(event, []) for h in g.get("hooks", [])]
        assert not any(old_marker in c for c in commands), (
            f"{event} still invokes the legacy ~/.firekeep/venv layout: {commands}")
        assert sum(new_marker in c for c in commands) == 1, (
            f"{event} should hold exactly one current-based command, got {commands}")
    assert any("notify-send bye" in h.get("command", "")
               for g in hooks["Stop"] for h in g.get("hooks", [])), (
        "the foreign Stop hook must survive the migration untouched")

    servers = json.loads(
        (fake_home / ".claude.json").read_text(encoding="utf-8"))["mcpServers"]
    assert str(fake_home / ".firekeep" / "current") in servers["firekeep"]["command"], (
        "the gateway entry must be repointed through the current alias")
    assert str(fake_home / ".firekeep" / "venv") + os.sep not in servers["firekeep"]["command"]
    assert servers["someone-elses-server"] == {"command": "keep me"}


_OLD = 1_600_000_000  # a fixed mtime in the past; no sleep needed


def _age(path):
    """Backdate a file so any rewrite is unmistakable."""
    os.utime(path, (_OLD, _OLD))


def _legacy_md_body():
    begin_prefix, end_marker = LEGACY_INSTRUCTION_MARKERS[0]
    return f"# My notes\n\n{begin_prefix} -->\nold block\n{end_marker}\n"


def _write_legacy_md(fake_home):
    """~/.claude/CLAUDE.md as a machine upgraded from the predecessor carries it."""
    md = fake_home / ".claude" / "CLAUDE.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(_legacy_md_body(), encoding="utf-8")
    return md


def _archives(md):
    return sorted(md.parent.glob("CLAUDE.md*.bak"))


class TestPredecessorInstructionBlockMigration:
    """The predecessor's OWN instruction block (Decision Board + Knowledge Ingest,
    upserted under `nexus:instructions` markers) is 0.75-similar to firekeep's --
    a near-duplicate of a subset, still worth removing. But ~/.claude/CLAUDE.md is
    user-owned prose, not a firekeep artifact: it must be archived to .bak, never
    deleted outright, and the user's own surrounding text must survive untouched.
    """

    def test_predecessor_instruction_block_is_archived_not_deleted(self, fake_home, tmp_path):
        md = fake_home / ".claude" / "CLAUDE.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        begin_prefix, end_marker = LEGACY_INSTRUCTION_MARKERS[0]
        # The live begin line carries a variable prose tail after the prefix --
        # exercising that the migration matches by prefix, not in full.
        md.write_text(
            "# My notes\nkeep me\n\n"
            f"{begin_prefix} — nexus-owned block, do not edit; "
            "re-rendered by `nexus install` -->\n"
            f"old block\n{end_marker}\n\n# more of my notes\n",
            encoding="utf-8",
        )

        get_adapter("claude").render(venv_bin=tmp_path / "venv" / "bin")

        body = md.read_text(encoding="utf-8")
        assert "old block" not in body                    # predecessor block gone
        assert "keep me" in body and "# more of my notes" in body   # user prose intact
        backups = list(md.parent.glob("CLAUDE.md*.bak"))
        assert backups, "a user-owned prose file must never be edited without a .bak"
        assert "old block" in backups[0].read_text(encoding="utf-8")

    def test_render_writes_no_backup_when_there_is_no_predecessor_block(self, fake_home, tmp_path):
        md = fake_home / ".claude" / "CLAUDE.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("# Just my notes\n", encoding="utf-8")
        get_adapter("claude").render(venv_bin=tmp_path / "venv" / "bin")
        assert not list(md.parent.glob("CLAUDE.md*.bak"))

    def test_pre_existing_bak_is_not_destroyed(self, fake_home, tmp_path):
        """A `.bak` that already exists at the archive path -- the user's own
        file, or another tool's -- must survive untouched. This is the same
        collision class that made kiro.py drop its own .bak archiving entirely
        (see the comment above KiroAdapter in adapters/kiro.py): overwriting a
        path we do not own is destruction, not migration. The live file must
        still be migrated -- archive-skip is not migration-skip.

        And the removed block must still be archived SOMEWHERE. Skipping the
        archive while stripping the block anyway left this run's content
        recoverable only outside this tool -- narrow, but the whole premise of
        the migration is that what it removes survives."""
        md = _write_legacy_md(fake_home)
        bak = md.with_name(md.name + ".bak")
        bak.write_text("someone else's backup -- not ours", encoding="utf-8")

        get_adapter("claude").render(venv_bin=tmp_path / "venv" / "bin")

        assert bak.read_text(encoding="utf-8") == "someone else's backup -- not ours"
        body = md.read_text(encoding="utf-8")
        assert "old block" not in body, "migration must still strip the block from the live file"
        assert "# My notes" in body

        fallbacks = [p for p in _archives(md) if p != bak]
        assert len(fallbacks) == 1, (
            f"the removed block must be archived under a fallback name, got {fallbacks}"
        )
        assert fallbacks[0].read_text(encoding="utf-8") == _legacy_md_body(), (
            "the fallback archive must hold the file AS IT WAS, block included"
        )

    def test_fallback_archive_name_is_deterministic_not_timestamped(self, fake_home, tmp_path):
        """The fallback name is derived from the archived BYTES, so archiving the
        same content twice reuses the same file and writes nothing. A time- or
        counter-based suffix would mint a fresh `.bak` on every render -- the
        `.bak`-clutter defect this fallback exists to avoid, in a new costume.

        Property 4 (idempotence) in the collision case: a second render creates
        no further file and rewrites nothing."""
        md = _write_legacy_md(fake_home)
        bak = md.with_name(md.name + ".bak")
        bak.write_text("someone else's backup -- not ours", encoding="utf-8")
        venv_bin = tmp_path / "venv" / "bin"

        get_adapter("claude").render(venv_bin=venv_bin)
        after_first = _archives(md)
        assert len(after_first) == 2
        for p in after_first:
            _age(p)

        # (a) a plain second render finds no legacy marker at all -- no archive.
        get_adapter("claude").render(venv_bin=venv_bin)
        assert _archives(md) == after_first, "a second render must not mint another archive"
        for p in after_first:
            assert p.stat().st_mtime == _OLD, f"{p.name} was rewritten for nothing"

        # (b) the same content presented again must land on the SAME path with no
        #     write -- which is only true if the name comes from the content.
        md.write_text(_legacy_md_body(), encoding="utf-8")
        get_adapter("claude").render(venv_bin=venv_bin)
        assert _archives(md) == after_first, (
            "re-archiving identical content minted a new file -- the name is not "
            "content-derived (a timestamp or counter crept in)"
        )
        for p in after_first:
            assert p.stat().st_mtime == _OLD, f"{p.name} was rewritten with identical content"

    def test_the_block_is_removed_only_if_it_was_archived(self, fake_home, tmp_path):
        """The invariant, totalised. If BOTH the plain `.bak` and the fallback
        path are held by content that is not ours, there is nowhere left to
        archive -- so the live file is left ALONE. A surviving duplicate
        instruction block is the pre-migration status quo and harmless; removing
        it with no archive anywhere is data loss."""
        md = _write_legacy_md(fake_home)
        bak = md.with_name(md.name + ".bak")
        bak.write_text("someone else's backup -- not ours", encoding="utf-8")
        venv_bin = tmp_path / "venv" / "bin"

        # Discover the fallback path the way a colliding tool would: by observation,
        # not by recomputing the implementation's own naming rule here.
        get_adapter("claude").render(venv_bin=venv_bin)
        fallback = next(p for p in _archives(md) if p != bak)

        # Now stage the double collision: same live file, fallback path occupied.
        md.write_text(_legacy_md_body(), encoding="utf-8")
        fallback.write_text("also not ours", encoding="utf-8")

        get_adapter("claude").render(venv_bin=venv_bin)

        assert "old block" in md.read_text(encoding="utf-8"), (
            "with nowhere to archive, the block must NOT be stripped"
        )
        assert bak.read_text(encoding="utf-8") == "someone else's backup -- not ours"
        assert fallback.read_text(encoding="utf-8") == "also not ours"
        assert len(_archives(md)) == 2, "no third archive may be minted"

    def test_second_render_does_not_re_archive(self, fake_home, tmp_path):
        """render() must be idempotent: once the predecessor block is stripped, a
        second render sees no marker and must not write a second .bak."""
        md = fake_home / ".claude" / "CLAUDE.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        begin_prefix, end_marker = LEGACY_INSTRUCTION_MARKERS[0]
        md.write_text(
            f"# My notes\n\n{begin_prefix} -->\nold block\n{end_marker}\n",
            encoding="utf-8",
        )
        venv_bin = tmp_path / "venv" / "bin"
        get_adapter("claude").render(venv_bin=venv_bin)
        get_adapter("claude").render(venv_bin=venv_bin)

        backups = list(md.parent.glob("CLAUDE.md*.bak"))
        assert len(backups) == 1, "a second render must not re-archive"
