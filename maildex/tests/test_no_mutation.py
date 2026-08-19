"""M2, asserted against the SOURCE rather than against behaviour.

The spy connection in `test_imapio.py` proves that the paths the tests drive
never mutate a mailbox. This file proves something stronger and cheaper: that
no such path can be written at all without failing the build. It reads the
shipped package as text and checks four structural properties.

Grep-level guards get a bad name because they are usually approximate. These
are not: each one names an exact mechanism, and each would have to be
deliberately worked around rather than accidentally tripped over.
"""
from __future__ import annotations

import re

from conftest import SRC

SOURCES = sorted(SRC.glob("*.py"))


def _read(path):
    return path.read_text(encoding="utf-8")


def _lines():
    """Every source line in the package. The call-site guards work per LINE
    rather than per regex-matched call: a paren-balancing regex over
    `select(_mailbox(folder), readonly=True)` stops at the inner paren and
    reports a violation that is not there."""
    for path in SOURCES:
        yield from _read(path).splitlines()


def test_the_package_has_the_modules_the_spec_names():
    assert {p.name for p in SOURCES} == {
        "__init__.py", "accounts.py", "cli.py", "imapio.py", "parse.py",
        "state.py", "sync.py", "vault.py", "wire.py",
    }


def test_only_imapio_speaks_imap():
    """One module imports `imaplib`, so there is exactly one place a mutating
    command could be issued from — and the next three tests are about that
    one place."""
    importers = [p.name for p in SOURCES if re.search(r"\bimport imaplib\b", _read(p))]
    assert importers == ["imapio.py"]


def test_imapio_touches_only_allowlisted_connection_methods():
    """Every attribute reached on the raw connection, enumerated.

    `Session` holds the connection privately; this is what stops a future
    method from quietly reaching past the four reads into `store` or `copy`.
    """
    allowed = {"select", "response", "uid", "login", "logout"}
    used = set(re.findall(r"_conn\.(\w+)", _read(SRC / "imapio.py")))
    assert used <= allowed, f"imapio reaches unallowlisted connection methods: {used - allowed}"


def test_every_mailbox_open_in_the_package_is_readonly():
    """M2's mechanism. `select(readonly=True)` is IMAP EXAMINE: the SERVER then
    refuses state-changing commands for the life of the connection, so this one
    keyword is what makes "maildex cannot delete your mail" a fact about the
    protocol rather than a claim about our discipline."""
    calls = [line.strip() for line in _lines() if ".select(" in line]
    assert calls, "no select() call found — this guard would pass vacuously"
    for call in calls:
        assert "readonly=True" in call, f"a mailbox is opened without readonly=True: {call}"


def test_every_fetch_peeks():
    """A plain `BODY[]` fetch sets \\Seen. Reading the Keep's copy of a mailbox
    must never mark a person's mail as read."""
    fetches = [line.strip() for line in _lines() if '"FETCH"' in line]
    assert fetches, "no FETCH found — this guard would pass vacuously"
    for fetch in fetches:
        assert "BODY.PEEK" in fetch, f"a fetch without PEEK: {fetch}"


def test_no_mutating_imap_verb_appears_anywhere_in_the_package():
    """The verbs travel to the server as string literals, so their absence as
    string literals is the property worth checking.

    `DELETE` is deliberately excluded: it is also the HTTP method `wire.py`
    passes to the transport for the M5 bulk removal, and a guard that flagged
    it would be turned off rather than obeyed. The IMAP DELETE path is closed
    by the allowlist test above, which is the stronger check anyway.
    """
    forbidden = re.compile(
        r"""["'](APPEND|STORE|EXPUNGE|SETACL|DELETEACL|COPY|CREATE|RENAME|SUBSCRIBE)\b""",
        re.IGNORECASE,
    )
    offenders = {p.name: forbidden.findall(_read(p)) for p in SOURCES}
    assert not any(offenders.values()), f"mutating IMAP verbs in the source: {offenders}"


def test_nothing_in_the_package_can_send_mail():
    """"No send capability" is not a policy maildex enforces — it is a library
    it does not import (M2). Checked as code, not as prose: the module
    docstrings are free to SAY there is no SMTP, and this is what makes the
    saying true."""
    for path in SOURCES:
        text = _read(path)
        assert not re.search(r"\bimport smtplib\b", text), f"{path.name} imports smtplib"
        assert "smtplib." not in text, f"{path.name} uses smtplib"
        assert "sendmail" not in text, f"{path.name} sends mail"


def test_tls_verification_is_never_turned_off():
    """Spec §5 rules out self-signed IMAP endpoints precisely because
    verification is on. imaplib's own default context does NOT verify, so the
    package passes `ssl.create_default_context()` explicitly — and must never
    reach for the unverified escape hatches."""
    text = _read(SRC / "imapio.py")
    assert "ssl_context=ssl.create_default_context()" in text
    assert "_create_unverified_context" not in text
    assert "CERT_NONE" not in text
    assert "check_hostname = False" not in text and "check_hostname=False" not in text
