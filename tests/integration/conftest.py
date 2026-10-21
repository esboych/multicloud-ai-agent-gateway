"""Black-box fixtures: real gateway processes on localhost, real HTTP.

No cloud. The gateway serves the ``demo_echo`` agent, exactly like the
README quickstart, and for the authenticated variant a tiny local OIDC
issuer (discovery document + JWKS) stands in for Entra / AKS so the
production JWKS path (`JwksKeyResolver`, `PyJWKClient`) is exercised over
the network instead of through an injected resolver.

Set ``GATEWAY_INTEGRATION_URL`` to run the anonymous tests against an
already running gateway (for example a port-forwarded cluster service);
the authenticated and load fixtures always start their own process.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

REPO_ROOT = Path(__file__).resolve().parents[2]

DEMO_AGENTS = [
    {
        "name": "DemoAgent",
        "backend_id": "direct",
        "direct_handler": "demo_echo",
        "description": "Echo agent for local runs.",
        "tags": ["demo", "echo", "hello"],
        "example_prompts": ["hello", "say hi", "echo this back"],
    },
    {
        "name": "SecondAgent",
        "backend_id": "direct",
        "direct_handler": "demo_echo",
        "description": "Second echo agent, used to exercise routing and conversation conflicts.",
        "tags": ["second", "backup", "mirror"],
        "example_prompts": ["mirror this", "use the second agent"],
    },
]

GATEWAY_AUDIENCE = "api://integration-gateway"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GATEWAY_", "AZURE_"))}
    env["PYTHONUNBUFFERED"] = "1"
    return env


@dataclass
class GatewayProcess:
    base_url: str
    process: subprocess.Popen[bytes] | None
    log_path: Path | None

    def logs(self) -> str:
        if self.log_path and self.log_path.exists():
            return self.log_path.read_text(errors="replace")
        return ""


def start_gateway(tmp_dir: Path, extra_env: dict[str, str], *, name: str) -> GatewayProcess:
    port = _free_port()
    env = _clean_env()
    env.update(
        {
            "GATEWAY_AGENTS_JSON": json.dumps(DEMO_AGENTS),
            "GATEWAY_HEALTH_PORT": str(port),
            "GATEWAY_PUBLIC_URL": f"http://127.0.0.1:{port}",
            "GATEWAY_LOG_LEVEL": "WARNING",
            "GATEWAY_CONVERSATION_STORE": "sqlite",
            "GATEWAY_CONVERSATION_STORE_PATH": str(tmp_dir / f"{name}-conversations.db"),
        }
    )
    env.update(extra_env)
    log_path = tmp_dir / f"{name}.log"
    log_file = log_path.open("wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "multicloud_ai_agent_gateway"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        if process.poll() is not None:
            log_file.close()
            raise RuntimeError(f"gateway exited early:\n{log_path.read_text(errors='replace')}")
        try:
            if httpx.get(base_url + "/readyz", timeout=1.0).status_code == 200:
                return GatewayProcess(base_url, process, log_path)
        except httpx.HTTPError:
            time.sleep(0.2)
    process.terminate()
    log_file.close()
    raise RuntimeError(f"gateway did not become ready:\n{log_path.read_text(errors='replace')}")


def stop_gateway(gateway: GatewayProcess) -> None:
    if gateway.process is None:
        return
    gateway.process.terminate()
    try:
        gateway.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        gateway.process.kill()
        gateway.process.wait(timeout=5)


@pytest.fixture(scope="session")
def integration_tmp(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("integration")


@pytest.fixture(scope="session")
def gateway(integration_tmp: Path) -> Iterator[GatewayProcess]:
    """Anonymous gateway (auth off), or an external one via GATEWAY_INTEGRATION_URL."""
    external = os.environ.get("GATEWAY_INTEGRATION_URL", "").strip()
    if external:
        yield GatewayProcess(external.rstrip("/"), None, None)
        return
    process = start_gateway(integration_tmp, {}, name="anonymous")
    try:
        yield process
    finally:
        stop_gateway(process)


@pytest.fixture(scope="session")
def slow_gateway(integration_tmp: Path) -> Iterator[GatewayProcess]:
    """Gateway whose demo agent sleeps 100 ms per request (load tests)."""
    process = start_gateway(
        integration_tmp, {"GATEWAY_DEMO_ECHO_DELAY_MS": "100"}, name="slow"
    )
    try:
        yield process
    finally:
        stop_gateway(process)


# -- local OIDC issuer -----------------------------------------------------


@dataclass
class LocalIssuer:
    issuer: str
    private_pem: bytes
    kid: str

    def token(self, **claims: Any) -> str:
        now = int(time.time())
        payload = {"iss": self.issuer, "aud": GATEWAY_AUDIENCE, "iat": now, "exp": now + 300}
        payload.update(claims)
        return jwt.encode(payload, self.private_pem, algorithm="RS256", headers={"kid": self.kid})


@pytest.fixture(scope="session")
def local_issuer() -> Iterator[LocalIssuer]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": "it-key-1", "use": "sig", "alg": "RS256"})
    documents: dict[str, dict[str, Any]] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            body = documents.get(self.path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    issuer = f"http://127.0.0.1:{server.server_address[1]}"
    documents["/.well-known/openid-configuration"] = {
        "issuer": issuer,
        "jwks_uri": issuer + "/jwks.json",
    }
    documents["/jwks.json"] = {"keys": [jwk]}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield LocalIssuer(issuer=issuer, private_pem=private_pem, kid="it-key-1")
    finally:
        server.shutdown()


@pytest.fixture(scope="session")
def auth_gateway(integration_tmp: Path, local_issuer: LocalIssuer) -> Iterator[GatewayProcess]:
    """Gateway with GATEWAY_AUTH_MODE=jwt trusting the local issuer."""
    issuers = [
        {
            "issuer": local_issuer.issuer,
            "audience": GATEWAY_AUDIENCE,
            "tenant_claim": "tid",
            "label": "integration",
        }
    ]
    policy = {"tenant-a": {"agents": ["DemoAgent"]}, "*": {"agents": []}}
    process = start_gateway(
        integration_tmp,
        {
            "GATEWAY_AUTH_MODE": "jwt",
            "GATEWAY_AUTH_ISSUERS_JSON": json.dumps(issuers),
            "GATEWAY_TENANT_POLICY_JSON": json.dumps(policy),
        },
        name="auth",
    )
    try:
        yield process
    finally:
        stop_gateway(process)


@pytest.fixture
def client(gateway: GatewayProcess) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=gateway.base_url, timeout=30.0) as client:
        yield client
