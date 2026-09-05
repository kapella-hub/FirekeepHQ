"""The other side of the loopback API, used by the MCP server (and by
`firekeep-hands-broker status`).

Stdlib urllib only, and no method raises on a transport failure. That is not
politeness, it is the fail-closed rule: a broker that has died mid-task must
make `consume` return False and every protected step refuse, not throw an
exception into the middle of `HandsSession.act` where the shape of the
failure decides what happens next.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from .. import paths

_POLL_S = 0.25


class BrokerClient:
    def __init__(self, port: int, token: str, timeout: float = 2.0):
        self.port = int(port)
        self.token = str(token)
        self.timeout = float(timeout)

    # -- construction -----------------------------------------------------

    @classmethod
    def from_disk(cls, timeout: float = 2.0) -> "BrokerClient | None":
        """The running broker, or None. `broker.json` alone is not proof —
        it outlives a killed process — so this also calls `/health` and only
        hands back a client that something actually answered."""
        path: Path = paths.broker_info_path()
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            port, token = int(info["port"]), str(info["token"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        client = cls(port, token, timeout=timeout)
        return client if client.health() else None

    # -- transport --------------------------------------------------------

    def _call(self, method: str, path: str, body=None) -> tuple[int | None, object]:
        """`(status, payload)`, or `(None, None)` when the broker could not
        be reached at all. Callers distinguish "the broker said no" from
        "there is no broker" on that None."""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - loopback
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"null")
            except (ValueError, OSError):
                return exc.code, None
        except (urllib.error.URLError, OSError, ValueError):
            return None, None

    # -- API --------------------------------------------------------------

    def health(self) -> dict | None:
        status, payload = self._call("GET", "/health")
        if status == 200 and isinstance(payload, dict) and payload.get("ok"):
            return payload
        return None

    def request(self, **fields) -> dict:
        """Ask for a permit. The reply is the permit as it stands — pending,
        or already approved if this is a retry of a request a human has
        answered in the meantime."""
        status, payload = self._call("POST", "/permits", fields)
        if status == 201 and isinstance(payload, dict):
            return payload
        return {
            "challenge": fields.get("challenge"),
            "state": "unreachable" if status is None else "error",
            "via": None,
        }

    def get(self, challenge) -> dict | None:
        status, payload = self._call("GET", f"/permits/{quote(str(challenge), safe='')}")
        if status == 200 and isinstance(payload, dict):
            return payload
        return None

    def wait(self, challenge, timeout_s: float) -> dict:
        """Block until a human answers, the permit expires, or `timeout_s`.

        Two shapes end the wait immediately rather than burning the whole
        timeout on something that will never change: `unreachable` (no
        broker) and `unknown` (nothing ever requested this challenge)."""
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        path = f"/permits/{quote(str(challenge), safe='')}"
        last: dict | None = None
        while True:
            status, payload = self._call("GET", path)
            if status is None:
                return {"challenge": challenge, "state": "unreachable", "via": None}
            if status == 404:
                return {"challenge": challenge, "state": "unknown", "via": None}
            if status == 200 and isinstance(payload, dict):
                last = payload
                if payload.get("state") != "pending":
                    return payload
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(_POLL_S, remaining))
        return last or {"challenge": challenge, "state": "unknown", "via": None}

    def consume(self, challenge) -> bool:
        """True only when the broker moved this permit from approved to
        consumed for us. Every other answer — 409, 404, no broker — is False,
        which is what makes an unreachable broker refuse the step."""
        status, payload = self._call(
            "POST", f"/permits/{quote(str(challenge), safe='')}/consume"
        )
        return status == 200 and isinstance(payload, dict) and payload.get("state") == "consumed"
