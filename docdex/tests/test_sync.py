"""Sync orchestration: the lock, the caps, the bypass gate, and the rules that
keep a sync from deleting or resurrecting something it should not."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from firekeep_client import transport

from firekeep_docdex import sources, state, sync, wire


def _folder(tmp_path, name="notes", files=None):
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    for rel, body in (files or {"a.md": "alpha", "b.md": "beta"}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return root


def _unreachable(*_a, **_kw):
    raise transport.TransportError("POST http://keep.test:8100/... unreachable: refused")


def _server_error(*_a, **_kw):
    raise transport.TransportError("POST ... failed: 503 busy", status=503)


# --- the happy path ---------------------------------------------------------


def test_a_first_sync_ingests_every_supported_file(tmp_path, client, server):
    src = sources.add(_folder(tmp_path))
    summary = sync.run_sync(src.id, client=client)
    one = summary["sources"][0]
    assert one["status"] == "synced"
    assert one["ingested"] == 2
    assert set(server.ingested_names) == {
        wire.source_name(src.id, "a.md"), wire.source_name(src.id, "b.md")
    }
    assert summary["ok"] is True


def test_a_second_sync_ingests_nothing_new(tmp_path, client, server):
    src = sources.add(_folder(tmp_path))
    sync.run_sync(src.id, client=client)
    server.posts.clear()
    summary = sync.run_sync(src.id, client=client)
    assert server.posts == []
    assert summary["sources"][0]["ingested"] == 0


def test_a_changed_file_is_re_ingested(tmp_path, client, server):
    root = _folder(tmp_path)
    src = sources.add(root)
    sync.run_sync(src.id, client=client)
    server.posts.clear()
    (root / "a.md").write_text("alpha, revised", encoding="utf-8")
    sync.run_sync(src.id, client=client)
    assert server.ingested_names == [wire.source_name(src.id, "a.md")]


def test_visibility_travels_from_the_source_record(tmp_path, client, server):
    src = sources.add(_folder(tmp_path), shared=True)
    sync.run_sync(src.id, client=client)
    assert {p["body"]["visibility"] for p in server.posts} == {"workspace"}


def test_sync_all_covers_every_active_source(tmp_path, client, server):
    a = sources.add(_folder(tmp_path, "one", {"x.md": "x"}))
    b = sources.add(_folder(tmp_path, "two", {"y.md": "y"}))
    summary = sync.run_sync(all_sources=True, client=client)
    assert {s["source_id"] for s in summary["sources"]} == {a.id, b.id}


def test_sync_needs_a_target(tmp_path, client):
    with pytest.raises(ValueError, match="source id"):
        sync.run_sync(client=client)


def test_sync_of_an_unknown_source_raises(tmp_path, client):
    with pytest.raises(ValueError, match="unknown source"):
        sync.run_sync("f" * 32, client=client)


def test_last_sync_is_stamped(tmp_path, client):
    src = sources.add(_folder(tmp_path))
    sync.run_sync(src.id, client=client)
    assert state.read_state(src.id).last_sync_at is not None


# --- I4a: no completed walk, no deletions -----------------------------------


def test_a_missing_root_deletes_nothing_and_says_so(tmp_path, client, server):
    root = _folder(tmp_path)
    src = sources.add(root)
    sync.run_sync(src.id, client=client)
    server.deletes.clear()

    for p in root.iterdir():
        p.unlink()
    root.rmdir()

    summary = sync.run_sync(src.id, client=client)["sources"][0]
    assert server.deletes == []          # the drive is unplugged, not emptied
    assert summary["deleted"] == 0
    assert summary["walk_completed"] is False
    assert any("could not be read" in w or "missing" in w.lower()
               for w in summary["warnings"])
    # and the state still knows about both files, so a remount recovers
    assert set(state.read_state(src.id).files) == {"a.md", "b.md"}


def test_an_errored_subtree_is_excluded_from_deletion_inference(
    tmp_path, client, server, monkeypatch
):
    root = _folder(tmp_path, files={"a.md": "a", "locked/b.md": "b"})
    src = sources.add(root)
    sync.run_sync(src.id, client=client)
    server.deletes.clear()

    real_scandir = os.scandir

    def boom(path):
        if str(path).endswith("locked"):
            raise PermissionError(13, "denied")
        return real_scandir(path)

    monkeypatch.setattr("firekeep_docdex.scan.os.scandir", boom)
    summary = sync.run_sync(src.id, client=client)["sources"][0]
    assert server.deletes == []
    assert summary["deleted"] == 0
    assert "locked/b.md" in state.read_state(src.id).files


def test_a_genuinely_deleted_file_is_deleted_on_the_server(tmp_path, client, server):
    root = _folder(tmp_path)
    src = sources.add(root)
    sync.run_sync(src.id, client=client)
    server.deletes.clear()

    (root / "b.md").unlink()
    summary = sync.run_sync(src.id, client=client)["sources"][0]
    assert summary["deleted"] == 1
    assert server.deletes[0]["url"].endswith(wire.source_name(src.id, "b.md"))
    assert "b.md" not in state.read_state(src.id).files


def test_a_failed_delete_stays_visibly_pending_and_retries(tmp_path, client, server):
    root = _folder(tmp_path)
    src = sources.add(root)
    sync.run_sync(src.id, client=client)
    (root / "b.md").unlink()

    server.delete_hook = lambda *_a: _server_error()
    summary = sync.run_sync(src.id, client=client)["sources"][0]
    assert summary["deleted"] == 0
    assert summary["pending_delete"] == 1
    assert state.read_state(src.id).files["b.md"].pending_delete is True

    server.delete_hook = None
    summary = sync.run_sync(src.id, client=client)["sources"][0]
    assert summary["deleted"] == 1
    assert "b.md" not in state.read_state(src.id).files


def test_a_404_on_delete_counts_as_confirmed(tmp_path, client, server):
    """The replica is gone — which is the outcome we wanted. Treating this as
    a failure would tombstone the file forever."""
    root = _folder(tmp_path)
    src = sources.add(root)
    sync.run_sync(src.id, client=client)
    (root / "b.md").unlink()
    server.delete_hook = lambda *_a: (_ for _ in ()).throw(
        transport.TransportError("404", status=404)
    )
    summary = sync.run_sync(src.id, client=client)["sources"][0]
    assert summary["deleted"] == 1
    assert "b.md" not in state.read_state(src.id).files


def test_an_oversize_file_is_skipped_but_its_replica_is_not_deleted(
    tmp_path, client, server, monkeypatch
):
    root = _folder(tmp_path)
    src = sources.add(root)
    sync.run_sync(src.id, client=client)
    server.deletes.clear()

    (root / "b.md").write_bytes(b"x" * (2 * 1024 * 1024))
    monkeypatch.setenv("FIREKEEP_DOCDEX_MAX_FILE_MB", "1")
    summary = sync.run_sync(src.id, client=client)["sources"][0]
    assert summary["skipped_oversize"] == 1
    assert summary["deleted"] == 0
    assert server.deletes == []


# --- the caps ---------------------------------------------------------------


def test_max_files_refuses_the_source_loudly_and_changes_nothing(
    tmp_path, client, server, monkeypatch
):
    src = sources.add(_folder(tmp_path, files={f"f{i}.md": str(i) for i in range(6)}))
    monkeypatch.setenv("FIREKEEP_DOCDEX_MAX_FILES", "3")
    summary = sync.run_sync(src.id, client=client)
    one = summary["sources"][0]
    assert one["status"] == "refused"
    assert server.posts == []                     # no silent subset
    assert not state.state_path(src.id).exists()  # nothing recorded
    assert summary["ok"] is False
    assert any("5000" in w or "cap" in w.lower() or "narrow" in w.lower()
               for w in one["warnings"])


def test_max_extract_kb_truncates_and_flags(tmp_path, client, server, monkeypatch):
    src = sources.add(_folder(tmp_path, files={"big.md": "y" * 5000}))
    monkeypatch.setenv("FIREKEEP_DOCDEX_MAX_EXTRACT_KB", "1")
    summary = sync.run_sync(src.id, client=client)["sources"][0]
    assert summary["truncated"] == 1
    assert len(server.posts[0]["body"]["content"].encode("utf-8")) <= 1024
    assert state.read_state(src.id).files["big.md"].truncated is True


def test_unsupported_files_are_counted_not_sent(tmp_path, client, server):
    src = sources.add(_folder(tmp_path, files={"a.md": "a", "b.rtf": "b"}))
    summary = sync.run_sync(src.id, client=client)["sources"][0]
    assert summary["skipped_unsupported"] == 1
    assert len(server.posts) == 1


# --- the seen/ingested split, end to end ------------------------------------


def test_an_honest_zero_is_never_re_extracted(tmp_path, client, server, docs):
    root = tmp_path / "scans"
    root.mkdir()
    (root / "scanned.pdf").write_bytes((docs / "scanned.pdf").read_bytes())
    src = sources.add(root)

    summary = sync.run_sync(src.id, client=client)["sources"][0]
    assert server.posts == []            # nothing to send
    assert summary["failed"] == 0        # and nothing failed
    fs = state.read_state(src.id).files["scanned.pdf"]
    assert fs.seen_hash and fs.ingested_hash is None and fs.error is None

    summary = sync.run_sync(src.id, client=client)["sources"][0]
    assert summary["ingested"] == 0 and summary["failed"] == 0


def test_a_transient_server_error_is_retried_next_sync(tmp_path, client, server):
    src = sources.add(_folder(tmp_path, files={"a.md": "alpha"}))
    server.post_hook = lambda *_a: _server_error()
    summary = sync.run_sync(src.id, client=client)["sources"][0]
    assert summary["failed"] == 1
    assert state.read_state(src.id).files["a.md"].error

    server.post_hook = None
    summary = sync.run_sync(src.id, client=client)["sources"][0]
    assert summary["ingested"] == 1
    assert state.read_state(src.id).files["a.md"].error is None


# --- unreachable server -----------------------------------------------------


def test_an_unreachable_server_aborts_cleanly_leaving_state_untouched(
    tmp_path, client, server
):
    src = sources.add(_folder(tmp_path))
    server.post_hook = lambda *_a: _unreachable()
    summary = sync.run_sync(src.id, client=client)
    assert summary["ok"] is False
    assert summary["aborted"]
    assert summary["sources"][0]["status"] == "aborted"
    assert not state.state_path(src.id).exists()


def test_an_unreachable_server_mid_run_keeps_what_was_earned(tmp_path, client, server):
    """State is a factual claim about the server. A file that DID land is
    recorded, because saying otherwise would be a lie that costs a re-ingest."""
    src = sources.add(_folder(tmp_path, files={f"f{i}.md": str(i) for i in range(4)}))
    server.post_hook = lambda i, *_a: _unreachable() if i >= 2 else None
    summary = sync.run_sync(src.id, client=client)
    assert summary["sources"][0]["status"] == "aborted"
    recorded = state.read_state(src.id)
    assert len(recorded.files) == 2
    assert recorded.last_sync_at is None  # an aborted run is not a sync


def test_an_unreachable_server_stops_the_whole_run(tmp_path, client, server):
    sources.add(_folder(tmp_path, "one", {"x.md": "x"}))
    sources.add(_folder(tmp_path, "two", {"y.md": "y"}))
    server.post_hook = lambda *_a: _unreachable()
    summary = sync.run_sync(all_sources=True, client=client)
    assert len(summary["sources"]) == 1  # the second source is not attempted
    assert summary["aborted"]


def test_an_unresolvable_config_aborts_without_raising(tmp_path, firekeep_home):
    """A kit that was never connected is not a crash — the hook spawns this
    process on machines that may not be enrolled yet."""
    sources.add(_folder(tmp_path))
    summary = sync.run_sync(all_sources=True)  # no client, no config on disk
    assert summary["ok"] is False
    assert summary["aborted"].startswith("cannot reach the Keep")
    assert summary["sources"] == []


# --- I3: private-session mode suspends sync ---------------------------------


def test_bypass_prevents_a_sync_from_starting(tmp_path, client, server, monkeypatch):
    src = sources.add(_folder(tmp_path))
    monkeypatch.setattr("firekeep_client.resolver.is_bypassed", lambda *a, **k: True)
    summary = sync.run_sync(src.id, client=client)
    assert server.posts == []
    assert summary["aborted"] and "private-session" in summary["aborted"]


def test_bypass_suspends_a_run_already_in_flight(tmp_path, client, server, monkeypatch):
    """I3 is explicit that "fully bypassed" includes background uploads: the
    flag is re-checked between batches, not only at the start."""
    src = sources.add(_folder(tmp_path, files={f"f{i}.md": str(i) for i in range(6)}))
    monkeypatch.setattr(sync, "BATCH_SIZE", 2)

    # The human types /personal once two files have gone up. Keyed off actual
    # uploads rather than a call count, so the test says what it means and does
    # not silently depend on how many times the gate happens to be consulted.
    monkeypatch.setattr(sync, "_bypassed", lambda: len(server.posts) >= 2)
    summary = sync.run_sync(src.id, client=client)
    assert len(server.posts) == 2                       # exactly the first batch
    assert summary["sources"][0]["status"] == "aborted"
    assert "suspended" in summary["aborted"]


def test_the_env_bypass_is_honored(tmp_path, client, server, monkeypatch):
    src = sources.add(_folder(tmp_path))
    monkeypatch.setenv("FIREKEEP_BYPASS", "1")
    sync.run_sync(src.id, client=client)
    assert server.posts == []


# --- the per-source lock ----------------------------------------------------


def test_a_held_lock_makes_sync_stand_down(tmp_path, client, server):
    src = sources.add(_folder(tmp_path))
    with sync.source_lock(src.id):
        summary = sync.run_sync(src.id, client=client)["sources"][0]
    assert summary["status"] == "locked"
    assert server.posts == []


def test_the_lock_is_released_on_the_way_out(tmp_path, client):
    src = sources.add(_folder(tmp_path))
    sync.run_sync(src.id, client=client)
    with sync.source_lock(src.id):  # no raise
        pass


def test_a_stale_lock_is_broken(tmp_path, client, server):
    src = sources.add(_folder(tmp_path))
    lock = sync.lock_path(src.id)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("dead process", encoding="utf-8")
    os.utime(lock, (0, 0))  # 1970 — far older than the staleness window
    summary = sync.run_sync(src.id, client=client)["sources"][0]
    assert summary["status"] == "synced"


def test_a_fresh_lock_is_not_broken(tmp_path, client):
    src = sources.add(_folder(tmp_path))
    lock = sync.lock_path(src.id)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("live process", encoding="utf-8")
    with pytest.raises(sync.LockBusy):
        with sync.source_lock(src.id):
            pass


# --- remove: the anti-resurrection ordering ---------------------------------


def test_remove_marks_pending_delete_before_it_needs_the_lock(tmp_path, client):
    """The mark comes FIRST so an in-flight sync can see it. If remove had to
    win the lock before marking, a long sync would keep re-uploading content
    the human has already asked to be gone."""
    src = sources.add(_folder(tmp_path))
    with sync.source_lock(src.id):
        result = sync.remove_source(src.id, client=client)
    assert result["status"] == "locked"
    assert sources.get(src.id).status == sources.PENDING_DELETE


def test_remove_racing_a_sync_cannot_resurrect_content(tmp_path, client, server, monkeypatch):
    """The load-bearing race (spec §6). A remove landing mid-sync flips the
    source to pending_delete; the sync re-reads the registry before every
    ingest batch and stops, so the content the human deleted does not get
    re-uploaded behind the delete."""
    src = sources.add(_folder(tmp_path, files={f"f{i}.md": str(i) for i in range(6)}))
    monkeypatch.setattr(sync, "BATCH_SIZE", 2)

    def remove_after_first_batch(index, url, body):
        if index == 1:
            sources.remove_mark(src.id)  # the concurrent `docdex remove`
        return None

    server.post_hook = remove_after_first_batch
    summary = sync.run_sync(src.id, client=client)["sources"][0]
    assert len(server.posts) == 2                  # the first batch, and no more
    assert summary["status"] == "aborted"
    assert sources.get(src.id).status == sources.PENDING_DELETE


def test_remove_bulk_deletes_then_drops_the_source(tmp_path, client, server):
    src = sources.add(_folder(tmp_path))
    sync.run_sync(src.id, client=client)
    server.delete_hook = lambda *_a: {"deleted_sources": 2, "deleted_chunks": "all"}
    result = sync.remove_source(src.id, client=client)
    assert result["status"] == "removed"
    assert server.deletes[-1]["url"].endswith(f"/corpus/dex-sources/{src.id}")
    # The count is the SERVER's, not a local guess — one bulk call removed both
    # replicas, and reporting "1" would be reporting the request, not the work.
    assert result["deleted"] == 2
    assert sources.get(src.id) is None
    assert not state.state_path(src.id).exists()


def test_removing_a_never_synced_source_reports_zero_deleted(tmp_path, client, server):
    src = sources.add(_folder(tmp_path))
    server.delete_hook = lambda *_a: (_ for _ in ()).throw(
        transport.TransportError("404 Unknown source", status=404)
    )
    assert sync.remove_source(src.id, client=client)["deleted"] == 0


def test_a_failed_bulk_delete_keeps_the_source_pending_and_retries_on_sync(
    tmp_path, client, server
):
    src = sources.add(_folder(tmp_path))
    sync.run_sync(src.id, client=client)
    server.delete_hook = lambda *_a: _server_error()
    result = sync.remove_source(src.id, client=client)
    assert result["status"] == "remove_pending"
    assert sources.get(src.id).status == sources.PENDING_DELETE

    server.delete_hook = None
    summary = sync.run_sync(all_sources=True, client=client)["sources"][0]
    assert summary["status"] == "removed"
    assert sources.get(src.id) is None


def test_a_pending_delete_source_is_never_ingested(tmp_path, client, server):
    src = sources.add(_folder(tmp_path))
    sources.remove_mark(src.id)
    sync.run_sync(all_sources=True, client=client)
    assert server.posts == []


def test_removing_a_never_synced_source_still_drops_it(tmp_path, client, server):
    """A 404 means the server holds nothing under this id — which is exactly
    the state removal wants. Anything else strands the source forever."""
    src = sources.add(_folder(tmp_path))
    server.delete_hook = lambda *_a: (_ for _ in ()).throw(
        transport.TransportError("404 Unknown source", status=404)
    )
    assert sync.remove_source(src.id, client=client)["status"] == "removed"
    assert sources.get(src.id) is None


def test_remove_of_an_unknown_source_raises(tmp_path, client):
    with pytest.raises(ValueError, match="unknown source"):
        sync.remove_source("e" * 32, client=client)


# --- the module entrypoint (the detached-spawn target) ----------------------


def test_main_syncs_all(tmp_path, monkeypatch, client, server, capsys):
    src = sources.add(_folder(tmp_path))
    monkeypatch.setattr(sync, "_make_client", lambda: client)
    assert sync.main(["--all"]) == 0
    assert len(server.posts) == 2
    assert src.id[:8] in capsys.readouterr().out


def test_main_quiet_prints_nothing(tmp_path, monkeypatch, client, server, capsys):
    sources.add(_folder(tmp_path))
    monkeypatch.setattr(sync, "_make_client", lambda: client)
    assert sync.main(["--all", "--quiet"]) == 0
    assert capsys.readouterr().out == ""


def test_main_reports_a_nonzero_exit_when_a_source_is_refused(
    tmp_path, monkeypatch, client, server
):
    sources.add(_folder(tmp_path, files={f"f{i}.md": str(i) for i in range(6)}))
    monkeypatch.setenv("FIREKEEP_DOCDEX_MAX_FILES", "3")
    monkeypatch.setattr(sync, "_make_client", lambda: client)
    assert sync.main(["--all", "--quiet"]) == 1


def test_main_needs_a_target(monkeypatch, client):
    monkeypatch.setattr(sync, "_make_client", lambda: client)
    assert sync.main([]) == 2


def test_main_never_raises_into_a_detached_process(tmp_path, monkeypatch, client):
    """`main` is what the session-start hook spawns. A traceback out of it is
    a background process that dies without a trace."""
    sources.add(_folder(tmp_path))

    def boom():
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(sync, "_make_client", boom)
    assert sync.main(["--all", "--quiet"]) == 1
