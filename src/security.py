"""Local-only checks for the booth API and WebSocket.

Starlette 1.7 compares the hostname after it parses the port, and it keeps the
brackets on an IPv6 literal. Starlette 1.6 split the header on the first colon,
so ``[::1]:8080`` became ``[`` and was rejected. The allow list therefore has
the bare names (including ``::1`` and ``[::1]``) and the same names with the
configured port, for either parser.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlparse

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


def trusted_hosts(port: int = 8080) -> list[str]:
    hosts: list[str] = []
    for host in LOCAL_HOSTS:
        hosts.append(host)
        hosts.append(f"{host}:{int(port)}")
    return hosts


def origin_is_local(origin: str) -> bool:
    parsed = urlparse(origin or "")
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def token_matches(got: str, expected: str) -> bool:
    if not expected or not got or len(got) != len(expected):
        return False
    return secrets.compare_digest(got, expected)


class BoothGuard:
    """Reject foreign origins, non-JSON writes, and POSTs without the control token."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        path = scope.get("path") or ""
        origin = headers.get("origin")
        if scope["type"] == "websocket":
            if origin and not origin_is_local(origin):
                await JSONResponse({"ok": False, "error": "bad origin"}, status_code=403)(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return
        method = scope.get("method") or "GET"
        writing = path.startswith("/api/") and method != "GET"
        if (writing or path == "/api/session") and origin and not origin_is_local(origin):
            await JSONResponse({"ok": False, "error": "bad origin"}, status_code=403)(scope, receive, send)
            return
        if writing:
            ctype = (headers.get("content-type") or "").lower()
            if not ctype.startswith("application/json"):
                await JSONResponse({"ok": False, "error": "expected application/json"}, status_code=415)(
                    scope, receive, send
                )
                return
            app = scope.get("app")
            expected = getattr(getattr(app, "state", None), "control_token", "")
            if not token_matches(headers.get("x-cohost-token") or "", expected):
                await JSONResponse({"ok": False, "error": "missing control token"}, status_code=401)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)
