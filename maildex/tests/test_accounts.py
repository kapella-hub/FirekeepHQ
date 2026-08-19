"""The mailbox registry, and the absence at its centre.

Half of these tests assert something is NOT there. That is the point: M3 is a
property of what `accounts.json` does not contain, and a property nobody
asserts is a property that comes back.
"""
from __future__ import annotations

import json

import pytest

from firekeep_maildex import accounts

SECRET = "correct-horse-battery-staple"


# --- M3: no secret on disk --------------------------------------------------


def test_add_has_no_password_parameter():
    """The signature itself is the guard. A function that accepts a password is
    one somebody eventually calls with an argv value."""
    import inspect

    names = set(inspect.signature(accounts.add).parameters)
    assert not names & {"password", "secret", "app_password", "token", "pass"}


def test_the_registry_file_never_contains_a_secret():
    accounts.add("imap.example.com", "me@example.com")
    raw = accounts.accounts_path().read_text(encoding="utf-8")
    assert SECRET not in raw
    stored = json.loads(raw)
    for entry in stored.values():
        assert set(entry) == {"host", "port", "username", "folders",
                              "backfill_days", "added_at", "status"}


def test_the_registry_holds_only_what_a_person_would_read_aloud():
    account = accounts.add("imap.example.com", "me@example.com")
    entry = json.loads(accounts.accounts_path().read_text(encoding="utf-8"))[account.id]
    assert entry["host"] == "imap.example.com"
    assert entry["username"] == "me@example.com"
    assert entry["port"] == 993


# --- M1: member-private, structurally ---------------------------------------


def test_visibility_is_always_member_and_is_not_stored():
    account = accounts.add("imap.example.com", "me@example.com")
    assert account.visibility == "member"
    entry = json.loads(accounts.accounts_path().read_text(encoding="utf-8"))[account.id]
    assert "visibility" not in entry


def test_add_has_no_shared_parameter():
    """docdex has `--shared`; maildex must not. Sharing a mailbox is a
    different dex, not a flag on this one (M1)."""
    import inspect

    names = set(inspect.signature(accounts.add).parameters)
    assert not names & {"shared", "visibility", "workspace"}


# --- identity ---------------------------------------------------------------


def test_the_id_is_128_bits_of_randomness_not_derived_from_the_address():
    a = accounts.add("imap.example.com", "me@example.com")
    accounts.drop(a.id)
    b = accounts.add("imap.example.com", "me@example.com")
    assert a.id != b.id
    assert len(a.id) == 32
    assert "example" not in a.id


def test_the_id_never_leaks_the_address():
    account = accounts.add("imap.example.com", "priya@example.com")
    assert "priya" not in account.id


# --- validation -------------------------------------------------------------


def test_a_blank_host_is_refused():
    with pytest.raises(ValueError, match="host"):
        accounts.add("  ", "me@example.com")


def test_a_blank_username_is_refused():
    with pytest.raises(ValueError, match="username"):
        accounts.add("imap.example.com", "")


@pytest.mark.parametrize("port", [0, -1, 70000, "imap"])
def test_a_nonsense_port_is_refused(port):
    with pytest.raises(ValueError, match="port"):
        accounts.add("imap.example.com", "me@example.com", port=port)


def test_the_same_mailbox_cannot_be_registered_twice():
    """Two ids over one mailbox would index every message twice under two
    source names, and only a human could tell which replica to keep."""
    accounts.add("imap.example.com", "me@example.com")
    with pytest.raises(ValueError, match="already registered"):
        accounts.add("imap.example.com", "me@example.com")


def test_the_same_address_on_a_different_host_is_a_different_mailbox():
    accounts.add("imap.example.com", "me@example.com")
    other = accounts.add("imap.other.test", "me@example.com")
    assert other.id


def test_a_mailbox_pending_removal_can_be_re_added():
    """The M5 manual rebuild: `remove` then `add` is how a person re-indexes a
    mailbox, and it must not trip the duplicate guard while the removal is
    still settling."""
    first = accounts.add("imap.example.com", "me@example.com")
    accounts.remove_mark(first.id)
    second = accounts.add("imap.example.com", "me@example.com")
    assert second.id != first.id


def test_a_zero_backfill_is_refused():
    with pytest.raises(ValueError, match="backfill"):
        accounts.add("imap.example.com", "me@example.com", backfill_days=0)


# --- folders ----------------------------------------------------------------


def test_folders_default_to_inbox_and_sent():
    account = accounts.add("imap.example.com", "me@example.com")
    assert account.folders == ("INBOX", "Sent")


def test_folders_accept_a_comma_separated_string():
    account = accounts.add("imap.example.com", "me@example.com",
                           folders="INBOX, Archive ,Work/2026")
    assert account.folders == ("INBOX", "Archive", "Work/2026")


