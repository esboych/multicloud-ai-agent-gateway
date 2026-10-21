"""Concurrent-user load generator for the gateway. Standard library + httpx.

    python scripts/loadtest.py --url http://localhost:8080 --users 10 --requests 50
    python scripts/loadtest.py --mix invoke=4,chat=2,stream=2,mcp=1,a2a=1 --json out.json

Each virtual user runs its requests sequentially; ``--users`` users run at
the same time. Operations:

  invoke   POST /v1/agents/<agent>:invoke
  chat     POST /v1/chat, every second call continues the user's conversation
  stream   POST /v1/agents/<agent>:stream, latency measured to the ``done`` event
  mcp      POST /mcp tools/call
  a2a      POST /a2a message/send

Prints per-operation p50 / p95 / p99 / max latency, throughput and errors;
exits 1 if any request failed. Pass ``--token`` (or GATEWAY_BEARER_TOKEN)
for a gateway running with GATEWAY_AUTH_MODE=jwt.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_MIX = "invoke=4,chat=2,stream=2,mcp=1,a2a=1"


@dataclass
class Sample:
    op: str
    latency_ms: float
    ok: bool
    status: int
    detail: str = ""


@dataclass
class UserState:
    index: int
    rng: random.Random
    conversation_id: str | None = None
    samples: list[Sample] = field(default_factory=list)


def parse_mix(text: str) -> list[tuple[str, int]]:
    mix: list[tuple[str, int]] = []
    for item in text.split(","):
        name, _, weight = item.strip().partition("=")
        if not name:
            continue
        mix.append((name, int(weight or 1)))
    if not mix:
        raise ValueError("empty mix")
    return mix


def pick(rng: random.Random, mix: list[tuple[str, int]]) -> str:
    total = sum(w for _, w in mix)
    roll = rng.uniform(0, total)
    for name, weight in mix:
        roll -= weight
        if roll <= 0:
            return name
    return mix[-1][0]


async def do_invoke(client: httpx.AsyncClient, agent: str, user: UserState, i: int) -> Sample:
    started = time.perf_counter()
    resp = await client.post(f"/v1/agents/{agent}:invoke", json={"prompt": f"user {user.index} request {i}"})
    ok = resp.status_code == 200 and bool(resp.json().get("output_text"))
    return Sample("invoke", _ms(started), ok, resp.status_code, "" if ok else resp.text[:200])


async def do_chat(client: httpx.AsyncClient, agent: str, user: UserState, i: int) -> Sample:
    body: dict[str, Any] = {"input": f"hello from user {user.index} turn {i}"}
    if user.conversation_id and i % 2 == 1:
        body["conversation_id"] = user.conversation_id
    started = time.perf_counter()
    resp = await client.post("/v1/chat", json=body)
    ok = resp.status_code == 200
    if ok:
        payload = resp.json()
        user.conversation_id = payload.get("conversation_id") or user.conversation_id
        ok = bool(payload.get("routing", {}).get("selected_agent"))
    return Sample("chat", _ms(started), ok, resp.status_code, "" if ok else resp.text[:200])


async def do_stream(client: httpx.AsyncClient, agent: str, user: UserState, i: int) -> Sample:
    started = time.perf_counter()
    saw_done = False
    status = 0
    async with client.stream("POST", f"/v1/agents/{agent}:stream", json={"prompt": f"stream {user.index}/{i}"}) as resp:
        status = resp.status_code
        async for line in resp.aiter_lines():
            if line.startswith("event: done"):
                saw_done = True
            elif line.startswith("event: error"):
                break
    ok = status == 200 and saw_done
    return Sample("stream", _ms(started), ok, status, "" if ok else "no done event")


async def do_mcp(client: httpx.AsyncClient, agent: str, user: UserState, i: int) -> Sample:
    body = {
        "jsonrpc": "2.0",
        "id": i,
        "method": "tools/call",
        "params": {"name": agent, "arguments": {"prompt": f"mcp {user.index}/{i}"}},
    }
    started = time.perf_counter()
    resp = await client.post("/mcp", json=body)
    ok = resp.status_code == 200
    if ok:
        payload = resp.json()
        ok = "result" in payload and not payload["result"].get("isError")
    return Sample("mcp", _ms(started), ok, resp.status_code, "" if ok else resp.text[:200])


async def do_a2a(client: httpx.AsyncClient, agent: str, user: UserState, i: int) -> Sample:
    body = {
        "jsonrpc": "2.0",
        "id": i,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": f"a2a {user.index}/{i}"}],
                "messageId": f"u{user.index}-{i}",
            },
            "metadata": {"agent": agent},
        },
    }
    started = time.perf_counter()
    resp = await client.post("/a2a", json=body)
    ok = resp.status_code == 200
    if ok:
        payload = resp.json()
        ok = payload.get("result", {}).get("status", {}).get("state") == "completed"
    return Sample("a2a", _ms(started), ok, resp.status_code, "" if ok else resp.text[:200])


OPS = {"invoke": do_invoke, "chat": do_chat, "stream": do_stream, "mcp": do_mcp, "a2a": do_a2a}


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


async def run_user(client: httpx.AsyncClient, agent: str, user: UserState, requests: int, mix: list[tuple[str, int]]) -> None:
    for i in range(requests):
        op = pick(user.rng, mix)
        try:
            sample = await OPS[op](client, agent, user, i)
        except Exception as exc:  # noqa: BLE001 - every failure is a data point
            sample = Sample(op, 0.0, False, 0, f"{type(exc).__name__}: {exc}"[:200])
        user.samples.append(sample)


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(pct / 100.0 * len(ordered)) - 1))
    return ordered[index]


def summarize(samples: list[Sample], wall_seconds: float, users: int) -> dict[str, Any]:
    by_op: dict[str, list[Sample]] = {}
    for sample in samples:
        by_op.setdefault(sample.op, []).append(sample)
    ops: dict[str, Any] = {}
    for op, items in sorted(by_op.items()):
        latencies = [s.latency_ms for s in items if s.ok]
        ops[op] = {
            "count": len(items),
            "errors": sum(1 for s in items if not s.ok),
            "p50_ms": round(percentile(latencies, 50), 1),
            "p95_ms": round(percentile(latencies, 95), 1),
            "p99_ms": round(percentile(latencies, 99), 1),
            "max_ms": round(max(latencies), 1) if latencies else 0.0,
            "mean_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
        }
    errors = [s for s in samples if not s.ok]
    return {
        "users": users,
        "requests": len(samples),
        "errors": len(errors),
        "wall_seconds": round(wall_seconds, 2),
        "requests_per_second": round(len(samples) / wall_seconds, 1) if wall_seconds else 0.0,
        "ops": ops,
        "error_samples": [{"op": e.op, "status": e.status, "detail": e.detail} for e in errors[:10]],
    }


def print_summary(summary: dict[str, Any]) -> None:
    print(
        f"users={summary['users']} requests={summary['requests']} errors={summary['errors']} "
        f"wall={summary['wall_seconds']}s throughput={summary['requests_per_second']} req/s"
    )
    print(f"{'op':<8}{'count':>7}{'errors':>8}{'p50':>9}{'p95':>9}{'p99':>9}{'max':>9}{'mean':>9}")
    for op, stats in summary["ops"].items():
        print(
            f"{op:<8}{stats['count']:>7}{stats['errors']:>8}{stats['p50_ms']:>9}{stats['p95_ms']:>9}"
            f"{stats['p99_ms']:>9}{stats['max_ms']:>9}{stats['mean_ms']:>9}"
        )
    for error in summary["error_samples"]:
        print(f"  error: {error['op']} status={error['status']} {error['detail']}")


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    mix = parse_mix(args.mix)
    headers = {}
    token = args.token or os.environ.get("GATEWAY_BEARER_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    limits = httpx.Limits(max_connections=args.users * 2, max_keepalive_connections=args.users * 2)
    users = [UserState(index=i, rng=random.Random(args.seed + i)) for i in range(args.users)]
    async with httpx.AsyncClient(base_url=args.url, headers=headers, timeout=args.timeout, limits=limits) as client:
        health = await client.get("/healthz")
        health.raise_for_status()
        started = time.perf_counter()
        await asyncio.gather(*(run_user(client, args.agent, user, args.requests, mix) for user in users))
        wall = time.perf_counter() - started
    samples = [s for user in users for s in user.samples]
    return summarize(samples, wall, args.users)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("GATEWAY_URL", "http://localhost:8080"))
    parser.add_argument("--users", type=int, default=10)
    parser.add_argument("--requests", type=int, default=50, help="requests per user")
    parser.add_argument("--mix", default=DEFAULT_MIX)
    parser.add_argument("--agent", default="DemoAgent")
    parser.add_argument("--token", default="")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--json", dest="json_path", default="", help="write the summary as JSON")
    args = parser.parse_args()

    summary = asyncio.run(main_async(args))
    print_summary(summary)
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2)
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
