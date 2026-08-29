"""BIND_ADDR is the service address; VPS_IP is not a substitute for it."""

from app.enroll.advertise import advertised_host, resolve_connection


def test_bind_addr_is_the_address_a_device_dials():
    # The deployment that surfaced this: ports bind to a tailnet address while
    # VPS_IP names a public one where nothing is published.
    advertised = advertised_host({"BIND_ADDR": "100.64.0.1", "VPS_IP": "203.0.113.7"})
    assert advertised.host == "100.64.0.1"
    assert advertised.source == "BIND_ADDR"
    assert advertised.reachable


def test_wildcard_bind_defers_to_vps_ip():
    advertised = advertised_host({"BIND_ADDR": "0.0.0.0", "VPS_IP": "203.0.113.9"})
    assert (advertised.host, advertised.source) == ("203.0.113.9", "VPS_IP")


def test_wildcard_bind_without_vps_ip_names_nothing():
    advertised = advertised_host({"BIND_ADDR": "0.0.0.0"})
    assert advertised.host == ""
    assert "enter the address" in advertised.detail


def test_loopback_or_unset_bind_is_not_reachable():
    for env in ({}, {"BIND_ADDR": "127.0.0.1", "VPS_IP": "203.0.113.9"}):
        advertised = advertised_host(env)
        assert advertised.host == "", env
        assert not advertised.reachable, env
        assert "localhost only" in advertised.detail, env


def test_auto_resolution_prefers_http_to_the_published_address():
    transport, host, server_chosen = resolve_connection(
        transport="", kind="ports", host="", env={"BIND_ADDR": "100.64.0.1"}
    )
    assert (transport, host, server_chosen) == ("http", "100.64.0.1", True)


def test_auto_resolution_falls_back_to_a_tunnel_when_nothing_is_reachable():
    transport, host, server_chosen = resolve_connection(
        transport="", kind="ports", host="", env={"VPS_IP": "203.0.113.9"}
    )
    assert (transport, host, server_chosen) == ("tunnel", "127.0.0.1", False)


def test_a_named_tunnel_keeps_localhost_whatever_the_caller_sent():
    # The client reaches the forwarded port, never `host` — a code claiming
    # otherwise would only mislead whoever reads it.
    transport, host, server_chosen = resolve_connection(
        transport="tunnel", kind="ports", host="10.0.0.4", env={"BIND_ADDR": "10.0.0.4"}
    )
    assert (transport, host, server_chosen) == ("tunnel", "127.0.0.1", False)


def test_a_named_host_is_never_overridden():
    transport, host, server_chosen = resolve_connection(
        transport="http", kind="ports", host="firekeep.example",
        env={"BIND_ADDR": "100.64.0.1"},
    )
    assert (transport, host, server_chosen) == ("http", "firekeep.example", False)


def test_a_named_transport_still_borrows_the_published_host():
    transport, host, server_chosen = resolve_connection(
        transport="http", kind="ports", host="", env={"BIND_ADDR": "100.64.0.1"}
    )
    # server_chosen stays False: the caller asked for http, so the insecure_http
    # confirmation it owes is still owed.
    assert (transport, host, server_chosen) == ("http", "100.64.0.1", False)
