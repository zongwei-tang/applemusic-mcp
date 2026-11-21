"""Streamable HTTP transport, exercised in-process (no socket is bound).

A real MCP handshake (initialize → initialized → tools/list → tools/call) runs
through the ASGI app with Starlette's TestClient, on whichever mcp major is
installed, so CI's mcp-2.x job covers the 2.x code path too."""

import json

import pytest
from starlette.testclient import TestClient

from applemusic_mcp import cli, http_transport, server

LOCAL = "http://127.0.0.1:8770"
STRONG = "f" * 64  # passes the public-bind length check
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _rpc(method, params=None, id_=1):
    msg = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        msg["id"] = id_
    if params is not None:
        msg["params"] = params
    return msg


def _payload(resp):
    """The JSON-RPC message in a response, whether sent as JSON or as SSE."""
    if resp.headers.get("content-type", "").startswith("application/json"):
        return resp.json()
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(f"no JSON-RPC message in response: {resp.text[:200]!r}")


def _fresh_app(token=None, host="127.0.0.1"):
    # FastMCP 1.x creates one session manager per instance and it can only run
    # once, so each test builds its own server object.
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError:
        from mcp.server.mcpserver import MCPServer as FastMCP

    m = FastMCP("Apple Music")
    m.tool()(server.config)
    return http_transport.build_app(m, host=host, path="/mcp", token=token)


def _handshake(client, headers=HEADERS):
    r = client.post(
        "/mcp",
        headers=headers,
        json=_rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        ),
    )
    assert r.status_code == 200, r.text
    sid = r.headers.get("mcp-session-id")
    assert sid
    h = {**headers, "mcp-session-id": sid}
    r = client.post("/mcp", headers=h, json=_rpc("notifications/initialized", id_=None))
    assert r.status_code in (200, 202)
    return h, _payload(client.post("/mcp", headers=h, json=_rpc("tools/list", id_=2)))


def test_initialize_and_list_tools():
    with TestClient(_fresh_app(), base_url=LOCAL) as client:
        _, listed = _handshake(client)
    names = [t["name"] for t in listed["result"]["tools"]]
    assert names == ["config"]


def test_tool_call_over_http():
    with TestClient(_fresh_app(), base_url=LOCAL) as client:
        h, _ = _handshake(client)
        r = client.post(
            "/mcp",
            headers=h,
            json=_rpc("tools/call", {"name": "config", "arguments": {"action": "nope"}}, id_=3),
        )
    result = _payload(r)["result"]
    assert result["content"][0]["type"] == "text"


def test_dns_rebinding_host_rejected():
    # A browser page that rebinds evil.example → 127.0.0.1 sends Host: evil.example.
    with TestClient(_fresh_app(), base_url="http://evil.example") as client:
        r = client.post("/mcp", headers=HEADERS, json=_rpc("initialize", {}))
    assert r.status_code in (400, 403, 421)


def test_cross_origin_browser_request_rejected():
    with TestClient(_fresh_app(), base_url=LOCAL) as client:
        r = client.post(
            "/mcp",
            headers={**HEADERS, "Origin": "https://evil.example"},
            json=_rpc("initialize", {}),
        )
    assert r.status_code in (400, 403)


def test_bearer_token_required_and_accepted():
    app = _fresh_app(token="s3cret")
    with TestClient(app, base_url=LOCAL) as client:
        assert client.post("/mcp", headers=HEADERS, json=_rpc("ping")).status_code == 401
        wrong = {**HEADERS, "Authorization": "Bearer nope"}
        r = client.post("/mcp", headers=wrong, json=_rpc("ping"))
        assert r.status_code == 401
        assert r.json()["error"]["code"] == -32001
        basic = {**HEADERS, "Authorization": "Basic s3cret"}
        assert client.post("/mcp", headers=basic, json=_rpc("ping")).status_code == 401
        _, listed = _handshake(client, {**HEADERS, "Authorization": "Bearer s3cret"})
    assert listed["result"]["tools"]


def test_bare_token_and_lowercase_header_accepted():
    with TestClient(_fresh_app(token="s3cret"), base_url=LOCAL) as client:
        _, listed = _handshake(client, {**HEADERS, "authorization": "s3cret"})
        assert listed["result"]["tools"]
        _, listed = _handshake(client, {**HEADERS, "authorization": "bearer   s3cret "})
        assert listed["result"]["tools"]


