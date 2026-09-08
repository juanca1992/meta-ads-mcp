"""HTTPX transport pinning each connection to a validated public address."""
import socket
import ssl

import anyio
import httpcore
import httpx


class PublicNetworkBackend(httpcore.AnyIOBackend):
    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        from .utils import _ip_is_disallowed, BlockedURLError
        import ipaddress

        with anyio.fail_after(timeout or 30):
            addresses = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            ips = list(dict.fromkeys(item[4][0] for item in addresses))
            if not ips or any(_ip_is_disallowed(ipaddress.ip_address(ip)) for ip in ips):
                raise BlockedURLError("Download connection resolves to a non-public address")
            # Connect to a literal IP, not the hostname: no second DNS resolution.
            # HTTPcore retains the original origin for Host, SNI and TLS checks.
            return await super().connect_tcp(ips[0], port, timeout, local_address, socket_options)


class PublicHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self):
        super().__init__(trust_env=False)
        # HTTPX 0.28 transport adapter, backed by HTTPcore's public pool API.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(), network_backend=PublicNetworkBackend(),
            max_connections=4, max_keepalive_connections=0, retries=0,
        )
