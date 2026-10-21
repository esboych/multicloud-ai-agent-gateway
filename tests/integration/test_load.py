"""Concurrent users against a real gateway process.

Two gateways: one with the instant demo agent (gateway overhead only) and
one whose agent sleeps 100 ms per request, which is the case that proves
requests overlap instead of serialising on the event loop.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import REPO_ROOT, GatewayProcess

pytestmark = pytest.mark.load

USERS = 10
REQUESTS_PER_USER = 30
SLOW_DELAY_SECONDS = 0.1


def _run_load(base_url: str, out: Path, *, users: int, requests: int, mix: str | None = None) -> dict[str, Any]:
    cmd = [
        sys.executable, str(REPO_ROOT / "scripts" / "loadtest.py"),
        "--url", base_url, "--users", str(users), "--requests", str(requests), "--json", str(out),
    ]
    if mix:
        cmd += ["--mix", mix]
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=600)
    print(proc.stdout)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(out.read_text())


def test_ten_users_mixed_protocols_no_errors(gateway: GatewayProcess, tmp_path: Path) -> None:
    summary = _run_load(gateway.base_url, tmp_path / "fast.json", users=USERS, requests=REQUESTS_PER_USER)
    assert summary["errors"] == 0
    assert summary["requests"] == USERS * REQUESTS_PER_USER
    assert set(summary["ops"]) == {"invoke", "chat", "stream", "mcp", "a2a"}
    for op, stats in summary["ops"].items():
        assert stats["p95_ms"] < 2000, f"{op} p95 {stats['p95_ms']} ms"


def test_slow_backend_requests_overlap(slow_gateway: GatewayProcess, tmp_path: Path) -> None:
    summary = _run_load(
        slow_gateway.base_url, tmp_path / "slow.json", users=USERS, requests=REQUESTS_PER_USER,
        mix="invoke=3,chat=1,stream=1",
    )
    assert summary["errors"] == 0
    sequential = USERS * REQUESTS_PER_USER * SLOW_DELAY_SECONDS  # 30 s if nothing overlapped
    ideal = REQUESTS_PER_USER * SLOW_DELAY_SECONDS  # 3 s if everything overlapped
    assert summary["wall_seconds"] < sequential / 3, summary
    assert summary["wall_seconds"] >= ideal * 0.9, summary
    for op, stats in summary["ops"].items():
        # Each request waits ~100 ms in the agent; the gateway must not add much.
        assert stats["p50_ms"] < 400, f"{op} p50 {stats['p50_ms']} ms"
