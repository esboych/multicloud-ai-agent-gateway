"""Talk to the gateway as an A2A client with nothing but the stdlib.

    python -m multicloud_ai_agent_gateway            # in another terminal
    python examples/a2a_client.py "say hi"
    python examples/a2a_client.py "say hi" --agent DemoAgent --stream

Discovers the agent card, sends one message (``message/send``) or streams it
(``message/stream``) and prints the answer. ``--context-id`` continues an
earlier conversation.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
import uuid
from typing import Any


def _headers(token: str, accept: str = "application/json") -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--agent", help="skill id; omit to let the gateway route")
    parser.add_argument("--context-id")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--base-url", default=os.environ.get("GATEWAY_URL", "http://localhost:8080"))
    args = parser.parse_args()
    token = os.environ.get("GATEWAY_BEARER_TOKEN", "")

    with urllib.request.urlopen(
        urllib.request.Request(args.base_url + "/.well-known/agent-card.json", headers=_headers(token)),
        timeout=30,
    ) as response:
        card = json.loads(response.read())
    print(f"agent card: {card['name']} -> {card['url']}; skills: {', '.join(s['id'] for s in card['skills'])}")

    message: dict[str, Any] = {
        "role": "user",
        "parts": [{"kind": "text", "text": args.prompt}],
        "messageId": uuid.uuid4().hex,
        "kind": "message",
    }
    if args.context_id:
        message["contextId"] = args.context_id
    params: dict[str, Any] = {"message": message}
    if args.agent:
        params["metadata"] = {"agent": args.agent}
    body = {"jsonrpc": "2.0", "id": "1", "method": "message/stream" if args.stream else "message/send", "params": params}
    request = urllib.request.Request(
        card["url"],
        data=json.dumps(body).encode(),
        headers=_headers(token, "text/event-stream" if args.stream else "application/json"),
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=900) as response:
        if not args.stream:
            payload = json.loads(response.read())
            if "error" in payload:
                raise SystemExit(f"error: {payload['error']}")
            task = payload["result"]
            print(f"task {task['id']} {task['status']['state']}  contextId: {task['contextId']}  agent: {task['metadata'].get('agent')}")
            for artifact in task.get("artifacts", []):
                for part in artifact["parts"]:
                    print(part.get("text", ""))
            return 0

        for raw in response:
            line = raw.decode("utf-8").rstrip("\n")
            if not line.startswith("data:"):
                continue
            event = json.loads(line[len("data:"):].strip())
            if "error" in event:
                raise SystemExit(f"error: {event['error']}")
            result = event["result"]
            kind = result.get("kind")
            if kind == "task":
                print(f"task {result['id']} {result['status']['state']}")
            elif kind == "status-update":
                state = result["status"]["state"]
                extra = ""
                if result.get("metadata", {}).get("routing"):
                    extra = f" -> {result['metadata']['routing']['selected_agent']}"
                print(f"[{state}{extra}]")
                if result.get("final"):
                    print(f"contextId: {result['contextId']}")
            elif kind == "artifact-update":
                for part in result["artifact"]["parts"]:
                    print(part.get("text", ""), end="", flush=True)
                if result.get("lastChunk"):
                    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
