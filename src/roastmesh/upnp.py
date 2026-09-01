"""UPnP IGD port mapping -- the third way to ask a router for a port.

PCP and NAT-PMP (port_mapping.py) are one UDP round trip each and are what a
modern or Apple-lineage router speaks. A large share of consumer routers speak
only this instead, so without it those users get nothing from `--public-port
auto` and are back to reading their router's admin page.

The protocol is small: a multicast probe, one HTTP GET, and some SOAP. What is
*not* small is the failure handling, and that is the reason this module reads
the way it does. Every error branch below is taken from libtorrent's own UPnP
implementation (`src/upnp.cpp`), which unlike Transmission does not vendor a
library and therefore had to write these workarounds down:

    725  the router refuses timed leases      -> ask again for a permanent one
    718  that external port is taken          -> try a different one
    501  "action failed", which some routers
         return instead of 718                -> same treatment as 718
    727  external port must be a wildcard     -> this router cannot help us

**Everything here is hostile input.** A UPnP device is whatever answered a
multicast probe; there is no authentication anywhere in the protocol, and the
device describes itself. So responses from outside the subnet we probed are
ignored, the description URL must point somewhere private, bodies are capped,
redirects are refused, and XML carrying entity declarations is rejected before
it reaches a parser. We ask only ever for a mapping to our own address and our
own port.
"""
from __future__ import annotations

import asyncio
import random
import re
import selectors
import socket
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from roastmesh.interfaces import local_interfaces, same_subnet

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900

# libtorrent sends this verbatim (upnp.cpp:248). MX is the most seconds a
# device may wait before answering, so it also sets how long a probe is worth
# listening for.
SSDP_MX = 2
_M_SEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
    "ST:upnp:rootdevice\r\n"
    'MAN:"ssdp:discover"\r\n'
    f"MX:{SSDP_MX}\r\n"
    "\r\n\r\n"
).encode("ascii")

# libtorrent retries up to twelve times over about two minutes. We do not:
# this runs behind two protocols that answer in one round trip, and
# wan_discovery retries the whole attempt every 15 minutes anyway. A discovery
# that stalls a serve round costs more than one that misses a router this time.
SSDP_ATTEMPTS = 3
SSDP_ATTEMPT_TIMEOUT_S = 2.0

# The services that can forward a port. Both WANIPConnection versions and the
# PPP variant, exactly as libtorrent matches them (upnp.cpp:972-974).
_WAN_SERVICES = (
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)

MAX_BODY_BYTES = 256 * 1024
HTTP_TIMEOUT_S = 5.0
DEFAULT_LIFETIME_S = 3600

# libtorrent's retry budget for a conflicting external port, and the range it
# picks a replacement from.
MAX_CONFLICT_RETRIES = 4
_RANDOM_PORT_RANGE = (40000, 50000)


@dataclass(frozen=True)
class Igd:
    """A router that told us it can forward ports."""

    control_url: str
    service_ns: str


@dataclass(frozen=True)
class UpnpMapping:
    external_port: int
    lifetime_s: int
    external_ip: str | None
    igd: Igd


class UpnpError(Exception):
    """A SOAP fault, carrying the numeric code the router gave."""

    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(f"UPnP error {code}{': ' + message if message else ''}")
        self.code = code


# --- discovery --------------------------------------------------------------

def _ssdp_probe_once(timeout: float) -> list[tuple[str, str]]:
    """One M-SEARCH per interface; returns (location_url, source_ip) pairs.

    Per interface, not once globally, and for the same reason the LAN beacon
    is: a single send to the multicast address follows the default route, and
    on a machine with a VPN up that route is the tunnel -- where there is no
    router to find. Transmission passes a multicast interface to its own
    discovery for exactly this.

    All the probes go out first and every socket is then read under one shared
    deadline. Doing it interface by interface instead multiplies the timeout by
    the number of interfaces -- measured at 16 seconds on a machine with seven,
    for a search budgeted at two.
    """
    interfaces = local_interfaces() or [None]  # type: ignore[list-item]
    sockets: list[tuple[socket.socket, object]] = []

    for iface in interfaces:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if iface is not None:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                socket.inet_aton(iface.address))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.sendto(_M_SEARCH, (SSDP_ADDR, SSDP_PORT))
        except OSError:
            sock.close()      # an interface that cannot carry multicast is fine
            continue
        sockets.append((sock, iface))

    found: list[tuple[str, str]] = []
    try:
        _collect_responses(sockets, timeout, found)
    finally:
        for sock, _iface in sockets:
            sock.close()
    return found


