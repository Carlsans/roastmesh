"""Offline IP->country lookup and ticket IP extraction for the peers list."""
import iroh
import pytest

from roastmesh.gui import geoip
from roastmesh.identity import generate_identity
from roastmesh.peers import public_ip_from_ticket


@pytest.mark.parametrize("ip,expected", [
    ("8.8.8.8", "US"),
    ("1.0.0.1", "AU"),
    ("212.27.48.10", "FR"),
])
def test_public_ipv4_maps_to_a_country(ip, expected):
    assert geoip.country_code(ip) == expected


@pytest.mark.parametrize("ip", [
    "192.168.1.5", "10.0.0.1", "172.16.0.1",   # RFC1918
    "127.0.0.1",                                 # loopback
    "169.254.1.1",                               # link-local
    "::1", "2001:db8::1",                        # IPv6 (roastmesh binds v4)
    "not-an-ip", "", None,                       # junk
])
def test_non_public_ipv4_has_no_country(ip):
    assert geoip.country_code(ip) is None


def _ticket_with(addrs):
    ident = generate_identity()
    addr = iroh.EndpointAddr(iroh.EndpointId.from_string(ident.public_key_hex), None, addrs)
    return str(iroh.EndpointTicket.from_addr(addr))


def test_public_ip_from_ticket_returns_the_public_v4():
    t = _ticket_with(["192.168.1.5:41890", "8.8.8.8:41890"])
    assert public_ip_from_ticket(t) == "8.8.8.8"


def test_public_ip_from_ticket_none_when_only_private():
    t = _ticket_with(["192.168.1.5:41890", "10.0.0.9:41890"])
    assert public_ip_from_ticket(t) is None


def test_public_ip_from_ticket_tolerates_garbage():
    assert public_ip_from_ticket("not a ticket") is None
