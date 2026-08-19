"""The wire contract (spec §3). Thin on purpose, so it can be asserted exactly.

Maildex builds no URL and no auth header of its own: `resolver.resolve("cortex")`
hands over `rest_base`, `headers` and `verify`, and `transport` makes the call —
the same seam docdex and `nightshift._evidence` use. That is why the TLS guard,
the `ca_path` handling and the attribution headers are correct here without this
module knowing they exist.

The source name is the load-bearing shape:

    maildex:<128-bit account id>:<sha256 of "folder|uidvalidity|uid|message_id">

Opaque by construction. No `/`, so it is one clean DELETE path parameter; ~104
characters, under the server's 500 ceiling; no subject, no address and no
folder name leaking through an identifier; and stable, so re-running a sync
over the same message overwrites its replica instead of adding a second one.

**UIDVALIDITY is inside the hash (M7).** After a provider-side rebuild the same
UID names a different message, and a name that ignored the generation would
overwrite real mail with unrelated mail. The cost is that a rebuild produces a
second replica of each message under a new name — disclosed, and the smaller of
the two errors by a wide margin.

**M1 is a constant here, not a parameter.** `VISIBILITY = "member"` is read
directly by `ingest_payload`; there is no argument, no default and no keyword
that can produce anything else. Sharing a mailbox is a different dex.
"""
from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import quote

DEX_ID = "firekeep.maildex"
SOURCE_PREFIX = "maildex"
SOURCE_TYPE = "email"

# M1 — member-private, structurally. Deliberately not a tuple of choices like
# docdex's VISIBILITIES: there is nothing to choose from.
VISIBILITY = "member"


def source_name(account_id: str, folder: str, uidvalidity: int, uid: int,
                message_id: str) -> str:
    identity = f"{folder}|{uidvalidity}|{uid}|{message_id}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{SOURCE_PREFIX}:{account_id}:{digest}"


def ingest_payload(account_id: str, folder: str, uidvalidity: int, uid: int, *,
                   text: str, subject: str = "", sender: str = "", date: str = "",
                   message_id: str = "", attachments=()) -> dict[str, Any]:
    """The exact `POST /corpus/ingest` body.

    Every metadata value is a STRING: the server declares `dict[str, str]` and
    rejects anything else, so `untrusted_content` ships as `"true"` and the
    attachment list as a comma-joined line rather than a JSON array. Tenancy is
    absent by design — `workspace_id`, the owning `member_id` and the writing
    dex identity are stamped from the verified principal, never client-asserted.
    """
    return {
        "content": text,
        "source_name": source_name(account_id, folder, uidvalidity, uid, message_id),
        "source_type": SOURCE_TYPE,
        "visibility": VISIBILITY,
        "metadata": {
            "folder": folder,
            "subject": subject,
            "from": sender,
            "date": date,
            "message_id": message_id,
            # Names only. Attachment CONTENT is not ingested in round 1 (M6),
            # and this line is the disclosure a person sees in recall output.
            "attachments": ", ".join(str(a) for a in attachments),
            "dex": DEX_ID,
            # M4 — email is the archetype of untrusted input. A prompt-injection
            # payload in a message is evidence of what someone sent, never an
            # instruction. There is no code path that omits this flag.
            "untrusted_content": "true",
        },
    }


class Client:
    """A cortex endpoint plus the two transport calls, both injectable.

    Injectable because the wire shapes are worth asserting exactly, and a test
    that has to stand up a server to do it will be written once and then
    weakened.
    """

    def __init__(self, endpoint=None, *, post=None, delete=None, timeout: float | None = None):
        if endpoint is None:
            from firekeep_client import resolver

            endpoint = resolver.resolve("cortex")
        self.endpoint = endpoint
        self._post = post or _default_post
        self._delete = delete or _default_delete
        self._timeout = timeout

    def _kwargs(self) -> dict:
        kw = {"headers": self.endpoint.headers, "verify": self.endpoint.verify}
        if self._timeout is not None:
            kw["timeout"] = self._timeout
        return kw

    def ingest(self, account_id: str, folder: str, uidvalidity: int, uid: int,
               **fields) -> Any:
        payload = ingest_payload(account_id, folder, uidvalidity, uid, **fields)
        return self._post(f"{self.endpoint.rest_base}/corpus/ingest", payload, **self._kwargs())

    def delete_account(self, account_id: str) -> Any:
        """One bounded bulk delete for a whole mailbox — not thousands of
        sequential per-message requests. This is the M5 removal path, and the
        only delete maildex makes: round 1 does not mirror provider-side
        deletions, which is disclosed rather than quietly compensated for."""
        return self._delete(
            f"{self.endpoint.rest_base}/corpus/dex-sources/{quote(account_id, safe='')}",
            **self._kwargs(),
        )


def _default_post(url, body, *, headers, verify, timeout=None):
    from firekeep_client import transport

    kw = {"headers": headers, "verify": verify}
    if timeout is not None:
        kw["timeout"] = timeout
    return transport.post_json(url, body, **kw)


def _default_delete(url, *, headers, verify, timeout=None):
    from firekeep_client import transport

    # transport publishes get_json/post_json only. DELETE goes through the same
    # `_request` rather than a hand-rolled urllib call here, so the TLS context
    # building and the TransportError contract are identical to every other call
    # the kit makes — a second implementation would be a second place for the
    # verify semantics to drift.
    kw = {"headers": headers, "verify": verify}
    if timeout is not None:
        kw["timeout"] = timeout
    return transport._request(url, method="DELETE", **kw)
