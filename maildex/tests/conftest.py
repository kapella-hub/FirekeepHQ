"""Shared fixtures. The whole suite runs OFFLINE: there is no IMAP server, no
Keep, and no network call anywhere in it.

Three fakes carry it:

* `SpyIMAP` — an imaplib-shaped connection that RAISES on every mutating verb
  and records the arguments of every read. It is what makes M2 testable rather
  than merely asserted in a docstring.
* `FakeServer` — records the exact wire call (url, body, headers, verify) so
  the corpus shapes can be asserted byte-for-byte.
* `FakeVault` — an injectable `call_tool` standing in for the MCP helper.

`firekeep_client` is a real dependency of the wheel (resolver + transport). In
a monorepo checkout it may not be pip-installed, so fall back to the sibling
`client/` directory — it is stdlib-only at the modules maildex touches.
"""
from __future__ import annotations

import email.message
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:  # noqa: SIM105
    import firekeep_client  # noqa: F401
except ImportError:  # pragma: no cover - exercised only on a bare checkout
    sys.path.insert(0, str(_REPO / "client"))

SRC = Path(__file__).resolve().parents[1] / "src" / "firekeep_maildex"

PASSWORD = "hunter2-app-password"


@pytest.fixture(autouse=True)
def firekeep_home(tmp_path, monkeypatch):
    """Isolate ~/.firekeep for every test.

    maildex derives its home from `resolver._config_path().parent`, so pointing
    FIREKEEP_CONFIG at a tmp file relocates accounts.json, state/ and locks/
    together — the same isolation docdex's and the client's own suites use.
    """
    home = tmp_path / "fkhome"
    home.mkdir()
    monkeypatch.setenv("FIREKEEP_CONFIG", str(home / "config"))
    monkeypatch.delenv("FIREKEEP_BYPASS", raising=False)
    for cap in ("FIREKEEP_MAILDEX_BACKFILL_DAYS", "FIREKEEP_MAILDEX_MAX_PER_SYNC",
                "FIREKEEP_MAILDEX_MAX_MESSAGE_KB", "FIREKEEP_MAILDEX_SYNC_INTERVAL_HOURS",
                "FIREKEEP_MAILDEX_INGEST_TIMEOUT_SECONDS"):
        monkeypatch.delenv(cap, raising=False)
    return home


# --- message fixtures -------------------------------------------------------


def make_message(*, subject="Quarterly numbers", sender="priya@example.com",
                 to="me@example.com", date="Mon, 18 Aug 2026 09:14:00 +0000",
                 message_id="<abc123@example.com>", plain="The numbers are attached.",
                 html=None, attachments=()) -> bytes:
    """A real RFC 5322 message, built by the stdlib that will parse it back."""
    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg["Date"] = date
    msg["Message-ID"] = message_id
    if plain is not None:
        msg.set_content(plain)
    if html is not None:
        if plain is None:
            msg.set_content(html, subtype="html")
        else:
            msg.add_alternative(html, subtype="html")
    for name in attachments:
        msg.add_attachment(b"\x00binary", maintype="application",
                           subtype="octet-stream", filename=name)
    return msg.as_bytes()


# --- the spy IMAP connection ------------------------------------------------


class MutationAttempted(AssertionError):
    """A mutating IMAP verb was called. M2 says this can never happen."""


