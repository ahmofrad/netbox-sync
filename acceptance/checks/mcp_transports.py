"""Packaged MCP STDIO/SSE/HTTP and GET-only synthetic-inventory acceptance."""
import asyncio
import json
import os
import subprocess
import sys
import time
from urllib.request import urlopen

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from netbox_sync_mcp.netbox_api import request
from users.models import Token

METHOD_LOG = "/tmp/netbox-sync-acceptance-mcp-methods.marker"


def assert_inventory(result):
    assert not result.isError, result
    text = result.content[0].text
    assert "Token" not in text and os.environ["MCP_NETBOX_TOKEN"] not in text
    payload = json.loads(text)
    assert payload["found"] is True
    assert payload["device"]["name"] == "fixture-server-1"
    return payload


async def query(read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        assert "netbox_get_device" in [tool.name for tool in tools.tools]
        result = await session.call_tool("netbox_get_device", {"identifier": "fixture-server-1"})
        return assert_inventory(result)


def mcp_environment():
    environment = dict(os.environ)
    environment["NETBOX_SYNC_ACCEPTANCE_MCP_METHOD_LOG"] = METHOD_LOG
    return environment


async def stdio():
    parameters = StdioServerParameters(command="/opt/netbox/venv/bin/netbox-sync-mcp", args=[],
                                      env=mcp_environment())
    async with stdio_client(parameters) as (read, write):
        await query(read, write)


async def transports():
    process = subprocess.Popen(
        ["/opt/netbox/venv/bin/netbox-sync-mcp", "--sse"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=mcp_environment())
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                async with sse_client("http://127.0.0.1:8901/mcp/sse") as (read, write):
                    await query(read, write)
                break
            except BaseException:
                if time.monotonic() >= deadline:
                    raise
                if process.poll() is not None:
                    raise AssertionError(process.stderr.read())
                await asyncio.sleep(0.2)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stderr.close()

    process = subprocess.Popen(
        ["/opt/netbox/venv/bin/netbox-sync-mcp", "--http"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=mcp_environment())
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                async with streamable_http_client("http://127.0.0.1:8901/mcp/") as (read, write, _):
                    await query(read, write)
                break
            except BaseException:
                if time.monotonic() >= deadline:
                    raise
                if process.poll() is not None:
                    raise AssertionError(process.stderr.read())
                await asyncio.sleep(0.2)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stderr.close()


async def main():
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        try:
            request(method, "/api/dcim/devices/")
            raise AssertionError(f"MCP allowed {method}")
        except ValueError as exc:
            assert "Only GET" in str(exc)
    await stdio()
    await transports()
    observed_methods = [line for line in open(METHOD_LOG, encoding="utf-8").read().splitlines() if line]
    assert observed_methods and set(observed_methods) == {"GET"}
    print(json.dumps({
        "stdio_synthetic_inventory": True,
        "sse_path": "/mcp/sse",
        "streamable_http_synthetic_inventory": True,
        "outbound_methods": sorted(set(observed_methods)),
        "outbound_get_count": len(observed_methods),
        "read_only_token_write_enabled": False,
        "non_get_methods_refused": ["POST", "PUT", "PATCH", "DELETE"],
        "token_not_returned": True,
    }))


token = Token.objects.get(plaintext=os.environ["MCP_NETBOX_TOKEN"])
assert token.write_enabled is False
with open(METHOD_LOG, "w", encoding="utf-8"):
    pass
try:
    asyncio.run(main())
finally:
    os.unlink(METHOD_LOG)
