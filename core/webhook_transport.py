"""Pinned-IP transport for SSRF-safe webhook delivery.

The caller resolves and validates a webhook hostname once, then mounts this
adapter for the request. The pool keeps the original hostname for the HTTP
Host header and TLS SNI/certificate verification, while the connection socket
is opened against the already-validated IP address. This prevents a second
DNS lookup from changing the destination between validation and connect.
"""

from __future__ import annotations

from typing import Any

from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.poolmanager import PoolManager
from urllib3.util import connection as urllib3_connection


class _PinnedConnectionMixin:
    """Override only the TCP destination; leave hostname identity untouched."""

    _pinned_ip: str

    def __init__(self, *args: Any, pinned_ip: str, **kwargs: Any) -> None:
        self._pinned_ip = pinned_ip
        super().__init__(*args, **kwargs)

    def _new_conn(self):  # type: ignore[no-untyped-def]
        return urllib3_connection.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            source_address=self.source_address,
            socket_options=self.socket_options,
        )


class _PinnedHTTPConnection(_PinnedConnectionMixin, HTTPConnection):
    """HTTP connection whose TCP socket targets a fixed validated IP."""


class _PinnedHTTPSConnection(_PinnedConnectionMixin, HTTPSConnection):
    """HTTPS connection with fixed TCP IP and original-host TLS identity."""


class _PinnedHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _PinnedHTTPConnection


class _PinnedHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _PinnedHTTPSConnection


class _PinnedPoolManager(PoolManager):
    """Inject the pin after urllib3 has constructed its immutable pool key."""

    def __init__(self, pinned_ip: str, **kwargs: Any) -> None:
        self.pinned_ip = pinned_ip
        super().__init__(**kwargs)
        self.pool_classes_by_scheme = {
            "http": _PinnedHTTPConnectionPool,
            "https": _PinnedHTTPSConnectionPool,
        }

    def _new_pool(self, scheme, host, port, request_context=None):  # type: ignore[no-untyped-def]
        context = dict(request_context or self.connection_pool_kw)
        context["pinned_ip"] = self.pinned_ip
        return super()._new_pool(scheme, host, port, request_context=context)


class PinnedIPAdapter(HTTPAdapter):
    """Requests adapter that pins all direct connections to one IP address.

    Proxies are disabled by the caller because a proxy would become the actual
    network destination and would bypass the validated-IP guarantee. The
    adapter supports HTTP and HTTPS while retaining the requested hostname for
    the Host header and TLS certificate checks.
    """

    def __init__(self, pinned_ip: str, **kwargs: Any) -> None:
        self.pinned_ip = pinned_ip
        super().__init__(**kwargs)

    def init_poolmanager(
        self,
        connections: int,
        maxsize: int,
        block: bool = False,
        **pool_kwargs: Any,
    ) -> None:
        self._pool_connections = connections
        self._pool_maxsize = maxsize
        self._pool_block = block
        self.poolmanager = _PinnedPoolManager(
            pinned_ip=self.pinned_ip,
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            **pool_kwargs,
        )
