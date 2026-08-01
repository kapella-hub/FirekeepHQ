from __future__ import annotations

import base64
import hashlib
import json

import pytest

from firekeep_client.joincode import JoinCodeError, decode_join_code


def encode(payload, prefix="fk_join_"):
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    check = base64.urlsafe_b64encode(hashlib.sha256(body.encode()).digest()[:3]).decode().rstrip("=")
    return f"{prefix}{body}.{check}"


def payload(**changes):
    value = {
        "v": 1,
        "t": "http",
        "k": "ports",
        "h": "firekeep.example",
        "x": "20260731T120000Z",
        "q": base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("="),
    }
    value.update(changes)
    return value


def test_round_trip_and_tid():
    code = decode_join_code(encode(payload()))
    assert code.host == "firekeep.example"
    assert code.tid == hashlib.sha256(bytes(range(32))).hexdigest()[:16]
    assert code.purpose == "device"


def test_member_invite_uses_same_validated_transport_envelope():
    value = payload()
    value["m"] = value.pop("q")
    code = decode_join_code(encode(value, prefix="fk_member_"))
    assert code.purpose == "member"
    assert code.host == "firekeep.example"


def test_whitespace_and_whole_command_are_tolerated():
    raw = encode(payload())
    wrapped = "\n".join(raw[i:i + 30] for i in range(0, len(raw), 30))
    assert decode_join_code("firekeep join " + wrapped).tid == decode_join_code(raw).tid


def test_single_character_damage_is_named_without_ticket():
    raw = encode(payload())
    index = len("fk_join_") + 10
    damaged = raw[:index] + ("A" if raw[index] != "A" else "B") + raw[index + 1:]
    with pytest.raises(JoinCodeError) as exc:
        decode_join_code(damaged)
    assert exc.value.code == "E_DAMAGED"
    assert payload()["q"] not in str(exc.value)
    assert payload()["q"] not in repr(exc.value)


@pytest.mark.parametrize("raw", ["hello", "fk_join_no-dot", "fk_join_a.b.c"])
def test_not_code_or_malformed(raw):
    with pytest.raises(JoinCodeError):
        decode_join_code(raw)


def test_future_version_is_actionable():
    with pytest.raises(JoinCodeError) as exc:
        decode_join_code(encode(payload(v=2)))
    assert exc.value.code == "E_VERSION"
    assert "firekeep update" in str(exc.value)


@pytest.mark.parametrize(
    "changes,field",
    [
        ({"t": "wat"}, "t"),
        ({"k": "wat"}, "k"),
        ({"u": "https://x"}, "h/u"),
        ({"t": "tls"}, "f"),
        ({"t": "tunnel"}, "s"),
        ({"q": "YQ"}, "q"),
    ],
)
def test_shape_errors_name_the_field_without_secret(changes, field):
    with pytest.raises(JoinCodeError) as exc:
        decode_join_code(encode(payload(**changes)))
    assert field in str(exc.value)
    assert payload()["q"] not in str(exc.value)


def test_tls_os_and_paths_are_valid():
    value = payload(t="tls", k="paths", u="https://firekeep.example", f="os")
    value.pop("h")
    code = decode_join_code(encode(value))
    assert code.base_url == "https://firekeep.example"
    assert code.fingerprint == "os"