class SpyIMAP:
    """An imaplib-shaped connection that cannot be used to change a mailbox.

    Every state-changing verb raises. If maildex ever grows a code path that
    flags, moves, copies, deletes or appends a message, the test that drives it
    fails with a MutationAttempted naming the verb — no reviewer vigilance
    required.
    """

    def __init__(self, folders: dict, *, uidvalidity: int = 900):
        # folder -> {uid: raw bytes}, or a dict with explicit "uidvalidity"
        self.folders = {}
        self.uidvalidity = {}
        for name, spec in folders.items():
            if isinstance(spec, dict) and "messages" in spec:
                self.folders[name] = dict(spec["messages"])
                self.uidvalidity[name] = spec.get("uidvalidity", uidvalidity)
            else:
                self.folders[name] = dict(spec)
                self.uidvalidity[name] = uidvalidity
        self.selects: list[tuple] = []
        self.fetches: list[tuple] = []
        self.searches: list[tuple] = []
        self.logins: list[tuple] = []
        self.logged_out = False
        self.current: str | None = None
        self.fetch_hook = None   # (uid) -> ("OK", data) | None
        self.search_hook = None  # (args) -> ("OK", data) | None
        self.select_hook = None  # (folder) -> ("NO", [b"..."]) | None

    # --- reads ---

    def login(self, username, password):
        self.logins.append((username, password))
        return "OK", [b"LOGIN completed"]

    def select(self, mailbox, readonly=False):
        folder = _unquote(mailbox)
        self.selects.append((folder, readonly))
        if self.select_hook is not None:
            forced = self.select_hook(folder)
            if forced is not None:
                return forced
        if folder not in self.folders:
            return "NO", [b"Mailbox does not exist"]
        self.current = folder
        return "OK", [str(len(self.folders[folder])).encode()]

    def response(self, name):
        if name != "UIDVALIDITY" or self.current is None:
            return name, [None]
        return name, [str(self.uidvalidity[self.current]).encode()]

    def uid(self, command, *args):
        command = command.upper()
        if command == "SEARCH":
            return self._search(args)
        if command == "FETCH":
            return self._fetch(args)
        raise MutationAttempted(f"UID {command} is not a read maildex may make")

    def _search(self, args):
        self.searches.append(tuple(a for a in args if a is not None))
        if self.search_hook is not None:
            forced = self.search_hook(args)
            if forced is not None:
                return forced
        uids = sorted(self.folders.get(self.current or "", {}))
        criteria = [a for a in args if a is not None]
        if criteria and criteria[0].startswith("UID "):
            low = int(criteria[0].split()[1].split(":")[0])
            matched = [u for u in uids if u >= low]
            # IMAP `n:*` always answers with the highest UID even when it is
            # below n. Reproduced deliberately — it is the exact behaviour
            # `search_after` filters client-side.
            if not matched and uids:
                matched = [uids[-1]]
            uids = matched
        return "OK", [" ".join(str(u) for u in uids).encode()]

    def _fetch(self, args):
        uid = int(args[0])
        spec = args[1] if len(args) > 1 else ""
        self.fetches.append((uid, spec))
        if self.fetch_hook is not None:
            forced = self.fetch_hook(uid)
            if forced is not None:
                return forced
        raw = self.folders.get(self.current or "", {}).get(uid)
        if raw is None:
            return "OK", [None]
        return "OK", [(f"{uid} (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]

    def logout(self):
        self.logged_out = True
        return "BYE", [b"Logging out"]

    # --- everything below is a mutation, and every one of them raises ---

    def _refuse(self, verb):
        raise MutationAttempted(f"maildex called the mutating IMAP verb {verb}")

    def append(self, *a, **k):
        self._refuse("APPEND")

    def store(self, *a, **k):
        self._refuse("STORE")

    def expunge(self, *a, **k):
        self._refuse("EXPUNGE")

    def setacl(self, *a, **k):
        self._refuse("SETACL")

    def deleteacl(self, *a, **k):
        self._refuse("DELETEACL")

    def copy(self, *a, **k):
        self._refuse("COPY")

    def create(self, *a, **k):
        self._refuse("CREATE")

    def rename(self, *a, **k):
        self._refuse("RENAME")

    def delete(self, *a, **k):
        self._refuse("DELETE")

    def subscribe(self, *a, **k):
        self._refuse("SUBSCRIBE")


def _unquote(mailbox: str) -> str:
    if len(mailbox) >= 2 and mailbox[0] == '"' and mailbox[-1] == '"':
        return mailbox[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return mailbox


def connector_for(spy, *, fail=None):
    """An `imapio.session(connector=...)` that hands back `spy`."""
    def connect(host, port, timeout):
        if fail is not None:
            raise fail
        spy.connected_to = (host, port, timeout)
        return spy
    return connect


@pytest.fixture
def spy():
    return SpyIMAP({"INBOX": {1: make_message(subject="One", message_id="<1@x>"),
                              2: make_message(subject="Two", message_id="<2@x>")},
                    "Sent": {5: make_message(subject="Sent one", message_id="<5@x>")}})


@pytest.fixture
def connector(spy):
    return connector_for(spy)


# --- the fake vault ---------------------------------------------------------


class FakeVault:
    """An injectable `call_tool`. Renders `vault_retrieve` exactly as the MCP
    tool does — markdown around the value — because that rendering is what
    `vault.retrieve` has to parse."""

    def __init__(self, secrets=None):
        self.secrets = dict(secrets or {})
        self.calls: list[tuple] = []
        self.refuse_store = False
        self.refuse_delete = False

    def __call__(self, service, tool, arguments, **kwargs):
        self.calls.append((service, tool, dict(arguments)))
        key = arguments.get("key", "")
        if tool == "vault_retrieve":
            if key not in self.secrets:
                return f"Secret '{key}' not found in the vault."
            return (f"## Secret: {key}\n- **Value:** {self.secrets[key]}\n"
                    f"- **Category:** maildex\n- **Updated:** 2026-08-19T00:00:00Z")
        if tool == "vault_store":
            if self.refuse_store:
                raise RuntimeError("cortex.vault_store: MCP error 403 Forbidden: requires scope 'admin'")
            self.secrets[key] = arguments["value"]
            return f"Secret '{key}' stored securely in the vault."
        if tool == "vault_delete":
            if self.refuse_delete:
                raise RuntimeError("cortex.vault_delete: MCP error 403 Forbidden: requires scope 'admin'")
            self.secrets.pop(key, None)
            return f"Secret '{key}' deleted."
        raise AssertionError(f"maildex called an unexpected MCP tool: {tool}")


@pytest.fixture
def fake_vault():
    return FakeVault()


# --- the fake server --------------------------------------------------------


class TransportFailure(Exception):
    """Shaped like `transport.TransportError`: `status` is None for a network
    failure and an int for a response that reached us."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class FakeServer:
    def __init__(self):
        self.posts: list[dict] = []
        self.deletes: list[dict] = []
        self.post_hook = None    # (index, url, body) -> response | None
        self.delete_hook = None  # (index, url) -> response | None

    def post(self, url, body, *, headers, verify, timeout=None):
        self.posts.append({"url": url, "body": body, "headers": headers,
                           "verify": verify, "timeout": timeout})
        if self.post_hook is not None:
            result = self.post_hook(len(self.posts) - 1, url, body)
            if result is not None:
                return result
        return {"source_name": body.get("source_name"), "chunks_stored": 1}

    def delete(self, url, *, headers, verify, timeout=None):
        self.deletes.append({"url": url, "headers": headers, "verify": verify})
        if self.delete_hook is not None:
            result = self.delete_hook(len(self.deletes) - 1, url)
            if result is not None:
                return result
        return {"deleted_sources": 3, "deleted_chunks": "all"}

    @property
    def ingested_names(self) -> list[str]:
        return [p["body"]["source_name"] for p in self.posts]


@pytest.fixture
def server():
    return FakeServer()


@pytest.fixture
def endpoint():
    from firekeep_client import resolver

    # verify=False here is not a maildex choice: it is what the resolver itself
    # produces for `scheme = http`, and maildex only ever passes `ep.verify`
    # through. The TLS decision has exactly one home, and it is not this wheel.
    return resolver.Endpoint(
        mcp_url="http://keep.test:8080/mcp",
        rest_base="http://keep.test:8100",
        headers={"X-Agent-Id": "tester", "X-API-Key": "k3y"},
        verify=False,
    )


@pytest.fixture
def client(endpoint, server):
    from firekeep_maildex import wire

    return wire.Client(endpoint, post=server.post, delete=server.delete)


@pytest.fixture
def configured(firekeep_home):
    """A real kit config, so the resolver seam is exercised for real rather
    than only through a hand-built Endpoint."""
    (firekeep_home / "config").write_text(
        "[server]\nkind = ports\nscheme = http\nhost = keep.test\napi_key = k3y\n"
        "\n[identity]\nagent_id = tester\n",
        encoding="utf-8",
    )
    return firekeep_home


@pytest.fixture
def account(fake_vault):
    """A registered mailbox whose app password is already in the fake vault."""
    from firekeep_maildex import accounts, vault

    acct = accounts.add("imap.example.com", "me@example.com")
    fake_vault.secrets[vault.vault_key(acct.id)] = PASSWORD
    return acct
