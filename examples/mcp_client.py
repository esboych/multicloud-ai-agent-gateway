"""Call the gateway through its MCP facade with nothing but the stdlib.

    python -m multicloud_ai_agent_gateway            # in another terminal
    python examples/mcp_client.py "say hi"
    GATEWAY_BEARER_TOKEN=... python examples/mcp_client.py "say hi" --tool DemoAgent

It performs the MCP Streamable HTTP handshake (initialize, initialized),
lists the tools and calls one. Any MCP client (Claude Desktop, Cursor,
VS Code, the MCP Inspector) works the same way against ``POST /mcp``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any


def rpc(url: str, method: str, params: dict[str, Any] | None, *, id_: int | None, token: str) -> Any:
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    if id_ is not None:
        body["id"] = id_
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=900) as response:
        raw = response.read()
    if not raw:
        return None  # notifications are answered with 202 and no body
    payload = json.loads(raw)
    if "error" in payload:
        raise SystemExit(f"{method}: {payload['error']}")
    return payload["result"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--tool", default="AutoRouter")
    parser.add_argument("--url", default=os.environ.get("GATEWAY_URL", "http://localhost:8080") + "/mcp")
    parser.add_argument("--conversation-id")
    args = parser.parse_args()
    token = os.environ.get("GATEWAY_BEARER_TOKEN", "")

    init = rpc(
        args.url,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "example", "version": "0"}},
        id_=1,
        token=token,
    )
    print(f"server: {init['serverInfo']['name']} {init['serverInfo']['version']} (protocol {init['protocolVersion']})")
    rpc(args.url, "notifications/initialized", None, id_=None, token=token)

    tools = rpc(args.url, "tools/list", None, id_=2, token=token)["tools"]
    print("tools:", ", ".join(t["name"] for t in tools))

    arguments: dict[str, Any] = {"prompt": args.prompt}
    if args.conversation_id:
        arguments["conversation_id"] = args.conversation_id
    result = rpc(args.url, "tools/call", {"name": args.tool, "arguments": arguments}, id_=3, token=token)
    if result.get("isError"):
        print("tool error:", result["content"][0]["text"], file=sys.stderr)
        return 1
    structured = result.get("structuredContent") or {}
    print(f"agent: {structured.get('agent')}  conversation_id: {structured.get('conversation_id')}")
    print(result["content"][0]["text"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
