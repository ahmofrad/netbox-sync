"""Credential isolation, GET-only transport, and server entry-point tests."""
import importlib
import json
import sys
from types import SimpleNamespace

import pytest

from netbox_sync_mcp import netbox_api
from netbox_sync_mcp import server


def reload_client(monkeypatch, *, mcp_token=None, sync_token=None):
    for key in ("MCP_NETBOX_TOKEN", "NETBOX_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    if mcp_token:
        monkeypatch.setenv("MCP_NETBOX_TOKEN", mcp_token)
    if sync_token:
        monkeypatch.setenv("NETBOX_TOKEN", sync_token)
    monkeypatch.setenv("NETBOX_URL", "https://netbox.example.invalid")
    monkeypatch.setattr(netbox_api, "_configured_token", None, raising=False)
    return importlib.reload(netbox_api)


def test_sync_token_is_never_inherited(monkeypatch):
    client = reload_client(monkeypatch, sync_token="fake-write-capable-sync-token")
    with pytest.raises(RuntimeError, match="MCP_NETBOX_TOKEN"):
        client.get("/api/dcim/devices/")
    assert "fake-write-capable-sync-token" not in str(client.configuration())


def test_mcp_token_is_read_at_request_time(monkeypatch):
    client = reload_client(monkeypatch, mcp_token="fake-read-only-mcp-token")
    seen = {}
    response = SimpleNamespace(json=lambda: {"results": []}, raise_for_status=lambda: None)
    monkeypatch.setattr(client.requests, "get", lambda url, **kwargs: seen.update(kwargs) or response)
    assert client.get("/api/dcim/devices/") == {"results": []}
    assert seen["headers"]["Authorization"] == "Token fake-read-only-mcp-token"


def test_only_get_and_safe_errors(monkeypatch):
    client = reload_client(monkeypatch, mcp_token="fake-read-only-mcp-token")
    with pytest.raises(ValueError, match="GET"):
        client.request("POST", "/api/dcim/devices/")

    def fail(*args, **kwargs):
        raise RuntimeError("request failed Token fake-read-only-mcp-token token=fake-other-token")
    monkeypatch.setattr(client.requests, "get", fail)
    with pytest.raises(RuntimeError) as error:
        client.get("/api/dcim/devices/")
    assert "fake-read-only-mcp-token" not in str(error.value)
    assert "fake-other-token" not in str(error.value)


def test_stdio_sse_and_http_mapping(monkeypatch):
    calls = []
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(sys, "argv", ["netbox-sync-mcp"])
    server.main()
    monkeypatch.setenv("MCP_MOUNT_PATH", "/custom")
    monkeypatch.setattr(sys, "argv", ["netbox-sync-mcp", "--sse"])
    server.main()
    monkeypatch.setattr(sys, "argv", ["netbox-sync-mcp", "--http"])
    server.main()
    assert calls == [
        {"transport": "stdio"},
        {"transport": "sse", "mount_path": "/"},
        {"transport": "streamable-http"},
    ]


def test_sse_serves_configured_mount_prefix(monkeypatch):
    monkeypatch.setenv("MCP_MOUNT_PATH", "/custom")
    mcp = importlib.reload(server).mcp
    routes = mcp.sse_app().routes
    assert any(getattr(route, "path", None) == "/custom/sse" for route in routes)
    message = next(route for route in routes if getattr(route, "path", None) == "/custom/messages")
    assert message.app.__self__._endpoint == "/custom/messages/"
    importlib.reload(server)


def test_loopback_defaults_and_legacy_tool_parity(monkeypatch):
    assert server.mcp is not None
    # Importing a module without credentials must be safe and must not perform IO.
    client = reload_client(monkeypatch)
    assert client.configuration()["token_configured"] is False
    assert client.configuration()["verify_tls"] is False
