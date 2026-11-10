"""Outbound destination guard, supplementary to the corporate firewall."""
from contextlib import contextmanager
from contextvars import ContextVar
import ipaddress
import socket
import sys
from urllib.parse import urlsplit


_route = ContextVar("approved_model_route", default=(None, frozenset()))


@contextmanager
def model_route(profile):
    """Authorize only this configured model destination for one transport context."""
    if not profile.interactive:
        yield
        return
    url = urlsplit(profile.proxy or profile.endpoint)
    port = url.port or (443 if url.scheme == "https" else 80)
    resolving = _route.set((url.hostname, frozenset()))
    try:
        addresses = {(record[4][0], port)
                     for record in socket.getaddrinfo(url.hostname, port, type=socket.SOCK_STREAM)}
        connected = _route.set((url.hostname, addresses))
        try:
            yield
        finally:
            _route.reset(connected)
    finally:
        _route.reset(resolving)


def install_policy(runtime):
    endpoints = []
    for profile in (runtime.model, runtime.reviewer):
        if not profile: continue
        url = urlsplit(profile.endpoint)
        endpoints.append((url.hostname, url.port or (443 if url.scheme == "https" else 80)))
        if profile.proxy:
            proxy = urlsplit(profile.proxy)
            if proxy.scheme not in {"http", "https"} or not proxy.hostname or proxy.username:
                raise ValueError("Proxy requires an explicit HTTP(S) host, no embedded credentials")
            endpoints.append((proxy.hostname, proxy.port or (443 if proxy.scheme == "https" else 80)))
    if runtime.database:
        endpoints.append((runtime.database.host, runtime.database.port))
    allowed_hosts = {h for h, _ in endpoints}
    addresses = set()
    for host, port in endpoints:
        for record in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
            addresses.add((record[4][0], port))

    def local(host):
        if host in {None, "localhost", b"localhost"}:
            return True
        try:
            return ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host).is_loopback
        except (ValueError, TypeError):
            return False

    def audit(event, args):
        route_host, route_addresses = _route.get()
        if event in {"socket.connect", "socket.sendto", "socket.bind"}:
            sock, address = args[0], args[1]
            if sock.family in {socket.AF_INET, socket.AF_INET6}:
                if not isinstance(address, tuple) or (not local(address[0]) and
                        (event == "socket.bind" or (address[0], address[1]) not in addresses and (address[0], address[1]) not in route_addresses)):
                    raise PermissionError("Destination is not configured")
        elif event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
            if args and not local(args[0]) and args[0] not in allowed_hosts and args[0] != route_host:
                raise PermissionError("Name resolution is not configured")
    sys.addaudithook(audit)
