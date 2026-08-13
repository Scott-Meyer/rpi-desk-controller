"""Network identity helpers shared by controller applications."""

import ipaddress
import socket
from typing import Optional


def _usable_ipv4(value: str) -> Optional[str]:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if address.version != 4 or address.is_loopback or address.is_unspecified:
        return None
    return str(address)


def get_lan_ip(
    preferred_host: Optional[str] = None,
    preferred_port: int = 9,
) -> Optional[str]:
    """Return the IPv4 address selected for LAN traffic without sending data."""
    route_targets = []
    if preferred_host:
        route_targets.append((preferred_host, preferred_port))
    route_targets.append(("192.0.2.1", 9))

    for target in route_targets:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(target)
                address = _usable_ipv4(probe.getsockname()[0])
                if address:
                    return address
        except OSError:
            continue

    try:
        candidates = socket.getaddrinfo(
            socket.gethostname(),
            None,
            family=socket.AF_INET,
            type=socket.SOCK_DGRAM,
        )
    except OSError:
        return None

    for candidate in candidates:
        address = _usable_ipv4(candidate[4][0])
        if address:
            return address
    return None