def _collect_responses(sockets, timeout: float, found: list[tuple[str, str]]) -> None:
    if not sockets:
        return
    by_fd = {sock.fileno(): (sock, iface) for sock, iface in sockets}
    selector = selectors.DefaultSelector()
    for sock, _iface in sockets:
        selector.register(sock, selectors.EVENT_READ)
    end = time.monotonic() + timeout
    try:
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            for key, _events in selector.select(remaining):
                sock, iface = by_fd[key.fd]
                try:
                    data, addr = sock.recvfrom(4096)
                except OSError:
                    continue
                # libtorrent's match_addr_mask: a device that is not even on
                # the network we probed has no business answering for it.
                if iface is not None and not same_subnet(iface.address, iface.netmask, addr[0]):
                    continue
                location = _header(data, "location")
                if location and _location_is_plausible(location, addr[0]):
                    found.append((location, addr[0]))
    finally:
        selector.close()


def _header(raw: bytes, name: str) -> str | None:
    for line in raw.split(b"\r\n"):
        key, _, value = line.partition(b":")
        if key.strip().lower() == name.encode("ascii"):
            return value.strip().decode("ascii", "replace")
    return None


def _is_private(host: str) -> bool:
    try:
        octets = socket.inet_aton(host)
    except OSError:
        return False
    a, b = octets[0], octets[1]
    return (a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b < 32)
            or (a == 169 and b == 254) or a == 127)


def _location_is_plausible(location: str, source_ip: str) -> bool:
    """The description URL has to point back at the device that offered it.

    Without this, anything on the LAN can answer a probe with a LOCATION
    pointing anywhere it likes and have us fetch it -- turning a port-mapping
    attempt into an HTTP request of someone else's choosing.
    """
    parsed = urlparse(location)
    if parsed.scheme != "http" or not parsed.hostname:
        return False
    return _is_private(parsed.hostname) and parsed.hostname == source_ip


# --- fetching and parsing ---------------------------------------------------

class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None      # a router has no business redirecting us elsewhere


_opener = urllib.request.build_opener(_NoRedirects)


def _http(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    try:
        with _opener.open(request, timeout=timeout) as response:
            return response.status, response.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # A SOAP fault arrives as HTTP 500 with the error code in the body, so
        # the body matters more than the status here.
        return exc.code, exc.read(MAX_BODY_BYTES + 1)


_ENTITY_DECL = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def _parse_xml(body: bytes):
    """Parse only after refusing the shapes that make parsing dangerous.

    `xml.etree` does not fetch external entities, but it does expand internal
    ones, and a few hundred bytes of nested declarations is enough to exhaust
    memory. A size cap does not bound that -- refusing the declaration does.
    """
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("device description is implausibly large")
    if _ENTITY_DECL.search(body):
        raise ValueError("device description carries an entity declaration")
    return ET.fromstring(body.decode("utf-8", "replace"))


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def describe(location: str, *, timeout: float = HTTP_TIMEOUT_S) -> Igd | None:
    """Fetch a device description and find its port-forwarding service."""
    status, body = _http(urllib.request.Request(location, method="GET"), timeout)
    if status != 200:
        return None
    try:
        root = _parse_xml(body)
    except (ValueError, ET.ParseError):
        return None

    base = location
    for element in root.iter():
        if _strip_ns(element.tag) == "urlbase" and (element.text or "").strip():
            base = element.text.strip()
            break

    for service in root.iter():
        if _strip_ns(service.tag) != "service":
            continue
        fields = {_strip_ns(child.tag): (child.text or "").strip() for child in service}
        service_type = fields.get("servicetype", "")
        control = fields.get("controlurl", "")
        if service_type in _WAN_SERVICES and control:
            url = urljoin(base if base.endswith("/") else base + "/", control) \
                if not control.startswith("http") else control
            # A control URL is a URL the device chose. It must still point at
            # the device, or the SOAP posts go somewhere else entirely.
            host = urlparse(url).hostname
            if host and _is_private(host):
                return Igd(control_url=url, service_ns=service_type)
    return None


def discover(*, attempts: int = SSDP_ATTEMPTS,
             timeout: float = SSDP_ATTEMPT_TIMEOUT_S) -> Igd | None:
    seen: set[str] = set()
    for _ in range(attempts):
        for location, _source in _ssdp_probe_once(timeout):
            if location in seen:
                continue
            seen.add(location)
            igd = describe(location)
            if igd is not None:
                return igd
    return None


# --- SOAP -------------------------------------------------------------------

_ENVELOPE = (
    '<?xml version="1.0"?>\n'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    '<s:Body><u:{action} xmlns:u="{ns}">{body}</u:{action}></s:Body></s:Envelope>'
)


def soap(igd: Igd, action: str, fields: str, *, timeout: float = HTTP_TIMEOUT_S) -> dict[str, str]:
    """One SOAP call. Raises UpnpError with the router's own code on a fault."""
    payload = _ENVELOPE.format(action=action, ns=igd.service_ns, body=fields).encode("utf-8")
    request = urllib.request.Request(
        igd.control_url, data=payload, method="POST",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "Soapaction": f'"{igd.service_ns}#{action}"',
        },
    )
    status, body = _http(request, timeout)
    try:
        root = _parse_xml(body)
    except (ValueError, ET.ParseError) as exc:
        raise UpnpError(-1, str(exc)) from None

    if status != 200:
        code = None
        for element in root.iter():
            if _strip_ns(element.tag) == "errorcode":
                code = (element.text or "").strip()
        raise UpnpError(int(code) if code and code.isdigit() else -1, f"HTTP {status}")
    return {_strip_ns(e.tag): (e.text or "") for e in root.iter()}


