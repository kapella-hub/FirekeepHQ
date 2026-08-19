"""The app password's custody (M3).

The vault is the only place the secret lives. These tests pin the two things
that make that true in practice: the key shape the server will actually accept,
and the parsing of what `vault_retrieve` returns — which is markdown for a
human, not JSON, and is the seam most likely to break silently.
"""
from __future__ import annotations

import re

import pytest

from firekeep_maildex import vault

ACCOUNT = "0123456789abcdef" * 2

# What `vault/store.py` enforces server-side. Copied here so a change to the
# key shape fails in this wheel's own suite rather than as a 400 on a live add.
SERVER_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9\-_.]{1,200}$")


def test_the_key_is_accepted_by_the_servers_own_pattern():
    """The spec writes `maildex/<id>`; the vault rejects a slash and the REST
    route would be reshaped by one. A dot namespaces it identically."""
    assert SERVER_KEY_PATTERN.match(vault.vault_key(ACCOUNT))


def test_the_key_namespaces_by_account():
    assert vault.vault_key(ACCOUNT) == f"maildex.{ACCOUNT}"
    assert vault.vault_key("other") != vault.vault_key(ACCOUNT)


def test_retrieve_lifts_the_value_out_of_the_tools_markdown(fake_vault):
    fake_vault.secrets[vault.vault_key(ACCOUNT)] = "s3cret"
    assert vault.retrieve(ACCOUNT, call_tool=fake_vault) == "s3cret"


def test_retrieve_asks_cortex_for_exactly_this_accounts_key(fake_vault):
    fake_vault.secrets[vault.vault_key(ACCOUNT)] = "s3cret"
    vault.retrieve(ACCOUNT, call_tool=fake_vault)
    assert fake_vault.calls == [
        ("cortex", "vault_retrieve", {"key": vault.vault_key(ACCOUNT)})
    ]


def test_a_password_containing_the_markdown_marker_survives_the_round_trip(fake_vault):
    """An app password is arbitrary text. Anchoring the match to the line start
    is what keeps a value containing "- **Value:** " from shifting it."""
    tricky = "a- **Value:** b"
    fake_vault.secrets[vault.vault_key(ACCOUNT)] = tricky
    assert vault.retrieve(ACCOUNT, call_tool=fake_vault) == tricky


def test_a_missing_secret_is_its_own_error(fake_vault):
    """"The Keep is down" and "you never stored a password" need different
    words and different actions from the person reading them."""
    with pytest.raises(vault.VaultMissing, match="no app password"):
        vault.retrieve(ACCOUNT, call_tool=fake_vault)


def test_a_transport_failure_is_a_vault_error(fake_vault):
    def boom(*a, **k):
        raise RuntimeError("connection refused")

    with pytest.raises(vault.VaultError, match="refused"):
        vault.retrieve(ACCOUNT, call_tool=boom)


def test_a_refusal_is_named_as_one(fake_vault):
    """Storing a secret is admin-scoped. "ask your Keep admin" and "your
    network is broken" are different problems."""
    fake_vault.refuse_store = True
    with pytest.raises(vault.VaultRefused, match="admin"):
        vault.store(ACCOUNT, "s3cret", call_tool=fake_vault)


def test_an_empty_stored_value_is_refused_rather_than_handed_to_login(fake_vault):
    """An empty password reaches IMAP as a failed authentication, which sends
    the human looking at their provider instead of at the vault."""
    fake_vault.secrets[vault.vault_key(ACCOUNT)] = ""
    with pytest.raises(vault.VaultError, match="empty"):
        vault.retrieve(ACCOUNT, call_tool=fake_vault)


def test_an_unparseable_answer_never_quotes_the_secret_back(fake_vault):
    """An error string travels into hooklog, into stderr, and into whatever
    collects them. A secret that reaches a log is a secret on disk."""
    def weird(*a, **k):
        return "## Secret: x\nValue is s3cret-in-the-wrong-shape"

    with pytest.raises(vault.VaultError) as exc:
        vault.retrieve(ACCOUNT, call_tool=weird)
    assert "s3cret" not in str(exc.value)


def test_store_sends_the_value_once_and_categorises_it(fake_vault):
    vault.store(ACCOUNT, "s3cret", call_tool=fake_vault)
    service, tool, args = fake_vault.calls[0]
    assert (service, tool) == ("cortex", "vault_store")
    assert args["key"] == vault.vault_key(ACCOUNT)
    assert args["value"] == "s3cret"
    assert args["category"] == "maildex"


def test_store_refuses_an_empty_password(fake_vault):
    with pytest.raises(ValueError, match="empty"):
        vault.store(ACCOUNT, "", call_tool=fake_vault)
    assert fake_vault.calls == []


def test_delete_forgets_the_secret(fake_vault):
    fake_vault.secrets[vault.vault_key(ACCOUNT)] = "s3cret"
    vault.delete(ACCOUNT, call_tool=fake_vault)
    assert fake_vault.secrets == {}


def test_delete_of_an_absent_key_is_not_a_failure(fake_vault):
    vault.delete(ACCOUNT, call_tool=fake_vault)  # must not raise


def test_the_vault_module_writes_nothing_to_disk(firekeep_home, fake_vault):
    """M3 as a filesystem property, not a promise: a full store/retrieve/delete
    cycle must leave the kit home exactly as it found it."""
    before = sorted(p.name for p in firekeep_home.rglob("*"))
    vault.store(ACCOUNT, "s3cret", call_tool=fake_vault)
    vault.retrieve(ACCOUNT, call_tool=fake_vault)
    vault.delete(ACCOUNT, call_tool=fake_vault)
    assert sorted(p.name for p in firekeep_home.rglob("*")) == before


class TestStoreVerifiesInBand:
    """MCP tools fail with a 200 and an error STRING (the night-shift lesson);
    the first live e2e stored a password into a refusal message and printed
    success. store() now requires the positive confirmation."""

    def test_inband_refusal_raises_vault_refused(self):
        import pytest
        from firekeep_maildex import vault

        def refusing(_svc, _tool, _args, **_kw):
            return "Error: Insufficient scope: requires 'admin'. Suggestion: ..."
        with pytest.raises(vault.VaultRefused):
            vault.store("acct1", "pw", call_tool=refusing)

    def test_inband_unconfirmed_raises_vault_error(self):
        import pytest
        from firekeep_maildex import vault

        def weird(_svc, _tool, _args, **_kw):
            return "OK maybe?"
        with pytest.raises(vault.VaultError):
            vault.store("acct1", "pw", call_tool=weird)

    def test_positive_confirmation_is_success(self):
        from firekeep_maildex import vault

        def confirming(_svc, _tool, _args, **_kw):
            return "Secret 'maildex.acct1' stored securely in the vault."
        vault.store("acct1", "pw", call_tool=confirming)  # no raise