def test_rejection_reason_is_logged_without_the_token(capsys):
    with TestClient(_fresh_app(token="s3cret"), base_url=LOCAL) as client:
        client.post("/mcp", headers=HEADERS, json=_rpc("ping"))
        client.post("/mcp", headers={**HEADERS, "Authorization": "Bearer wrong-one"}, json={})
        client.post("/mcp", headers={**HEADERS, "Authorization": "Basic s3cret"}, json={})
    err = capsys.readouterr().err
    assert "401 POST /mcp: no Authorization header" in err
    assert "mismatch (got 9 chars, expected 6)" in err
    assert "scheme is not Bearer" in err
    assert "wrong-one" not in err and "s3cret" not in err


def test_oauth_discovery_is_404_not_401():
    with TestClient(_fresh_app(token="s3cret"), base_url=LOCAL) as client:
        for p in (
            "/.well-known/oauth-protected-resource/mcp",
            "/.well-known/oauth-authorization-server",
        ):
            assert client.get(p).status_code == 404


def test_secret_store_value_with_bearer_prefix_or_quotes(monkeypatch):
    monkeypatch.setenv(http_transport.ENV_TOKEN, ' "Bearer abc123" ')
    assert http_transport.resolve_token(None) == "abc123"
    monkeypatch.setenv(http_transport.ENV_TOKEN, "'xyz'\n")
    assert http_transport.resolve_token(None) == "xyz"


def test_non_loopback_host_has_no_host_check_but_needs_token():
    sec = http_transport.security_settings("0.0.0.0")
    assert sec.enable_dns_rebinding_protection is False
    with pytest.raises(ValueError, match="bearer token"):
        http_transport.check_bind("0.0.0.0", None)
    with pytest.raises(ValueError, match="at least 32"):
        http_transport.check_bind("0.0.0.0", "short-token")
    http_transport.check_bind("0.0.0.0", STRONG)  # no raise
    http_transport.check_bind("127.0.0.1", None)
    http_transport.check_bind("127.0.0.1", "short is fine locally")
    http_transport.check_bind("::1", None)


def test_resolve_port(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    assert http_transport.resolve_port(None) == 8770
    monkeypatch.setenv("PORT", "4321")
    assert http_transport.resolve_port(None) == 4321
    assert http_transport.resolve_port(9000) == 9000  # --port wins
    for bad, msg in (("http", "not a port"), ("0", "out of range"), ("70000", "out of range")):
        monkeypatch.setenv("PORT", bad)
        with pytest.raises(ValueError, match=msg):
            http_transport.resolve_port(None)


def test_healthz_needs_no_token_and_reveals_nothing():
    with TestClient(_fresh_app(token=STRONG), base_url=LOCAL) as client:
        r = client.get("/healthz")
        assert (r.status_code, r.text) == (200, "ok")
        assert client.get("/healthz/x").status_code == 401  # only the exact path
        assert client.post("/mcp", headers=HEADERS, json=_rpc("ping")).status_code == 401


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", True),
        ("127.0.0.2", True),
        ("localhost", True),
        ("::1", True),
        ("[::1]", True),
        ("0.0.0.0", False),
        ("192.168.1.5", False),
        ("my-mac.local", False),
    ],
)
def test_is_loopback(host, expected):
    assert http_transport.is_loopback(host) is expected


def test_normalize_transport():
    assert http_transport.normalize_transport(None) == "stdio"
    assert http_transport.normalize_transport("STDIO") == "stdio"
    assert http_transport.normalize_transport("http") == "http"
    assert http_transport.normalize_transport("streamable-http") == "http"
    with pytest.raises(ValueError, match="deprecated"):
        http_transport.normalize_transport("sse")
    with pytest.raises(ValueError, match="unknown transport"):
        http_transport.normalize_transport("carrier-pigeon")


def test_normalize_path_and_token(monkeypatch):
    assert http_transport.normalize_path(None) == "/mcp"
    assert http_transport.normalize_path("music/") == "/music"
    assert http_transport.normalize_path("/") == "/mcp"
    monkeypatch.delenv(http_transport.ENV_TOKEN, raising=False)
    assert http_transport.resolve_token(None) is None
    monkeypatch.setenv(http_transport.ENV_TOKEN, "  envtok ")
    assert http_transport.resolve_token(None) == "envtok"
    assert http_transport.resolve_token("cli") == "cli"
    assert http_transport.resolve_token("") is None