def get_external_ip(igd: Igd) -> str | None:
    try:
        result = soap(igd, "GetExternalIPAddress", "")
    except (UpnpError, OSError):
        return None
    value = result.get("newexternalipaddress", "").strip()
    return value or None


def delete_mapping(igd: Igd, external_port: int) -> bool:
    fields = ("<NewRemoteHost></NewRemoteHost>"
              f"<NewExternalPort>{external_port}</NewExternalPort>"
              "<NewProtocol>UDP</NewProtocol>")
    try:
        soap(igd, "DeletePortMapping", fields)
        return True
    except (UpnpError, OSError):
        return False


def _add_once(igd: Igd, internal_port: int, external_port: int,
              internal_client: str, lease_s: int) -> None:
    fields = ("<NewRemoteHost></NewRemoteHost>"
              f"<NewExternalPort>{external_port}</NewExternalPort>"
              "<NewProtocol>UDP</NewProtocol>"
              f"<NewInternalPort>{internal_port}</NewInternalPort>"
              f"<NewInternalClient>{internal_client}</NewInternalClient>"
              "<NewEnabled>1</NewEnabled>"
              "<NewPortMappingDescription>roastmesh</NewPortMappingDescription>"
              f"<NewLeaseDuration>{lease_s}</NewLeaseDuration>")
    soap(igd, "AddPortMapping", fields)


def add_mapping(igd: Igd, internal_port: int, internal_client: str,
                lease_s: int = DEFAULT_LIFETIME_S) -> UpnpMapping | None:
    """Ask for a mapping, working around the routers that say no.

    The retries are the whole point of this function; see the module docstring
    for where each of them comes from.
    """
    external_port = internal_port
    conflicts = 0
    while True:
        try:
            _add_once(igd, internal_port, external_port, internal_client, lease_s)
            return UpnpMapping(external_port=external_port, lifetime_s=lease_s,
                               external_ip=get_external_ip(igd), igd=igd)
        except OSError:
            return None
        except UpnpError as exc:
            if exc.code == 725 and lease_s != 0:
                # OnlyPermanentLeasesSupported. Plenty of routers do nothing
                # else, so take the permanent mapping -- wan_discovery deletes
                # it on the way out, which is the only thing that will.
                lease_s = 0
                continue
            if exc.code == 501 and conflicts >= 1:
                # 501 twice, on two different ports, is not a conflict.
                #
                # libtorrent treats 501 exactly like 718 because some routers
                # substitute it for a genuine port clash, and that is worth one
                # retry. But measured against a router that refuses to create
                # mappings for us at all -- while answering every query and
                # processing deletes -- it returns 501 for every port, forever.
                # Spending the full conflict budget there is five failed SOAP
                # calls to learn what the second one already said.
                return None
            if exc.code in (718, 501) and conflicts < MAX_CONFLICT_RETRIES:
                # 718 is "that port is already mapped"; some routers say 501
                # "action failed" for the same thing. Either way the fix worth
                # trying is a different external port, not a different request.
                conflicts += 1
                external_port = random.randint(*_RANDOM_PORT_RANGE)
                continue
            return None    # 727 and everything else: this router cannot help


# --- the async surface port_mapping calls -----------------------------------

def _map_blocking(internal_port: int, lease_s: int) -> UpnpMapping | None:
    igd = discover()
    if igd is None:
        return None
    host = urlparse(igd.control_url).hostname
    if host is None:
        return None
    internal_client = _local_address_towards(host)
    if internal_client is None:
        return None
    return add_mapping(igd, internal_port, internal_client, lease_s)


def _local_address_towards(host: str) -> str | None:
    """Our address on the route to the router -- what NewInternalClient must
    hold. The same trick port_mapping uses to fill PCP's client field."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


async def map_udp_port(internal_port: int, *,
                       lifetime_s: int = DEFAULT_LIFETIME_S) -> UpnpMapping | None:
    return await asyncio.to_thread(_map_blocking, internal_port, lifetime_s)


async def unmap(mapping: UpnpMapping) -> bool:
    return await asyncio.to_thread(delete_mapping, mapping.igd, mapping.external_port)
