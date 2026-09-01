"""upnp.py against a router we write ourselves.

The live coverage available for this is one router, which may not even speak
UPnP -- so the fake IGD here carries the correctness burden, and it has to
model the ways routers say *no*, not just the way they say yes. Every quirk
below is one libtorrent had to work around in `src/upnp.cpp`; a suite that
only exercised the happy path would be testing the least interesting third of
the module.
"""
from __future__ import annotations

import http.server
import socket
import threading

import pytest

from roastmesh.upnp import (
    MAX_BODY_BYTES,
    Igd,
    UpnpError,
    _is_private,
    _location_is_plausible,
    _parse_xml,
    add_mapping,
    delete_mapping,
    describe,
    get_external_ip,
    soap,
)

DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:Layer3Forwarding:1</serviceType>
        <controlURL>/ignored</controlURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
        <controlURL>/ctl/IPConn</controlURL>
      </service>
    </serviceList>
  </device>
</root>"""


def _fault(code: int) -> bytes:
    return (f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>
<detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">
<errorCode>{code}</errorCode></UPnPError></detail>
</s:Fault></s:Body></s:Envelope>""").encode()


def _ok(action: str, extra: str = "") -> bytes:
    return (f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>
<u:{action}Response xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1">{extra}
</u:{action}Response></s:Body></s:Envelope>""").encode()


class _FakeIgd:
    """An HTTP server that answers SOAP the way a router does, including badly.

    `faults` is a list of error codes to return from AddPortMapping before
    finally succeeding -- which is how a router that refuses timed leases, or
    already has that port mapped, actually behaves.
    """

    def __init__(self, faults: list[int] | None = None, external_ip: str = "203.0.113.9") -> None:
        self.faults = list(faults or [])
        self.external_ip = external_ip
        self.adds: list[dict[str, str]] = []
        self.deletes: list[int] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_GET(self):
                body = DESCRIPTION.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                payload = self.rfile.read(length).decode()
                action = self.headers.get("Soapaction", "").rsplit("#", 1)[-1].strip('"')
                status, body = outer._respond(action, payload)
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _respond(self, action: str, payload: str) -> tuple[int, bytes]:
        if action == "AddPortMapping":
            if self.faults:
                return 500, _fault(self.faults.pop(0))
            self.adds.append({
                "external": _field(payload, "NewExternalPort"),
                "internal": _field(payload, "NewInternalPort"),
                "lease": _field(payload, "NewLeaseDuration"),
                "client": _field(payload, "NewInternalClient"),
            })
            return 200, _ok("AddPortMapping")
        if action == "DeletePortMapping":
            self.deletes.append(int(_field(payload, "NewExternalPort")))
            return 200, _ok("DeletePortMapping")
        if action == "GetExternalIPAddress":
            return 200, _ok("GetExternalIPAddress",
                            f"<NewExternalIPAddress>{self.external_ip}</NewExternalIPAddress>")
        return 500, _fault(401)

    @property
    def igd(self) -> Igd:
        return Igd(control_url=f"http://127.0.0.1:{self.port}/ctl/IPConn",
                   service_ns="urn:schemas-upnp-org:service:WANIPConnection:1")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _field(payload: str, name: str) -> str:
    start = payload.index(f"<{name}>") + len(name) + 2
    return payload[start:payload.index(f"</{name}>")]


@pytest.fixture
def igd():
    fake = _FakeIgd()
    yield fake
    fake.close()


# --- the happy path ---------------------------------------------------------

def test_a_router_that_cooperates_gives_us_the_port_we_asked_for(igd) -> None:
    mapping = add_mapping(igd.igd, 41890, "192.168.0.222", lease_s=3600)
    assert mapping is not None
    assert mapping.external_port == 41890
    assert mapping.external_ip == "203.0.113.9"
    assert igd.adds == [{"external": "41890", "internal": "41890",
                         "lease": "3600", "client": "192.168.0.222"}]


def test_the_description_yields_the_forwarding_service_not_the_first_one(igd) -> None:
    """A device lists several services and only some can forward a port. Taking
    the first would send every later call to Layer3Forwarding, which answers
    plausibly and does nothing."""
    found = describe(f"http://127.0.0.1:{igd.port}/desc.xml")
    assert found is not None
    assert found.control_url.endswith("/ctl/IPConn")
    assert found.service_ns == "urn:schemas-upnp-org:service:WANIPConnection:1"


def test_external_ip_is_read_back(igd) -> None:
    assert get_external_ip(igd.igd) == "203.0.113.9"


def test_a_mapping_can_be_deleted(igd) -> None:
    assert delete_mapping(igd.igd, 41890) is True
    assert igd.deletes == [41890]


# --- the ways routers say no ------------------------------------------------

def test_a_router_that_only_does_permanent_leases_gets_asked_again(igd) -> None:
    """UPnP error 725. Plenty of routers support nothing else, and refusing
    them would mean UPnP does nothing at all for those users."""
    igd.faults = [725]
    mapping = add_mapping(igd.igd, 41890, "192.168.0.222", lease_s=3600)

    assert mapping is not None
    assert mapping.lifetime_s == 0, "the retry did not ask for a permanent lease"
    assert [a["lease"] for a in igd.adds] == ["0"]


def test_a_taken_external_port_is_retried_on_a_different_one(igd) -> None:
    """UPnP error 718, ConflictInMappingEntry: the port is already mapped, so
    the fix is a different port rather than a different request."""
    igd.faults = [718]
    mapping = add_mapping(igd.igd, 41890, "192.168.0.222")

    assert mapping is not None
    assert mapping.external_port != 41890
    assert 40000 <= mapping.external_port <= 50000
    assert igd.adds[0]["internal"] == "41890", "the internal port must not move"


def test_action_failed_is_treated_as_a_port_conflict_too(igd) -> None:
    """libtorrent: "some routers return 501 action failed, instead of 716".
    Taking 501 at face value would abandon a mapping that a different port
    would have got."""
    igd.faults = [501]
    mapping = add_mapping(igd.igd, 41890, "192.168.0.222")

    assert mapping is not None
    assert mapping.external_port != 41890


def test_a_router_that_only_accepts_wildcard_ports_is_not_retried(igd) -> None:
    """UPnP error 727. Nothing we can ask for differently, so retrying just
    delays the answer."""
    igd.faults = [727]
    assert add_mapping(igd.igd, 41890, "192.168.0.222") is None
    assert igd.adds == []


def test_a_router_that_always_says_501_is_given_up_on_after_one_retry(igd) -> None:
    """501 twice, on two different ports, is not a port conflict.

    Measured against a real router that refuses to create mappings for us at
    all -- while answering every query and processing deletes correctly -- and
    returns 501 for every port. libtorrent spends its whole conflict budget
    there; the second refusal already carried the answer.
    """
    igd.faults = [501] * 10
    assert add_mapping(igd.igd, 41890, "192.168.0.222") is None
    assert len(igd.faults) == 8, "expected exactly two attempts before giving up"


def test_conflict_retries_are_bounded(igd) -> None:
    """A router that rejects everything must not keep us here forever."""
    igd.faults = [718] * 20
    assert add_mapping(igd.igd, 41890, "192.168.0.222") is None
    assert len(igd.faults) > 0, "it consumed every fault instead of giving up"


def test_an_unknown_error_stops_rather_than_guessing(igd) -> None:
    igd.faults = [402]
    assert add_mapping(igd.igd, 41890, "192.168.0.222") is None


def test_a_soap_fault_carries_the_routers_own_code(igd) -> None:
    igd.faults = [718]
    with pytest.raises(UpnpError) as caught:
        soap(igd.igd, "AddPortMapping", "<NewExternalPort>1</NewExternalPort>")
    assert caught.value.code == 718


# --- hostile input ----------------------------------------------------------

def test_a_location_pointing_off_the_lan_is_refused() -> None:
    """The whole of SSDP is unauthenticated. Without this check, anything on
    the network can answer a probe with a LOCATION of its choosing and have us
    fetch it."""
    assert _location_is_plausible("http://192.168.0.1:80/desc.xml", "192.168.0.1") is True
    assert _location_is_plausible("http://203.0.113.9/desc.xml", "203.0.113.9") is False
    # Right shape, wrong device: private, but not the host that answered.
    assert _location_is_plausible("http://192.168.0.5/desc.xml", "192.168.0.1") is False
    assert _location_is_plausible("https://192.168.0.1/desc.xml", "192.168.0.1") is False
    assert _location_is_plausible("file:///etc/passwd", "192.168.0.1") is False


def test_private_address_classification() -> None:
    for host in ("10.0.0.1", "192.168.1.1", "172.16.0.1", "172.31.255.1", "169.254.1.1"):
        assert _is_private(host), host
    for host in ("203.0.113.9", "8.8.8.8", "172.32.0.1", "not-an-ip"):
        assert not _is_private(host), host


def test_xml_with_an_entity_declaration_is_refused_before_parsing() -> None:
    """xml.etree does not fetch external entities but does expand internal
    ones, and a few hundred bytes of nested declarations is enough to exhaust
    memory. A size cap does not bound that; refusing the declaration does."""
    bomb = b'<?xml version="1.0"?><!DOCTYPE t [<!ENTITY a "aaaa">]><root>&a;</root>'
    with pytest.raises(ValueError, match="entity declaration"):
        _parse_xml(bomb)


def test_an_oversized_body_is_refused() -> None:
    with pytest.raises(ValueError, match="implausibly large"):
        _parse_xml(b"<root>" + b"x" * (MAX_BODY_BYTES + 1) + b"</root>")


def test_a_control_url_off_the_lan_is_refused() -> None:
    """A device describes itself, including where to send commands. It must
    still point at itself."""
    hostile = DESCRIPTION.replace("<controlURL>/ctl/IPConn</controlURL>",
                                  "<controlURL>http://203.0.113.9/ctl</controlURL>")
    server = _ServeBody(hostile.encode())
    try:
        assert describe(f"http://127.0.0.1:{server.port}/desc.xml") is None
    finally:
        server.close()


class _ServeBody:
    def __init__(self, body: bytes) -> None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# --- discovery goes out every interface -------------------------------------

def test_the_ssdp_probe_is_sent_on_every_interface(monkeypatch) -> None:
    """Same lesson as the LAN beacon: one send to the multicast address
    follows the default route, which on a machine with a VPN up is the tunnel
    -- where there is no router to find."""
    from roastmesh import upnp
    from roastmesh.interfaces import Interface

    interfaces = [
        Interface("wlan0", 3, "192.168.2.19", "192.168.2.255", "255.255.255.0"),
        Interface("tun0", 9, "10.137.8.74", None, "255.255.0.0"),
    ]
    monkeypatch.setattr(upnp, "local_interfaces", lambda: interfaces)
    chosen: list[str] = []
    real_socket = socket.socket

    class _Sock:
        """A real socket for its file descriptor -- the collector selects on
        it -- with the multicast interface recorded and sends discarded."""

        def __init__(self) -> None:
            self._real = real_socket(socket.AF_INET, socket.SOCK_DGRAM)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def setsockopt(self, _level, opt, value):
            if opt == socket.IP_MULTICAST_IF:
                chosen.append(socket.inet_ntoa(value))

        def sendto(self, payload, _addr):
            return len(payload)

    monkeypatch.setattr(upnp.socket, "socket", lambda *_a, **_kw: _Sock())
    upnp._ssdp_probe_once(0.05)

    assert chosen == ["192.168.2.19", "10.137.8.74"]


def test_a_probe_does_not_take_longer_the_more_interfaces_there_are(monkeypatch) -> None:
    """Reading each socket in turn under its own timeout multiplied the search
    budget by the interface count -- measured at 16 seconds on a machine with
    seven, for a search meant to take two. All the probes go out first now and
    every socket is read under one shared deadline."""
    import time as _time

    from roastmesh import upnp
    from roastmesh.interfaces import Interface

    many = [Interface(f"if{i}", i, f"10.0.{i}.2", f"10.0.{i}.255", "255.255.255.0")
            for i in range(8)]
    monkeypatch.setattr(upnp, "local_interfaces", lambda: many)
    real_socket = socket.socket

    class _Silent:
        def __init__(self) -> None:
            self._real = real_socket(socket.AF_INET, socket.SOCK_DGRAM)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def setsockopt(self, *_a):
            pass

        def sendto(self, payload, _addr):
            return len(payload)

    monkeypatch.setattr(upnp.socket, "socket", lambda *_a, **_kw: _Silent())

    started = _time.monotonic()
    upnp._ssdp_probe_once(0.5)
    elapsed = _time.monotonic() - started

    assert elapsed < 0.5 * 3, f"eight interfaces took {elapsed:.1f}s for a 0.5s budget"
