"""The read-only IMAP session, driven against a spy that raises on every
mutating verb (M2).

`SpyIMAP` is the point of this file. It is not a stub that happens not to
implement `store` — it implements it, and it raises. Anything maildex ever
does that would touch a mailbox surfaces here as a MutationAttempted naming
the verb, in whichever test drove the path.
"""
from __future__ import annotations

import datetime

import pytest
from conftest import PASSWORD, MutationAttempted, SpyIMAP, connector_for, make_message

from firekeep_maildex import imapio


@pytest.fixture
def sess(spy):
    with imapio.session("imap.example.com", 993, "me@example.com", PASSWORD,
                        connector=connector_for(spy)) as s:
        yield s


# --- M2: EXAMINE, always ----------------------------------------------------


def test_examine_opens_the_mailbox_readonly(sess, spy):
    sess.examine("INBOX")
    assert spy.selects == [("INBOX", True)]


def test_every_select_in_a_whole_sync_shaped_run_is_readonly(sess, spy):
    for folder in ("INBOX", "Sent", "INBOX"):
        sess.examine(folder)
    assert all(readonly is True for _, readonly in spy.selects)


def test_the_spy_actually_raises_on_a_mutating_verb():
    """The guard's own guard. A spy that silently accepted `store` would make
    every M2 test in this file pass vacuously."""
    spy = SpyIMAP({"INBOX": {}})
    with pytest.raises(MutationAttempted, match="STORE"):
        spy.store("1", "+FLAGS", r"\Seen")
    with pytest.raises(MutationAttempted, match="APPEND"):
        spy.append("INBOX", None, None, b"x")
    with pytest.raises(MutationAttempted, match="EXPUNGE"):
        spy.expunge()


def test_a_full_read_cycle_never_calls_a_mutating_verb(sess, spy):
    """The whole conversation a sync makes — open, search, fetch — against a
    connection that would raise if any of it mutated anything."""
    sess.examine("INBOX")
    for uid in sess.search_after(0):
        sess.fetch(uid)
    assert [uid for uid, _ in spy.fetches] == [1, 2]


def test_uid_rejects_any_command_that_is_not_a_read(sess, spy):
    with pytest.raises(MutationAttempted):
        spy.uid("MOVE", "1", "Archive")


# --- M2: PEEK ---------------------------------------------------------------


def test_every_fetch_peeks(sess, spy):
    """A plain BODY[] fetch sets \\Seen — reading the Keep's copy must not mark
    a person's mail as read."""
    sess.examine("INBOX")
    sess.fetch(1)
    sess.fetch(2)
    assert [spec for _, spec in spy.fetches] == ["(BODY.PEEK[])", "(BODY.PEEK[])"]


def test_fetch_returns_the_raw_message_bytes(sess, spy):
    sess.examine("INBOX")
    raw = sess.fetch(1)
    assert raw.startswith(b"Subject: One") or b"Subject: One" in raw


# --- UIDVALIDITY (M7) -------------------------------------------------------


def test_examine_returns_the_folders_uidvalidity(sess, spy):
    spy.uidvalidity["INBOX"] = 4242
    assert sess.examine("INBOX") == 4242


def test_a_server_that_reports_no_uidvalidity_is_refused(sess, spy):
    """Watermarks we cannot trust are worse than no watermarks: they skip mail
    silently. Refusing the folder is the honest failure."""
    spy.uidvalidity["INBOX"] = "not-a-number"
    with pytest.raises(imapio.ImapError, match="UIDVALIDITY"):
        sess.examine("INBOX")


# --- search -----------------------------------------------------------------


def test_search_since_sends_a_locale_independent_date(sess, spy):
    sess.examine("INBOX")
    sess.search_since(datetime.date(2026, 5, 3))
    assert spy.searches[-1] == ("SINCE", "03-May-2026")


def test_imap_date_never_uses_the_machines_locale():
    """`strftime("%b")` on a German workstation produces `Okt`, which the
    server answers with a parse error rather than a smaller result set."""
    assert imapio.imap_date(datetime.date(2026, 10, 1)) == "01-Oct-2026"
    assert imapio.imap_date(datetime.date(2026, 12, 31)) == "31-Dec-2026"


def test_search_after_asks_for_everything_above_the_watermark(sess, spy):
    sess.examine("INBOX")
    sess.search_after(1)
    assert spy.searches[-1] == ("UID 2:*",)


def test_search_after_filters_the_uid_the_protocol_always_returns(sess, spy):
    """`UID n:*` is SPECIFIED to answer with the mailbox's highest UID even
    when it is below n. Without the client-side filter, every sync of a quiet
    mailbox would re-fetch and re-ingest its newest message forever."""
    sess.examine("INBOX")
    assert sess.search_after(2) == []
    # ...and the spy really did hand back the trailing UID, so the filter is
    # what produced the empty list rather than an empty server answer.
    assert spy.searches[-1] == ("UID 3:*",)


