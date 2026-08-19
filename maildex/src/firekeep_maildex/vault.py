"""The app password's only home — the Keep's vault (M3).

Nothing in this module writes to disk, and nothing outside it holds a password
for longer than one connection. `retrieve` returns a string into a local
variable in `sync`; `sync` drops it in a `finally`. That is the whole custody
model, and `test_sync.py::test_the_password_never_touches_disk` is what keeps
it true.

Vault access goes through the client kit's MCP helper, the same seam
`nightshift` uses for every tool call — so the endpoint, the auth headers and
the TLS decision are made in exactly one place, and `call_tool` is injectable
for tests that must never reach a network.

**Two deviations from the spec's letter, both forced by the server that
already exists:**

* The key is `maildex.<account_id>`, not `maildex/<account_id>`. The vault
  validates key names against `^[a-zA-Z0-9\\-_.]{1,200}$` and the REST route is
  `/vault/secrets/{key}` — a slash would be rejected by the first and would
  reshape the second. A dot namespaces it identically.
* `vault_store` and `vault_delete` are **admin-scoped** server-side, while an
  enrolled member key carries `vault:read` and not `admin`. `retrieve` (the
  sync path) works for any enrolled member; `store` and `delete` may be
  refused, so both surface the refusal in words a person can act on instead of
  a bare 403.
"""
from __future__ import annotations

import re

KEY_PREFIX = "maildex."

# What `vault_retrieve` renders around the secret. The MCP tool returns
# markdown for a human reader, not JSON, so the value has to be lifted back
# out of it — anchored to the line start so a password that itself contains
# "- **Value:** " cannot shift the match.
_VALUE_LINE = re.compile(r"^- \*\*Value:\*\* (.*)$", re.MULTILINE)

_NOT_FOUND = re.compile(r"not found in the vault", re.IGNORECASE)
_REFUSED = re.compile(r"\b(403|forbidden|scope|admin)\b", re.IGNORECASE)


class VaultError(RuntimeError):
    """The vault could not be reached, or refused."""


class VaultMissing(VaultError):
    """No secret is stored under this account's key.

    Distinct from VaultError on purpose: "the Keep is down" and "you never
    stored a password for this mailbox" need different words and different
    actions from the human reading them.
    """


class VaultRefused(VaultError):
    """The Keep answered, and said this key may not do that.

    Storing and deleting a secret are admin operations; an ordinary enrolled
    member key can read the vault and not write it. Naming that explicitly is
    the difference between "ask your Keep admin" and "your network is broken".
    """


def vault_key(account_id: str) -> str:
    """`maildex.<account_id>` — see the module docstring for why not a slash."""
    return f"{KEY_PREFIX}{account_id}"


def _call(tool: str, arguments: dict, *, call_tool=None, timeout: float = 20.0):
    if call_tool is None:
        from firekeep_client.hooks import _mcp

        call_tool = _mcp.call_tool
    try:
        return call_tool("cortex", tool, arguments, timeout=timeout)
    except Exception as e:  # noqa: BLE001 - transport, MCP and JSON errors alike
        if _REFUSED.search(str(e)):
            raise VaultRefused(str(e)) from e
        raise VaultError(str(e)) from e


def retrieve(account_id: str, *, call_tool=None) -> str:
    """The app password for one account, straight into the caller's variable.

    Never cached, never written, never logged — and never returned empty: an
    empty string would be handed to `IMAP4.login` and read as a failed
    authentication rather than as the missing secret it is.
    """
    key = vault_key(account_id)
    result = _call("vault_retrieve", {"key": key}, call_tool=call_tool)
    text = result if isinstance(result, str) else str(result)
    if _NOT_FOUND.search(text):
        raise VaultMissing(
            f"no app password is stored for this mailbox (vault key {key}) — "
            f"re-add the account, or store it with `vault_store`"
        )
    match = _VALUE_LINE.search(text)
    # The value is deliberately NOT included in any exception below: an error
    # string travels into hooklog, into stderr, and into whatever collects
    # them. A secret that reaches a log is a secret on disk (M3).
    if match is None:
        raise VaultError(f"the vault answered with no value for {key}")
    password = match.group(1)
    if not password:
        raise VaultError(f"the vault holds an empty password for {key}")
    return password


def store(account_id: str, password: str, *, call_tool=None) -> None:
    """Put the app password in the vault. The ONE place it is ever sent."""
    if not password:
        raise ValueError("refusing to store an empty password")
    _call("vault_store", {
        "key": vault_key(account_id),
        "value": password,
        "description": "maildex IMAP app password",
        "category": "maildex",
    }, call_tool=call_tool)


def delete(account_id: str, *, call_tool=None) -> None:
    """Forget the app password. Idempotent: a key that is already gone is the
    outcome this wanted, not a failure to report."""
    try:
        _call("vault_delete", {"key": vault_key(account_id)}, call_tool=call_tool)
    except VaultMissing:  # pragma: no cover - _call does not raise this today
        return