def test_folders_accept_the_list_the_client_bridge_builds():
    """`firekeep maildex add --folders INBOX Archive` arrives as a list — the
    bridge's own option is `nargs="+"`, and it forwards the values verbatim."""
    account = accounts.add("imap.example.com", "me@example.com",
                           folders=["INBOX", "[Gmail]/Sent Mail"])
    assert account.folders == ("INBOX", "[Gmail]/Sent Mail")


def test_folders_accept_the_two_spellings_mixed():
    """Somebody who types `--folders INBOX,Archive Work` is not making a
    mistake worth a parse error."""
    account = accounts.add("imap.example.com", "me@example.com",
                           folders=["INBOX,Archive", "Work"])
    assert account.folders == ("INBOX", "Archive", "Work")


def test_duplicate_folders_are_collapsed():
    """The same folder listed twice would be opened twice per sync and burn the
    per-run message budget on messages already indexed."""
    account = accounts.add("imap.example.com", "me@example.com",
                           folders="INBOX,INBOX, INBOX ")
    assert account.folders == ("INBOX",)


def test_an_all_blank_folder_list_falls_back_to_the_default():
    account = accounts.add("imap.example.com", "me@example.com", folders=" , , ")
    assert account.folders == accounts.DEFAULT_FOLDERS


# --- caps -------------------------------------------------------------------


def test_the_backfill_horizon_defaults_to_90_days():
    assert accounts.add("imap.example.com", "me@example.com").backfill_days == 90


def test_the_backfill_horizon_honours_its_env_override(monkeypatch):
    monkeypatch.setenv("FIREKEEP_MAILDEX_BACKFILL_DAYS", "30")
    assert accounts.add("imap.example.com", "me@example.com").backfill_days == 30


def test_the_backfill_horizon_is_frozen_onto_the_account(monkeypatch):
    """A horizon that moved with an env var would make "why is that March
    email missing?" unanswerable."""
    monkeypatch.setenv("FIREKEEP_MAILDEX_BACKFILL_DAYS", "30")
    account = accounts.add("imap.example.com", "me@example.com")
    monkeypatch.setenv("FIREKEEP_MAILDEX_BACKFILL_DAYS", "365")
    assert accounts.get(account.id).backfill_days == 30


def test_a_nonsense_env_cap_falls_back_to_the_documented_default(monkeypatch):
    monkeypatch.setenv("FIREKEEP_MAILDEX_BACKFILL_DAYS", "soon")
    assert accounts.add("imap.example.com", "me@example.com").backfill_days == 90


# --- lifecycle --------------------------------------------------------------


def test_get_returns_none_for_an_unknown_id():
    assert accounts.get("nope") is None


def test_list_is_in_registration_order():
    first = accounts.add("imap.a.test", "me@example.com")
    second = accounts.add("imap.b.test", "me@example.com")
    assert [a.id for a in accounts.list_accounts()] == [first.id, second.id]


def test_remove_mark_is_idempotent():
    account = accounts.add("imap.example.com", "me@example.com")
    accounts.remove_mark(account.id)
    accounts.remove_mark(account.id)
    assert accounts.get(account.id).status == accounts.PENDING_DELETE


def test_remove_mark_refuses_an_unknown_id():
    with pytest.raises(ValueError, match="unknown account"):
        accounts.remove_mark("nope")


def test_drop_forgets_the_account():
    account = accounts.add("imap.example.com", "me@example.com")
    accounts.drop(account.id)
    assert accounts.get(account.id) is None


def test_rollback_undoes_a_registration_that_never_completed():
    account = accounts.add("imap.example.com", "me@example.com")
    accounts.rollback(account.id)
    assert accounts.list_accounts() == []


# --- durability -------------------------------------------------------------


def test_a_corrupt_registry_reads_as_empty_and_is_left_in_place():
    """The bad file is the only evidence of whatever produced it."""
    accounts.add("imap.example.com", "me@example.com")
    accounts.accounts_path().write_text("{not json", encoding="utf-8")
    assert accounts.read_accounts() == {}
    assert accounts.accounts_path().read_text(encoding="utf-8") == "{not json"


def test_a_registry_entry_missing_fields_still_reads():
    """A file written by an older version must not crash `list`."""
    accounts.write_accounts({"abc": {"host": "imap.example.com"}})
    account = accounts.get("abc")
    assert account.port == 993
    assert account.folders == accounts.DEFAULT_FOLDERS
    assert account.status == accounts.ACTIVE


def test_the_write_is_atomic_and_leaves_no_temp_file():
    accounts.add("imap.example.com", "me@example.com")
    leftovers = list(accounts.accounts_path().parent.glob("*.tmp-*"))
    assert leftovers == []
