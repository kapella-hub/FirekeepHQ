"""`[generic] agents_md` — the one bit of state that makes generic a lifecycle
runtime rather than a one-shot print.

Two properties are load-bearing here:

  - The probe NEVER has a side effect on the user's config. resolver.load_config()
    raises on a missing file and MIGRATES (backup + atomic rewrite + stderr, and
    possibly ConfigMigrationConflict) when `[server]` is absent — and this probe
    runs on every install and every uninstall. It must be a raw read.
  - `_selected_runtimes` stays PURE. The generic flag is computed at the call
    site and passed in, so the function never depends on a home directory —
    which is what keeps the four's `len(calls) == 4` invariants honest.
"""
from __future__ import annotations

from firekeep_client import cli, resolver


# --- the probe: read-only, never migrates ------------------------------------


def test_generic_is_configured_false_without_section(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "config"))
    (tmp_path / "config").write_text("[identity]\nagent_id = a\n", encoding="utf-8")
    assert cli._generic_is_configured() is False


def test_generic_is_configured_false_without_a_config_at_all(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "nope" / "config"))
    assert cli._generic_is_configured() is False
    assert resolver.generic_agents_md() is None


def test_generic_probe_never_migrates_a_serverless_config(tmp_path, monkeypatch):
    """No [server] section -> load_config would migrate and REWRITE the file."""
    p = tmp_path / "config"
    p.write_text("[identity]\nagent_id = a\n", encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(p))
    before = p.read_bytes()
    cli._generic_is_configured()
    resolver.generic_agents_md()
    assert p.read_bytes() == before  # untouched: raw read, no migration


def test_generic_probe_survives_a_corrupt_config(tmp_path, monkeypatch):
    p = tmp_path / "config"
    p.write_text("this is not INI [[[\n= = =\n", encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(p))
    assert cli._generic_is_configured() is False  # cannot tell -> not configured
    assert resolver.generic_agents_md() is None


def test_blank_agents_md_value_is_not_configured(tmp_path, monkeypatch):
    p = tmp_path / "config"
    p.write_text("[generic]\nagents_md =   \n", encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(p))
    assert cli._generic_is_configured() is False
    assert resolver.generic_agents_md() is None


# --- write / read round trip --------------------------------------------------


def test_set_then_read_generic_agents_md(tmp_path, monkeypatch):
    p = tmp_path / "config"
    p.write_text("[server]\nx = 1\n[identity]\nagent_id = a\n", encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(p))
    resolver.set_generic_agents_md(tmp_path / "AGENTS.md")
    assert resolver.generic_agents_md() == (tmp_path / "AGENTS.md").resolve()
    assert cli._generic_is_configured() is True
    text = p.read_text(encoding="utf-8")
    assert "[server]" in text and "[identity]" in text  # other sections preserved


def test_set_generic_agents_md_stores_an_absolute_resolved_path(tmp_path, monkeypatch):
    p = tmp_path / "config"
    p.write_text("[identity]\nagent_id = a\n", encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(p))
    monkeypatch.chdir(tmp_path)
    resolver.set_generic_agents_md("AGENTS.md")
    stored = resolver.generic_agents_md()
    assert stored is not None and stored.is_absolute()
    assert stored == (tmp_path / "AGENTS.md").resolve()


def test_clear_generic_agents_md_removes_only_that_section(tmp_path, monkeypatch):
    p = tmp_path / "config"
    p.write_text("[server]\nx = 1\n", encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(p))
    resolver.set_generic_agents_md(tmp_path / "AGENTS.md")
    resolver.clear_generic_agents_md()
    assert resolver.generic_agents_md() is None
    assert "[server]" in p.read_text(encoding="utf-8")


def test_clear_is_a_noop_when_never_set(tmp_path, monkeypatch):
    p = tmp_path / "config"
    p.write_text("[server]\nx = 1\n", encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(p))
    before = p.read_bytes()
    resolver.clear_generic_agents_md()
    assert p.read_bytes() == before


# --- selection stays pure -----------------------------------------------------


def test_selected_runtimes_all_excludes_generic_by_default():
    assert cli._selected_runtimes("all") == ["claude", "codex", "kiro", "opencode"]


def test_selected_runtimes_all_includes_generic_when_flagged():
    assert cli._selected_runtimes("all", include_generic=True) == [
        "claude", "codex", "kiro", "opencode", "generic"]


def test_selected_runtimes_single_is_unchanged():
    assert cli._selected_runtimes("generic") == ["generic"]
    assert cli._selected_runtimes("claude") == ["claude"]
    # The flag never widens an explicit single-runtime selection.
    assert cli._selected_runtimes("claude", include_generic=True) == ["claude"]