def test_search_results_are_sorted_and_deduplicated(sess, spy):
    spy.search_hook = lambda args: ("OK", [b"7 3 3 5"])
    sess.examine("INBOX")
    assert sess.search_since(datetime.date(2026, 1, 1)) == [3, 5, 7]


def test_an_empty_search_result_is_an_empty_list(sess, spy):
    spy.search_hook = lambda args: ("OK", [b""])
    sess.examine("INBOX")
    assert sess.search_since(datetime.date(2026, 1, 1)) == []


def test_a_search_result_of_none_is_an_empty_list(sess, spy):
    """Some servers answer an empty SEARCH with `[None]`."""
    spy.search_hook = lambda args: ("OK", [None])
    sess.examine("INBOX")
    assert sess.search_since(datetime.date(2026, 1, 1)) == []


# --- folder names -----------------------------------------------------------


def test_a_folder_name_with_a_space_is_quoted():
    """`[Gmail]/Sent Mail` is the common case a naive implementation breaks on:
    imaplib passes the mailbox through verbatim, so an unquoted space becomes
    two arguments."""
    spy = SpyIMAP({"[Gmail]/Sent Mail": {1: make_message()}})
    with imapio.session("h", 993, "u", PASSWORD, connector=connector_for(spy)) as s:
        assert s.examine("[Gmail]/Sent Mail") == 900
    assert spy.selects == [("[Gmail]/Sent Mail", True)]


def test_a_plain_folder_name_is_not_quoted(sess, spy):
    sess.examine("INBOX")
    assert spy.selects[-1][0] == "INBOX"


def test_a_folder_name_with_a_quote_in_it_is_escaped():
    spy = SpyIMAP({'Odd"Name': {}})
    with imapio.session("h", 993, "u", PASSWORD, connector=connector_for(spy)) as s:
        s.examine('Odd"Name')
    assert spy.selects == [('Odd"Name', True)]


def test_a_missing_folder_is_an_imap_error_not_a_crash(sess):
    with pytest.raises(imapio.ImapError, match="Archive"):
        sess.examine("Archive")


# --- connection lifecycle ---------------------------------------------------


def test_the_session_logs_in_and_always_logs_out(spy):
    with imapio.session("imap.example.com", 993, "me@example.com", PASSWORD,
                        connector=connector_for(spy)):
        assert spy.logins == [("me@example.com", PASSWORD)]
    assert spy.logged_out


def test_the_session_logs_out_even_when_the_body_raises(spy):
    with pytest.raises(RuntimeError):
        with imapio.session("h", 993, "u", PASSWORD, connector=connector_for(spy)):
            raise RuntimeError("boom")
    assert spy.logged_out


def test_a_rejected_password_is_an_auth_error_not_a_generic_one(spy):
    """An expired app password needs re-adding the mailbox, not retrying the
    sync — so the two failures cannot share a type."""
    spy.login = lambda u, p: ("NO", [b"AUTHENTICATIONFAILED"])
    with pytest.raises(imapio.AuthError, match="rejected"):
        with imapio.session("h", 993, "u", PASSWORD, connector=connector_for(spy)):
            pass


def test_a_raising_login_is_also_an_auth_error(spy):
    def refuse(u, p):
        raise Exception("[AUTHENTICATIONFAILED] Invalid credentials")

    spy.login = refuse
    with pytest.raises(imapio.AuthError):
        with imapio.session("h", 993, "u", PASSWORD, connector=connector_for(spy)):
            pass


def test_an_unreachable_host_is_an_imap_error(spy):
    connect = connector_for(spy, fail=OSError("Name or service not known"))
    with pytest.raises(imapio.ImapError, match="cannot connect"):
        with imapio.session("nope.test", 993, "u", PASSWORD, connector=connect):
            pass


def test_the_password_is_never_stored_on_the_session(sess):
    """M3 at the object level: nothing in `imapio` holds the secret, so nothing
    in `imapio` can leak it into a repr or a pickled traceback."""
    assert PASSWORD not in repr(sess.__dict__)
    for value in vars(sess).values():
        assert value is not PASSWORD


def test_a_vanished_uid_is_reported_rather_than_returning_empty_bytes(sess, spy):
    """A UID that disappeared between SEARCH and FETCH answers OK with no
    literal. That is a message the human deleted a moment ago."""
    sess.examine("INBOX")
    with pytest.raises(imapio.ImapError, match="no message body"):
        sess.fetch(999)