class TestServeCli:
    def _run(self, monkeypatch, argv):
        calls = []
        monkeypatch.setattr(server, "main", lambda **kw: calls.append(kw))
        monkeypatch.setattr("sys.argv", ["applemusic-mcp", "serve", *argv])
        cli.main()
        return calls

    def test_default_is_stdio(self, monkeypatch):
        assert self._run(monkeypatch, []) == [{}]

    def test_http_defaults(self, monkeypatch):
        monkeypatch.delenv(http_transport.ENV_TOKEN, raising=False)
        monkeypatch.delenv("PORT", raising=False)
        calls = self._run(monkeypatch, ["--transport", "http"])
        assert calls == [
            {"transport": "http", "host": "127.0.0.1", "port": 8770, "path": "/mcp", "token": None}
        ]

    def test_http_options_and_env_token(self, monkeypatch):
        monkeypatch.setenv(http_transport.ENV_TOKEN, STRONG)
        calls = self._run(
            monkeypatch,
            [
                "--transport",
                "streamable-http",
                "--host",
                "0.0.0.0",
                "--port",
                "9001",
                "--path",
                "x",
            ],
        )
        assert calls == [
            {"transport": "http", "host": "0.0.0.0", "port": 9001, "path": "/x", "token": STRONG}
        ]

    def test_port_from_env_and_bad_port(self, monkeypatch, capsys):
        monkeypatch.delenv(http_transport.ENV_TOKEN, raising=False)
        monkeypatch.setenv("PORT", "5555")
        assert self._run(monkeypatch, ["--transport", "http"])[0]["port"] == 5555
        monkeypatch.setenv("PORT", "nope")
        with pytest.raises(SystemExit) as e:
            self._run(monkeypatch, ["--transport", "http"])
        assert e.value.code == 2 and "not a port" in capsys.readouterr().err

    def test_public_bind_with_weak_token_exits(self, monkeypatch, capsys):
        monkeypatch.setenv(http_transport.ENV_TOKEN, "password")
        with pytest.raises(SystemExit) as e:
            self._run(monkeypatch, ["--transport", "http", "--host", "0.0.0.0"])
        assert e.value.code == 2 and "at least 32" in capsys.readouterr().err

    def test_no_playback_flag_sets_env(self, monkeypatch):
        # setenv (not delenv) so monkeypatch records the variable and removes the
        # value the CLI writes straight into os.environ when the test ends.
        monkeypatch.setenv(server.ENV_NO_PLAYBACK, "0")
        self._run(monkeypatch, ["--no-playback"])
        import os

        assert os.environ.get(server.ENV_NO_PLAYBACK) == "1"

    def test_public_bind_without_token_exits(self, monkeypatch, capsys):
        monkeypatch.delenv(http_transport.ENV_TOKEN, raising=False)
        with pytest.raises(SystemExit) as e:
            self._run(monkeypatch, ["--transport", "http", "--host", "0.0.0.0"])
        assert e.value.code == 2
        assert "bearer token" in capsys.readouterr().err

    def test_bad_transport_exits(self, monkeypatch, capsys):
        with pytest.raises(SystemExit) as e:
            self._run(monkeypatch, ["--transport", "sse"])
        assert e.value.code == 2
        assert "deprecated" in capsys.readouterr().err


def test_tool_calls_are_serialized():
    """A registered tool waits while another call holds the lock, on mcp 1.x
    (event loop) and 2.x (worker thread) alike, so HTTP clients can't overlap."""
    import asyncio
    import threading

    started, release, done = threading.Event(), threading.Event(), threading.Event()

    def holder():
        with server._TOOL_CALL_LOCK:
            started.set()
            release.wait(5)

    def caller():
        asyncio.run(server.mcp.call_tool("config", {"action": "bogus"}))
        done.set()

    h = threading.Thread(target=holder)
    h.start()
    assert started.wait(5)
    c = threading.Thread(target=caller)
    c.start()
    try:
        assert not done.wait(0.3), "tool call ran while another held the lock"
    finally:
        release.set()
    assert done.wait(5)
    h.join(5)
    c.join(5)


def test_other_loopback_bind_address_is_reachable():
    sec = http_transport.security_settings("127.0.0.2")
    assert "127.0.0.2:*" in sec.allowed_hosts and "http://127.0.0.2:*" in sec.allowed_origins
    assert "[::1]:*" in http_transport.security_settings("[::1]").allowed_hosts
    assert len(http_transport.security_settings("localhost").allowed_hosts) == 3
    with TestClient(_fresh_app(host="127.0.0.2"), base_url="http://127.0.0.2:8770") as client:
        _, listed = _handshake(client)
    assert listed["result"]["tools"]
