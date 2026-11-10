"""Loopback-only network policy for the current packaged offline MVP.

This is an application guard, not a substitute for the corporate firewall.
Future corporate API support must explicitly replace this policy with a
reviewed destination policy. No public fallback is permitted.
"""
from __future__ import annotations

import ipaddress
import socket
import sys


def _local(host) -> bool:
    if host is None or host in ("localhost", b"localhost"):
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    try:
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


def install_loopback_only() -> None:
    def audit(event, args):
        if event in {"socket.connect", "socket.sendto", "socket.bind"}:
            sock, address = args[0], args[1]
            if sock.family in {socket.AF_INET, socket.AF_INET6}:
                if not isinstance(address, tuple) or not _local(address[0]):
                    raise PermissionError("Packaged MVP permits loopback connections only")
        elif event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
            if args and not _local(args[0]):
                raise PermissionError("External name resolution is disabled in the packaged MVP")
    sys.addaudithook(audit)
