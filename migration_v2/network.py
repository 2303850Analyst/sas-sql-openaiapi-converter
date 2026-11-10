"""Outbound destination guard, supplementary to the corporate firewall."""
import ipaddress
import socket
import sys
from urllib.parse import urlsplit


def install_policy(runtime):
    endpoints = []
    for profile in (runtime.model, runtime.reviewer):
        if not profile: continue
        url = urlsplit(profile.endpoint)
        endpoints.append((url.hostname, url.port or 443))
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
        if event in {"socket.connect", "socket.sendto", "socket.bind"}:
            sock, address = args[0], args[1]
            if sock.family in {socket.AF_INET, socket.AF_INET6}:
                if not isinstance(address, tuple) or (not local(address[0]) and
                        (event == "socket.bind" or (address[0], address[1]) not in addresses)):
                    raise PermissionError("Destination is not configured")
        elif event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
            if args and not local(args[0]) and args[0] not in allowed_hosts:
                raise PermissionError("Name resolution is not configured")
    sys.addaudithook(audit)
