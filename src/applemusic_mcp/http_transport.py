"""Streamable HTTP transport: serve the MCP over a local HTTP endpoint instead of stdio.

`applemusic-mcp serve --transport http` runs one long-lived server that any
number of MCP clients connect to by URL (``http://127.0.0.1:8770/mcp``). It is
useful when a client can't spawn subprocesses, when several clients should share
one server (and one browser engine), or when the server should outlive a client.

Defaults are local-only and deliberately strict, because this process holds the
user's Apple Music tokens and can delete playlists:

- it binds 127.0.0.1 unless told otherwise;
- DNS-rebinding protection is on for loopback binds, so a web page cannot reach
  the server by pointing a hostname at 127.0.0.1;
- an optional bearer token (``--token`` or ``APPLEMUSIC_MCP_TOKEN``) gates every
  request, and binding to a non-loopback address refuses to start without one.

The mcp SDK's HTTP API differs between majors (1.x reads ``mcp.settings``, 2.x
takes keyword arguments), so ``build_app`` handles both and the app is run with
uvicorn directly rather than through ``mcp.run``.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
from typing import Any

ENV_TOKEN = "APPLEMUSIC_MCP_TOKEN"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8770
DEFAULT_PATH = "/mcp"
TRANSPORTS = ("stdio", "http")
_TRANSPORT_ALIASES = {"streamable-http": "http", "streamable_http": "http", "http": "http"}


def normalize_transport(value: str | None) -> str:
    """Map a user-supplied transport name to ``stdio`` or ``http``."""
    v = (value or "stdio").strip().lower()
    if v == "stdio":
        return "stdio"
    if v in _TRANSPORT_ALIASES:
        return _TRANSPORT_ALIASES[v]
    if v == "sse":
        raise ValueError(
            "the SSE transport is deprecated in MCP; use --transport http (Streamable HTTP)"
        )
    raise ValueError(f"unknown transport {value!r}; use one of: {', '.join(TRANSPORTS)}")


def is_loopback(host: str) -> bool:
    """True for addresses only this machine can reach."""
    h = host.strip().strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def normalize_path(path: str | None) -> str:
    p = (path or DEFAULT_PATH).strip()
    if not p.startswith("/"):
        p = "/" + p
    return p.rstrip("/") or DEFAULT_PATH


def resolve_token(cli_token: str | None) -> str | None:
    """The bearer token from ``--token`` or the environment; empty means none."""
    token = cli_token if cli_token is not None else os.environ.get(ENV_TOKEN, "")
    token = token.strip()
    return token or None


def security_settings(host: str):
    """Transport security for the bind address.

    Loopback: Host and Origin must be loopback, which blocks DNS rebinding.
    Anything else: the Host header can be any name the LAN uses for this machine,
    so the check can't be enumerated; the mandatory bearer token is the guard."""
    from mcp.server.transport_security import TransportSecuritySettings

    if is_loopback(host):
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
        )
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


class BearerAuth:
    """ASGI middleware: every HTTP request must carry ``Authorization: Bearer <token>``.

    Lifespan and other non-HTTP scopes pass straight through so the wrapped
    app's session manager still starts and stops."""

    def __init__(self, app: Any, token: str):
        self.app = app
        self._token = token.encode()

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        supplied = b""
        for name, value in scope.get("headers") or []:
            if name.lower() == b"authorization":
                scheme, _, rest = value.partition(b" ")
                if scheme.lower() == b"bearer":
                    supplied = rest.strip()
                break
        if supplied and hmac.compare_digest(supplied, self._token):
            return await self.app(scope, receive, send)
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32001, "message": "Unauthorized: missing or wrong bearer token"},
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b'Bearer realm="applemusic-mcp"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def build_app(
    mcp: Any, host: str = DEFAULT_HOST, path: str = DEFAULT_PATH, token: str | None = None
):
    """The Streamable HTTP ASGI app for ``mcp``, on mcp 1.x or 2.x."""
    sec = security_settings(host)
    path = normalize_path(path)
    try:  # mcp 2.x: configuration is passed to the app factory
        app = mcp.streamable_http_app(streamable_http_path=path, transport_security=sec, host=host)
    except TypeError:  # mcp 1.x: the factory takes no arguments and reads settings
        mcp.settings.host = host
        mcp.settings.streamable_http_path = path
        mcp.settings.transport_security = sec
        app = mcp.streamable_http_app()
    return BearerAuth(app, token) if token else app


def check_bind(host: str, token: str | None) -> None:
    """Refuse to expose the server beyond this machine without a token."""
    if not is_loopback(host) and not token:
        raise ValueError(
            f"refusing to listen on {host} without a bearer token: anyone who can reach "
            f"this address could control your library. Set {ENV_TOKEN} (or --token), "
            "or bind 127.0.0.1."
        )


def serve(
    mcp: Any,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    path: str = DEFAULT_PATH,
    token: str | None = None,
    log_level: str = "warning",
) -> None:  # pragma: no cover  # binds a real socket; the app itself is tested in-process
    """Run the Streamable HTTP server until interrupted. Logs go to stderr."""
    import sys

    import uvicorn

    check_bind(host, token)
    app = build_app(mcp, host, path, token)
    shown = f"[{host}]" if ":" in host else host
    auth = "bearer token required" if token else "no token (local only)"
    print(
        f"applemusic-mcp: Streamable HTTP on http://{shown}:{port}{normalize_path(path)} ({auth})",
        file=sys.stderr,
        flush=True,
    )
    uvicorn.run(app, host=host, port=port, log_level=log_level)
