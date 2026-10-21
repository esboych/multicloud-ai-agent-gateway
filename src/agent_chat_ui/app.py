from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlparse


class ConfigError(ValueError):
    """Raised when required runtime configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    gateway_url: str
    request_timeout_seconds: int
    health_port: int
    # Honour X-Forwarded-For for login rate limiting. Off by default: the
    # header is client-controlled unless a proxy in front overwrites it.
    trust_proxy_headers: bool = False
    # Bearer token presented to the gateway when it runs with
    # GATEWAY_AUTH_MODE=jwt. A file path is preferred in Kubernetes: point it
    # at a projected ServiceAccount token (audience = the gateway) and the
    # UI authenticates as its own namespace; the file is re-read per request
    # because projected tokens rotate.
    gateway_bearer_token: str = ""
    gateway_bearer_token_file: str = ""


@dataclass(frozen=True)
class AuthSettings:
    enabled: bool
    username: str = ""
    password: str = ""
    session_secret: str = ""
    session_ttl_seconds: int = 28800
    max_login_attempts: int = 5
    login_window_seconds: int = 300
    lockout_seconds: int = 600
    cookie_secure: bool = False


_STATE = {
    "ready": False,
    "last_success_at": None,
    "last_error_at": None,
    "last_error": None,
    "last_duration_ms": None,
    "last_chat_at": None,
}
_STOP = threading.Event()
_SETTINGS: Settings | None = None
_AUTH: AuthSettings | None = None
_LOGIN_FAILURES: dict[str, list[float]] = {}
_LOGIN_LOCKOUTS: dict[str, float] = {}
_LOGIN_LOCK = threading.Lock()
AUTH_COOKIE_NAME = "agent_chat_ui_session"
AUTO_ROUTER_AGENT = "AutoRouter"
FOUNDRY_CHAT_AGENTS = frozenset({"AzureFinOpsAgent", "AzureMLTrainingAgent"})
AGENTCORE_CHAT_AGENTS = frozenset(
    {
        "AwsFinOpsSmartAgent",
        "AwsFinOpsHiddenCostAgent",
        "AwsSageMakerTrainingAgent",
        "AwsSageMakerTrainingSmartAgent",
    }
)
GCP_CHAT_AGENTS = frozenset({"GcpAgent"})
CHAT_AGENTS = FOUNDRY_CHAT_AGENTS | AGENTCORE_CHAT_AGENTS | GCP_CHAT_AGENTS
SUPPORTED_CHAT_AGENTS = frozenset({AUTO_ROUTER_AGENT, *CHAT_AGENTS})
ML_RESULT_FIELDS = ("job_name", "status", "studio_url", "compute", "workspace")
AUTO_ROUTER_ALIASES = {"auto", "autorouter", "router"}

# Routing classifier moved to the gateway in Phase 0. The chat-ui keeps only
# (a) the AutoRouter sentinel handling so a malformed request returns 400
# without a gateway round-trip, and (b) a flat list of supported agent names
# so unknown manual selections are also rejected locally.
#
# When auto mode is requested we forward the prompt to the gateway as
# agent="AutoRouter" (/v1/agents/AutoRouter:invoke, or AutoRouter:stream for
# the streaming path) and let the gateway pick a concrete agent. Streaming
# through AutoRouter, rather than resolving via /v1/route first, is what
# lets the gateway retry a runner-up agent ('routing-retry').


@dataclass(frozen=True)
class RouteDecision:
    requested_agent: str
    selected_agent: str
    mode: str
    reason: str
    scores: dict[str, float]
    method: str = "manual"
    confidence: float = 0.0
    candidates: list[dict[str, Any]] = field(default_factory=list)


def main() -> int:
    global _SETTINGS, _AUTH
    settings = load_settings(os.environ)
    auth = load_auth_settings(os.environ)
    _SETTINGS = settings
    _AUTH = auth
    install_signal_handlers()
    start_health_server(settings.health_port)

    log_event(
        "chat_ui_started",
        gateway_url=settings.gateway_url,
        auth_enabled=auth.enabled,
    )
    _STATE["ready"] = True

    _STOP.wait()
    log_event("chat_ui_stopped")
    return 0


def load_settings(env: dict[str, str]) -> Settings:
    gateway_url = (env.get("GATEWAY_URL") or env.get("GATEWAY_BASE_URL") or "").strip()
    if not gateway_url:
        raise ConfigError("Missing required environment variable: GATEWAY_URL")
    timeout_value = env.get("REQUEST_TIMEOUT_SECONDS") or env.get("GATEWAY_TIMEOUT_SECONDS")
    return Settings(
        gateway_url=gateway_url.rstrip("/"),
        request_timeout_seconds=_int_value(timeout_value, "REQUEST_TIMEOUT_SECONDS", 900, min_value=1),
        health_port=_int_env(env, "HEALTH_PORT", 8080, min_value=1),
        trust_proxy_headers=_bool_env(env.get("TRUST_PROXY_HEADERS"), default=False),
        gateway_bearer_token=(env.get("GATEWAY_BEARER_TOKEN") or "").strip(),
        gateway_bearer_token_file=(env.get("GATEWAY_BEARER_TOKEN_FILE") or "").strip(),
    )


def gateway_headers(settings: Settings, *, accept: str = "application/json") -> dict[str, str]:
    """Headers for every call to the gateway, including the bearer token if any."""
    headers = {"Content-Type": "application/json", "Accept": accept}
    token = settings.gateway_bearer_token
    if settings.gateway_bearer_token_file:
        try:
            with open(settings.gateway_bearer_token_file, "r", encoding="utf-8") as handle:
                token = handle.read().strip() or token
        except OSError as exc:
            log_event("gateway_token_file_unreadable", path=settings.gateway_bearer_token_file, error=str(exc))
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def load_auth_settings(env: dict[str, str]) -> AuthSettings:
    username = env.get("AUTH_USERNAME", "").strip()
    password = env.get("AUTH_PASSWORD", "")
    if not username and not password:
        return AuthSettings(enabled=False)
    if not username or not password:
        raise ConfigError("Set both AUTH_USERNAME and AUTH_PASSWORD, or neither to disable login.")
    if "|" in username:
        raise ConfigError("AUTH_USERNAME cannot contain '|'.")
    session_secret = env.get("AUTH_SESSION_SECRET", "").strip()
    if len(session_secret) < 32:
        raise ConfigError("Set AUTH_SESSION_SECRET to at least 32 characters.")
    return AuthSettings(
        enabled=True,
        username=username,
        password=password,
        session_secret=session_secret,
        session_ttl_seconds=_int_env(env, "AUTH_SESSION_TTL_SECONDS", 28800, min_value=60),
        max_login_attempts=_int_env(env, "AUTH_MAX_LOGIN_ATTEMPTS", 5, min_value=1),
        login_window_seconds=_int_env(env, "AUTH_LOGIN_WINDOW_SECONDS", 300, min_value=1),
        lockout_seconds=_int_env(env, "AUTH_LOCKOUT_SECONDS", 600, min_value=1),
        cookie_secure=_bool_env(env.get("AUTH_COOKIE_SECURE"), default=False),
    )


def client_identity(
    forwarded_for: str,
    client_address: Any,
    *,
    trust_proxy_headers: bool,
) -> str:
    """Key used for login rate limiting.

    ``X-Forwarded-For`` is whatever the client sent unless a proxy in front
    overwrites it, so it is only honoured when the operator opted in with
    ``TRUST_PROXY_HEADERS=true``. Otherwise the TCP peer address is used.
    """
    if trust_proxy_headers and forwarded_for:
        first = forwarded_for.split(",", 1)[0].strip()
        if first:
            return first
    if client_address:
        return str(client_address[0])
    return "unknown"


def stream_target_agent(route: RouteDecision) -> str:
    """Agent name to open the gateway stream on.

    Auto mode streams through ``AutoRouter`` so the gateway keeps its BM25
    candidate list and can retry a runner-up (``routing-retry``); manual
    mode streams the chosen agent directly.
    """
    return AUTO_ROUTER_AGENT if route.mode == "auto" else route.selected_agent


def route_chat_agent(requested_agent: str | None) -> RouteDecision:
    """Validate the agent name locally; defer auto-classification to the gateway.

    Returns a ``RouteDecision`` whose ``selected_agent`` is the canonical
    ``AUTO_ROUTER_AGENT`` sentinel for auto mode (the gateway resolves it on
    invoke) and the requested agent name itself for manual mode.

    Raises ``ValueError`` for unknown manual selections so the chat handler
    can return a 400 without a wasted gateway round-trip.
    """
    normalized = (requested_agent or AUTO_ROUTER_AGENT).strip() or AUTO_ROUTER_AGENT
    if normalized.lower() in AUTO_ROUTER_ALIASES:
        return RouteDecision(
            requested_agent=AUTO_ROUTER_AGENT,
            selected_agent=AUTO_ROUTER_AGENT,
            mode="auto",
            reason="auto route via gateway",
            scores={},
            method="auto",
            confidence=0.0,
            candidates=[],
        )
    if normalized not in CHAT_AGENTS:
        raise ValueError(f"Unsupported chat agent: {normalized}")
    return RouteDecision(
        requested_agent=normalized,
        selected_agent=normalized,
        mode="manual",
        reason="manual selection",
        scores={},
        method="manual",
        confidence=1.0,
        candidates=[{"agent": normalized, "score": 1.0}],
    )


def invoke_agent_via_gateway(settings: Settings, agent_name: str, prompt: str) -> dict[str, Any]:
    """POST to gateway's /v1/agents/<name>:invoke and normalize the response.

    Output shape matches what a direct Foundry SDK caller would return, plus a ``routing`` field that is non-null when the gateway
    resolved the AutoRouter sentinel into a concrete agent.
    """
    url = f"{settings.gateway_url}/v1/agents/{quote(agent_name)}:invoke"
    body = json.dumps({"prompt": prompt}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers=gateway_headers(settings),
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.request_timeout_seconds) as response:
            payload_bytes = response.read()
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read().decode("utf-8"))
            detail = err.get("detail") or err
        except Exception:
            detail = exc.reason
        raise RuntimeError(f"Gateway returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gateway unreachable at {url}: {exc.reason}") from exc

    payload = json.loads(payload_bytes.decode("utf-8"))
    return {
        "id": payload.get("conversation_id"),
        "output_text": payload.get("output_text") or "",
        "tool_events": payload.get("tool_events") or [],
        "usage": payload.get("usage"),
        "finish_reason": payload.get("finish_reason"),
        "routing": payload.get("routing"),
    }


def route_via_gateway(settings: Settings, prompt: str) -> dict[str, Any]:
    """POST to gateway's /v1/route and return its routing decision.

    Used by the streaming path so the chat-ui can emit a 'meta' event with
    the resolved agent before opening the SSE connection.
    """
    url = f"{settings.gateway_url}/v1/route"
    body = json.dumps({"prompt": prompt}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers=gateway_headers(settings),
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.request_timeout_seconds) as response:
            payload_bytes = response.read()
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read().decode("utf-8"))
            detail = err.get("detail") or err
        except Exception:
            detail = exc.reason
        raise RuntimeError(f"Gateway returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gateway unreachable at {url}: {exc.reason}") from exc

    payload = json.loads(payload_bytes.decode("utf-8"))
    routing = payload.get("routing") or {}
    return {
        "selected_agent": routing.get("selected_agent") or "",
        "requested_agent": routing.get("requested_agent") or AUTO_ROUTER_AGENT,
        "mode": routing.get("mode") or "auto",
        "reason": routing.get("reason") or "",
        "scores": routing.get("scores") or {},
        "method": routing.get("method") or "unknown",
        "confidence": routing.get("confidence") or 0.0,
        "candidates": routing.get("candidates") or [],
    }


def _router_block_from_route(route: RouteDecision) -> dict[str, Any]:
    """Build a router-block dict from the local RouteDecision (manual mode shape)."""
    return {
        "mode": route.mode,
        "requested_agent": route.requested_agent,
        "selected_agent": route.selected_agent,
        "reason": route.reason,
        "scores": dict(route.scores),
        "method": route.method,
        "confidence": route.confidence,
        "candidates": list(route.candidates),
    }


def _router_block_from_result(route: RouteDecision, result: dict[str, Any]) -> dict[str, Any]:
    """Prefer the gateway's routing decision when it resolved AutoRouter; otherwise local."""
    routing = result.get("routing")
    if routing:
        return {
            "mode": routing.get("mode") or route.mode,
            "requested_agent": routing.get("requested_agent") or route.requested_agent,
            "selected_agent": routing.get("selected_agent") or route.selected_agent,
            "reason": routing.get("reason") or route.reason,
            "scores": dict(routing.get("scores") or {}),
            "method": routing.get("method") or route.method,
            "confidence": routing.get("confidence") if routing.get("confidence") is not None else route.confidence,
            "candidates": list(routing.get("candidates") or []),
        }
    return _router_block_from_route(route)


def summarize_foundry_result(result: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "response_id": result.get("id"),
        "tool_event_count": len(result.get("tool_events") or []),
        "output_preview": str(result.get("output_text") or "")[:1000],
    }
    for event in result.get("tool_events") or []:
        tool_result = event.get("result")
        if not isinstance(tool_result, dict):
            continue
        summary["last_tool"] = event.get("name")
        summary["findings_count"] = len(tool_result.get("findings") or [])
        summary["recommendations_count"] = len(tool_result.get("recommendations") or [])
        summary["cost_group_count"] = len(tool_result.get("cost_groups") or [])
        jobs = tool_result.get("jobs")
        if isinstance(jobs, list):
            summary["job_count"] = len(jobs)
            if jobs and isinstance(jobs[0], dict):
                for field in ML_RESULT_FIELDS:
                    if jobs[0].get(field) is not None:
                        summary[field] = jobs[0].get(field)
        for field in ML_RESULT_FIELDS:
            if tool_result.get(field) is not None:
                summary[field] = tool_result.get(field)
        config = tool_result.get("config")
        if isinstance(config, dict):
            summary["window"] = config.get("window")
            summary["subscriptions"] = config.get("subscription_ids")
    return summary


def start_health_server(port: int) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log_event("health_server_started", port=port)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            self._json(200, {"ok": True})
            return
        if path == "/readyz":
            status = 200 if _STATE["ready"] else 503
            self._json(status, {"ready": _STATE["ready"], "state": _STATE})
            return
        if path == "/login":
            if self._is_authenticated():
                self._redirect("/")
                return
            self._html(200, render_login_page())
            return
        if path == "/logout":
            self._redirect("/login", headers={"Set-Cookie": clear_session_cookie()})
            return
        if path == "/":
            if not self._require_auth(json_response=False):
                return
            self._html(200, CHAT_HTML)
            return
        if path == "/api/status":
            if not self._require_auth(json_response=True):
                return
            self._json(200, {"ready": _STATE["ready"], "state": _STATE})
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/login":
            self._handle_login()
            return
        if path == "/api/chat-stream":
            if not self._require_auth(json_response=True):
                return
            self._handle_chat_stream()
            return
        if path != "/api/chat":
            self._json(404, {"error": "not_found"})
            return
        if not self._require_auth(json_response=True):
            return

        try:
            payload = self._read_json_body(max_bytes=32768)
            message = str(payload.get("message") or "").strip()
            if not message:
                self._json(400, {"error": "message_required"})
                return
            if _SETTINGS is None:
                self._json(503, {"error": "not_ready"})
                return
            requested_agent = str(payload.get("agent") or AUTO_ROUTER_AGENT).strip() or AUTO_ROUTER_AGENT
            try:
                route = route_chat_agent(requested_agent)
            except ValueError as exc:
                self._json(400, {"error": "unsupported_agent", "message": str(exc), "supported_agents": sorted(SUPPORTED_CHAT_AGENTS)})
                return

            # In auto mode we send 'AutoRouter' to the gateway and let it
            # resolve the concrete agent. In manual mode we pass through.
            started = time.monotonic()
            result = invoke_agent_via_gateway(_SETTINGS, route.selected_agent, message)
            duration_ms = int((time.monotonic() - started) * 1000)
            _STATE["last_chat_at"] = now_iso()

            router_info = _router_block_from_result(route, result)
            final_agent = router_info["selected_agent"] or route.selected_agent

            self._json(
                200,
                {
                    "agent": final_agent,
                    "answer": result.get("output_text") or "",
                    "duration_ms": duration_ms,
                    "router": router_info,
                    "summary": summarize_foundry_result(result),
                    "tool_events": _compact_tool_events(result.get("tool_events") or []),
                },
            )
        except Exception as exc:
            log_event("chat_failed", error=str(exc), error_type=type(exc).__name__)
            self._json(500, {"error": type(exc).__name__, "message": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _handle_chat_stream(self) -> None:
        """Proxy gateway's :stream SSE response straight through to the browser.

        Validates auth + AutoRouter on this side (so the same login flow works
        for streaming). Emits any pre-stream errors as a single SSE error
        event so the browser-side UI can render them in the same code path.
        """
        try:
            payload = self._read_json_body(max_bytes=32768)
            message = str(payload.get("message") or "").strip()
            if not message:
                self._json(400, {"error": "message_required"})
                return
            if _SETTINGS is None:
                self._json(503, {"error": "not_ready"})
                return
            requested_agent = str(payload.get("agent") or AUTO_ROUTER_AGENT).strip() or AUTO_ROUTER_AGENT
            try:
                route = route_chat_agent(requested_agent)
            except ValueError as exc:
                self._json(
                    400,
                    {
                        "error": "unsupported_agent",
                        "message": str(exc),
                        "supported_agents": sorted(SUPPORTED_CHAT_AGENTS),
                    },
                )
                return
        except Exception as exc:
            log_event("chat_stream_failed", error=str(exc), error_type=type(exc).__name__)
            self._json(500, {"error": type(exc).__name__, "message": str(exc)})
            return

        # Auto mode streams through the gateway's AutoRouter so the gateway
        # keeps its BM25 candidate list and can retry a runner-up agent
        # ('routing-retry'). Its first SSE event ('routing') carries the
        # selected agent and scores; the browser renders it like 'meta'.
        # Resolving via /v1/route first and then streaming a concrete agent
        # would throw that candidate list away.
        target_agent = stream_target_agent(route)

        # Headers go out before we know if the agent stream is reachable. If
        # it turns out unreachable we still need to write a valid SSE error
        # event into the already-opened response.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        # For a manual selection the browser needs the routing context up
        # front; in auto mode the gateway's 'routing' event provides it.
        if route.mode != "auto":
            self._sse_write(
                "meta", {"agent": target_agent, "router": _router_block_from_route(route)}
            )

        url = f"{_SETTINGS.gateway_url}/v1/agents/{quote(target_agent)}:stream"
        body = json.dumps({"prompt": message}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers=gateway_headers(_SETTINGS, accept="text/event-stream"),
        )

        try:
            with urllib.request.urlopen(
                request, timeout=_SETTINGS.request_timeout_seconds
            ) as response:
                while True:
                    chunk = response.read(1024)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
            _STATE["last_chat_at"] = now_iso()
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read().decode("utf-8"))
                detail = err.get("detail") or err
            except Exception:
                detail = exc.reason
            self._sse_write(
                "error",
                {"message": f"Gateway HTTP {exc.code}: {detail}", "error_type": "GatewayError"},
            )
        except urllib.error.URLError as exc:
            self._sse_write(
                "error",
                {"message": f"Gateway unreachable: {exc.reason}", "error_type": "GatewayUnreachable"},
            )
        except Exception as exc:  # noqa: BLE001 - last-ditch envelope
            self._sse_write(
                "error",
                {"message": str(exc), "error_type": type(exc).__name__},
            )

    def _sse_write(self, event: str, payload: dict[str, Any]) -> None:
        try:
            line = f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_login(self) -> None:
        auth = current_auth_settings()
        if not auth.enabled:
            self._redirect("/")
            return

        client_id = self._client_id()
        retry_after = login_retry_after(client_id, auth)
        if retry_after > 0:
            self._html(
                429,
                render_login_page(f"Too many failed login attempts. Try again in {retry_after} seconds."),
                headers={"Retry-After": str(retry_after)},
            )
            return

        try:
            form = self._read_form_body(max_bytes=8192)
        except ValueError as exc:
            self._html(400, render_login_page(str(exc)))
            return

        username = str(form.get("username") or "")
        password = str(form.get("password") or "")
        if hmac.compare_digest(username, auth.username) and hmac.compare_digest(password, auth.password):
            clear_login_failures(client_id)
            self._redirect("/", headers={"Set-Cookie": make_session_cookie(auth)})
            log_event("login_succeeded", client_id=client_id)
            return

        retry_after = record_login_failure(client_id, auth)
        status = 429 if retry_after > 0 else 401
        headers = {"Retry-After": str(retry_after)} if retry_after > 0 else None
        message = (
            f"Too many failed login attempts. Try again in {retry_after} seconds."
            if retry_after > 0
            else "Invalid username or password."
        )
        self._html(status, render_login_page(message), headers=headers)
        log_event("login_failed", client_id=client_id, locked=retry_after > 0)

    def _require_auth(self, *, json_response: bool) -> bool:
        auth = current_auth_settings()
        if not auth.enabled or self._is_authenticated():
            return True
        if json_response:
            self._json(401, {"error": "unauthorized", "message": "Login required."})
        else:
            self._redirect("/login")
        return False

    def _is_authenticated(self) -> bool:
        auth = current_auth_settings()
        if not auth.enabled:
            return True
        raw_cookie = self.headers.get("Cookie", "")
        if not raw_cookie:
            return False
        cookies = SimpleCookie()
        try:
            cookies.load(raw_cookie)
        except Exception:
            return False
        morsel = cookies.get(AUTH_COOKIE_NAME)
        return bool(morsel and validate_session_token(morsel.value, auth))

    def _client_id(self) -> str:
        return client_identity(
            self.headers.get("X-Forwarded-For", ""),
            self.client_address,
            trust_proxy_headers=bool(_SETTINGS and _SETTINGS.trust_proxy_headers),
        )

    def _read_json_body(self, *, max_bytes: int) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length") or "0")
        if content_length > max_bytes:
            raise ValueError(f"Request body is too large; limit is {max_bytes} bytes.")
        raw = self.rfile.read(content_length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Request body must be JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def _read_form_body(self, *, max_bytes: int) -> dict[str, str]:
        content_length = int(self.headers.get("Content-Length") or "0")
        if content_length > max_bytes:
            raise ValueError(f"Request body is too large; limit is {max_bytes} bytes.")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type and content_type != "application/x-www-form-urlencoded":
            raise ValueError("Login form must use application/x-www-form-urlencoded.")
        raw = self.rfile.read(content_length).decode("utf-8")
        parsed = parse_qs(raw, keep_blank_values=True)
        return {key: values[-1] if values else "" for key, values in parsed.items()}

    def _redirect(self, location: str, *, headers: dict[str, str] | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _json(self, status: int, payload: dict[str, Any], *, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, status: int, html: str, *, headers: dict[str, str] | None = None) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def current_auth_settings() -> AuthSettings:
    return _AUTH or AuthSettings(enabled=False)


def make_session_cookie(auth: AuthSettings, *, now: float | None = None) -> str:
    token = create_session_token(auth, now=now)
    parts = [
        f"{AUTH_COOKIE_NAME}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={auth.session_ttl_seconds}",
    ]
    if auth.cookie_secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_session_cookie() -> str:
    return f"{AUTH_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


def create_session_token(auth: AuthSettings, *, now: float | None = None) -> str:
    expires_at = int((now if now is not None else time.time()) + auth.session_ttl_seconds)
    nonce = secrets.token_urlsafe(16)
    payload = f"{auth.username}|{expires_at}|{nonce}"
    signature = hmac.new(auth.session_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return b64url_encode(f"{payload}|{signature}".encode("utf-8"))


def validate_session_token(token: str, auth: AuthSettings, *, now: float | None = None) -> bool:
    try:
        decoded = b64url_decode(token).decode("utf-8")
        username, expires_raw, nonce, signature = decoded.split("|", 3)
        expires_at = int(expires_raw)
    except Exception:
        return False

    if username != auth.username or expires_at < int(now if now is not None else time.time()):
        return False

    payload = f"{username}|{expires_at}|{nonce}"
    expected = hmac.new(auth.session_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def login_retry_after(client_id: str, auth: AuthSettings, *, now: float | None = None) -> int:
    current = now if now is not None else time.time()
    with _LOGIN_LOCK:
        locked_until = _LOGIN_LOCKOUTS.get(client_id)
        if not locked_until:
            return 0
        if locked_until <= current:
            _LOGIN_LOCKOUTS.pop(client_id, None)
            _LOGIN_FAILURES.pop(client_id, None)
            return 0
        return max(1, int(locked_until - current))


def record_login_failure(client_id: str, auth: AuthSettings, *, now: float | None = None) -> int:
    current = now if now is not None else time.time()
    with _LOGIN_LOCK:
        failures = [
            timestamp
            for timestamp in _LOGIN_FAILURES.get(client_id, [])
            if current - timestamp <= auth.login_window_seconds
        ]
        failures.append(current)
        if len(failures) >= auth.max_login_attempts:
            locked_until = current + auth.lockout_seconds
            _LOGIN_LOCKOUTS[client_id] = locked_until
            _LOGIN_FAILURES[client_id] = []
            return auth.lockout_seconds
        _LOGIN_FAILURES[client_id] = failures
        return 0


def clear_login_failures(client_id: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_FAILURES.pop(client_id, None)
        _LOGIN_LOCKOUTS.pop(client_id, None)


def render_login_page(error_message: str = "") -> str:
    error = ""
    if error_message:
        error = f'<div class="error">{html.escape(error_message)}</div>'
    return LOGIN_HTML.replace("{{error}}", error)


def _compact_tool_events(tool_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted = []
    for event in tool_events:
        result = event.get("result")
        compacted.append(
            {
                "name": event.get("name"),
                "arguments": event.get("arguments"),
                "result_summary": summarize_tool_result(result) if isinstance(result, dict) else str(result)[:1000],
            }
        )
    return compacted


def summarize_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "findings": result.get("findings") or [],
        "recommendations": result.get("recommendations") or [],
        "cost_group_count": len(result.get("cost_groups") or []),
        "resource_count": len(result.get("resources") or []),
        "storage_account_count": len(result.get("storage_accounts") or []),
    }
    jobs = result.get("jobs")
    if isinstance(jobs, list):
        summary["job_count"] = len(jobs)
        summary["jobs"] = [
            {field: job.get(field) for field in ML_RESULT_FIELDS if isinstance(job, dict) and job.get(field) is not None}
            for job in jobs[:5]
        ]
    for key in ML_RESULT_FIELDS:
        if result.get(key) is not None:
            summary[key] = result.get(key)
    config = result.get("config")
    if isinstance(config, dict):
        summary["window"] = config.get("window")
        summary["subscription_ids"] = config.get("subscription_ids")
    return summary


def install_signal_handlers() -> None:
    def _handler(signum: int, _frame: Any) -> None:
        log_event("signal_received", signal=signum)
        _STOP.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def log_event(event: str, **fields: Any) -> None:
    print(json.dumps({"ts": now_iso(), "event": event, **fields}, sort_keys=True, default=str), flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required_env(env: dict[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"Set {name}.")
    return value


def _int_env(env: dict[str, str], name: str, default: int, *, min_value: int) -> int:
    return _int_value(env.get(name), name, default, min_value=min_value)


def _int_value(raw: str | None, name: str, default: int, *, min_value: int) -> int:
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer.") from exc
    if value < min_value:
        raise ConfigError(f"{name} must be >= {min_value}.")
    return value


def _bool_env(value: str | None, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


LOGIN_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Multi-Cloud Agent Chat — Sign in</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f8fb;
      --panel: #ffffff;
      --border: #d9e2ec;
      --text: #182230;
      --muted: #5c6b7a;
      --accent: #0f766e;
      --blue: #2563eb;
      --danger: #b42318;
      --shadow: 0 20px 50px rgba(24, 34, 48, 0.12);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    .login {
      width: min(420px, 100%);
      padding: 28px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 26px;
      font-weight: 750;
    }
    .mark {
      width: 40px;
      height: 40px;
      display: grid;
      place-items: center;
      border-radius: 8px;
      background: var(--accent);
      color: white;
      font-weight: 800;
    }
    h1 {
      margin: 0 0 4px;
      font-size: 22px;
      letter-spacing: 0;
    }
    p {
      margin: 0;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.45;
    }
    form {
      display: grid;
      gap: 14px;
      margin-top: 24px;
    }
    label {
      display: grid;
      gap: 7px;
      font-size: 13px;
      font-weight: 700;
      color: var(--muted);
    }
    input {
      width: 100%;
      height: 44px;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0 12px;
      color: var(--text);
      font: inherit;
      outline: none;
      background: white;
    }
    input:focus {
      border-color: var(--blue);
      box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.14);
    }
    button {
      height: 44px;
      border: 0;
      border-radius: 8px;
      background: var(--accent);
      color: white;
      font: inherit;
      font-weight: 750;
      cursor: pointer;
    }
    .error {
      margin-top: 18px;
      padding: 11px 12px;
      border: 1px solid rgba(180, 35, 24, 0.28);
      border-radius: 8px;
      color: var(--danger);
      background: #fff4f2;
      font-size: 13px;
      line-height: 1.4;
    }
  </style>
</head>
<body>
  <section class="login">
    <div class="brand"><div class="mark">AG</div><div>Multi-Cloud Agents</div></div>
    <h1>Sign in</h1>
    <p>This UI talks to Azure Foundry and AWS Bedrock AgentCore through the agent gateway. Access is protected with a temporary username and password.</p>
    {{error}}
    <form method="post" action="/login">
      <label>
        Username
        <input name="username" autocomplete="username" required autofocus>
      </label>
      <label>
        Password
        <input name="password" type="password" autocomplete="current-password" required>
      </label>
      <button type="submit">Sign in</button>
    </form>
  </section>
</body>
</html>
"""


CHAT_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Multi-Cloud Agent Chat (via Gateway)</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f8fb;
      --panel: #ffffff;
      --border: #d9e2ec;
      --text: #182230;
      --muted: #5c6b7a;
      --accent: #0f766e;
      --accent-strong: #115e59;
      --blue: #2563eb;
      --danger: #b42318;
      --shadow: 0 20px 50px rgba(24, 34, 48, 0.10);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    .app {
      min-height: 100vh;
      display: grid;
      grid-template-columns: minmax(240px, 300px) minmax(0, 1fr);
    }
    aside {
      border-right: 1px solid var(--border);
      background: #eef3f7;
      padding: 22px;
      display: flex;
      flex-direction: column;
      gap: 18px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      min-height: 40px;
      font-weight: 700;
    }
    .mark {
      width: 38px;
      height: 38px;
      display: grid;
      place-items: center;
      border-radius: 8px;
      background: var(--accent);
      color: white;
      font-weight: 800;
    }
    .status {
      display: grid;
      gap: 10px;
      font-size: 13px;
      color: var(--muted);
    }
    .status-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      border-bottom: 1px solid rgba(92, 107, 122, 0.18);
      padding-bottom: 9px;
    }
    .pill {
      color: var(--accent-strong);
      background: rgba(15, 118, 110, 0.10);
      padding: 4px 8px;
      border-radius: 999px;
      white-space: nowrap;
      font-weight: 650;
    }
    .header-actions {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .logout {
      min-height: 32px;
      padding: 6px 10px;
      border: 1px solid var(--border);
      border-radius: 8px;
      color: var(--text);
      background: #ffffff;
      font-size: 13px;
      font-weight: 650;
      line-height: 1;
      text-decoration: none;
    }
    main {
      min-width: 0;
      display: grid;
      grid-template-rows: auto 1fr auto;
      height: 100vh;
    }
    header {
      min-height: 72px;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
      padding: 18px 26px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
    }
    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 750;
      letter-spacing: 0;
    }
    .subtitle {
      margin-top: 4px;
      color: var(--muted);
      font-size: 13px;
    }
    .messages {
      overflow: auto;
      padding: 24px 26px;
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    .message {
      max-width: min(920px, 100%);
      padding: 14px 16px;
      border: 1px solid var(--border);
      background: var(--panel);
      border-radius: 8px;
      box-shadow: 0 1px 2px rgba(24, 34, 48, 0.04);
      white-space: pre-wrap;
      line-height: 1.48;
      overflow-wrap: anywhere;
    }
    .message.user {
      align-self: flex-end;
      background: #e8f5f3;
      border-color: rgba(15, 118, 110, 0.25);
    }
        .message.error {
          border-color: rgba(180, 35, 24, 0.35);
          color: var(--danger);
          background: #fff4f2;
        }
        .message.loading {
          min-width: min(460px, 100%);
          border-color: rgba(15, 118, 110, 0.25);
          background: linear-gradient(180deg, #ffffff 0%, #f8fcfb 100%);
        }
        .progress-head {
          display: flex;
          align-items: center;
          gap: 10px;
          font-weight: 750;
        }
        .progress-head [data-stage] {
          min-width: 0;
          overflow-wrap: anywhere;
        }
        .spinner {
          width: 18px;
          height: 18px;
          border: 3px solid rgba(15, 118, 110, 0.18);
          border-top-color: var(--accent);
          border-radius: 999px;
          animation: spin 0.8s linear infinite;
          flex: 0 0 auto;
        }
        .progress-elapsed {
          margin-left: auto;
          color: var(--muted);
          font-size: 12px;
          font-variant-numeric: tabular-nums;
          font-weight: 650;
        }
        .router-badge {
          display: inline-flex;
          align-items: center;
          min-height: 22px;
          padding: 2px 8px;
          border: 1px solid rgba(37, 99, 235, 0.2);
          border-radius: 999px;
          background: #eff6ff;
          color: #1d4ed8;
          font-size: 12px;
          font-weight: 700;
          white-space: nowrap;
        }
        .progress-track {
          height: 7px;
          margin: 12px 0 8px;
          overflow: hidden;
          border-radius: 999px;
          background: rgba(92, 107, 122, 0.14);
        }
        .progress-fill {
          position: relative;
          height: 100%;
          width: 10%;
          overflow: hidden;
          border-radius: inherit;
          background: linear-gradient(90deg, var(--accent), var(--blue));
          transition: width 0.45s ease;
        }
        .progress-fill::after {
          position: absolute;
          inset: 0;
          content: "";
          background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.55), transparent);
          animation: shimmer 1.3s ease-in-out infinite;
        }
        .progress-note {
          color: var(--muted);
          font-size: 13px;
        }
        .meta {
          margin-top: 10px;
          color: var(--muted);
          font-size: 12px;
        }
    form {
      border-top: 1px solid var(--border);
      background: var(--panel);
      padding: 16px 26px 20px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: end;
    }
    textarea {
      width: 100%;
      min-height: 58px;
      max-height: 180px;
      resize: vertical;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 13px 14px;
      color: var(--text);
      font: inherit;
      line-height: 1.4;
      outline: none;
    }
    textarea:focus {
      border-color: var(--blue);
      box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.14);
    }
    button {
      height: 44px;
      min-width: 96px;
      border: 0;
      border-radius: 8px;
      background: var(--accent);
      color: white;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }
    button:disabled {
      cursor: wait;
      opacity: 0.68;
    }
    .quick {
      display: grid;
      gap: 14px;
    }
    .quick-group {
      display: grid;
      gap: 8px;
    }
    .label,
    .quick-title {
      color: var(--muted);
      font-size: 12px;
      font-weight: 750;
      letter-spacing: 0;
      text-transform: uppercase;
    }
    select {
      width: 100%;
      height: 40px;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0 10px;
      color: var(--text);
      background: #ffffff;
      font: inherit;
      font-size: 13px;
      outline: none;
    }
    select:focus {
      border-color: var(--blue);
      box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.14);
    }
        .quick button {
          min-width: 0;
          width: 100%;
      height: auto;
      min-height: 38px;
      padding: 9px 10px;
      text-align: left;
      background: #ffffff;
      color: var(--text);
      border: 1px solid var(--border);
          font-size: 13px;
          font-weight: 600;
        }
        @keyframes spin {
          to { transform: rotate(360deg); }
        }
        @keyframes shimmer {
          from { transform: translateX(-100%); }
          to { transform: translateX(100%); }
        }
    @media (max-width: 820px) {
      .app { grid-template-columns: 1fr; }
      aside { display: none; }
      main { height: 100vh; }
      header, .messages, form { padding-left: 16px; padding-right: 16px; }
      form { grid-template-columns: 1fr; }
      button { width: 100%; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <div class="brand"><div class="mark">AG</div><div>Multi-Cloud Agents</div></div>
      <div class="status">
        <div class="status-row"><span>Runtime</span><span class="pill" id="ready">checking</span></div>
        <div class="status-row"><span>Last chat</span><span id="lastChat">none</span></div>
          </div>
          <div class="quick">
            <div class="quick-group">
              <div class="quick-title">Costs</div>
              <button type="button" data-prompt="Show current month Azure costs and top cost drivers.">Current Azure costs</button>
              <button type="button" data-prompt="Show current month GCP costs and top cost drivers.">Current GCP costs</button>
              <button type="button" data-prompt="What changed in Azure spend and which resources should I inspect first?">Azure spend triage</button>
            </div>
            <div class="quick-group">
              <div class="quick-title">Inventory and jobs</div>
              <button type="button" data-prompt="List recent Azure ML training jobs and summarize their statuses.">Azure ML jobs</button>
              <button type="button" data-prompt="List GCP resources and summarize them by type.">GCP resources</button>
            </div>
          </div>
    </aside>
    <main>
      <header>
        <div>
          <h1>Multi-Cloud Agent Chat</h1>
          <div class="subtitle">Routes through the agent gateway to Microsoft Foundry (Azure) and deterministic Google Cloud tools</div>
        </div>
        <div class="header-actions">
          <span class="pill" id="busy">idle</span>
          <a class="logout" href="/logout">Logout</a>
        </div>
      </header>
          <section class="messages" id="messages" aria-live="polite">
            <div class="message">Ask anything. The gateway's hybrid router (BM25 over agent metadata, plus an optional LLM tie-breaker) automatically picks the right agent across Microsoft Foundry (Azure) and Google Cloud. The footer of each reply shows which classifier was used and how confident it was.</div>
          </section>
      <form id="chatForm">
        <textarea id="message" name="message" placeholder="Ask about Azure or GCP..." required></textarea>
        <button id="send" type="submit">Send</button>
      </form>
    </main>
  </div>
  <script>
    const messages = document.getElementById('messages');
    const form = document.getElementById('chatForm');
    const textarea = document.getElementById('message');
    const send = document.getElementById('send');
    const busy = document.getElementById('busy');
    // Manual agent selection was removed -- the gateway's hybrid router
    // (BM25 + optional LLM) picks the agent from the prompt + metadata.
    // Quick-prompt buttons can still force an agent by passing it to
    // sendPrompt(prompt, agentName), but there is no UI dropdown.
    const AUTO_AGENT = 'AutoRouter';

        function addMessage(text, className, meta) {
          const el = document.createElement('div');
          el.className = `message ${className || ''}`.trim();
          renderMessage(el, text, meta);
          messages.appendChild(el);
          messages.scrollTop = messages.scrollHeight;
          return el;
        }

        function renderMessage(el, text, meta) {
          el.textContent = text;
          if (meta) {
            const metaEl = document.createElement('div');
            metaEl.className = 'meta';
            metaEl.textContent = meta;
            el.appendChild(metaEl);
          }
        }

        function createStreamBubble(selectedAgent) {
          // Initial stage text is honest about where the routing is happening:
          // for AutoRouter we have no agent name yet (the gateway will tell us
          // via the meta SSE event); for a manual override we can show the
          // explicit name immediately.
          const initialStage = selectedAgent === 'AutoRouter'
            ? 'Routing\u2026'
            : `Calling ${selectedAgent}`;
          // resolvedAgent is the concrete agent that received the prompt. Set
          // immediately for manual selections; populated from the gateway's
          // 'meta' SSE event for AutoRouter. Captured in the closure so the
          // done/error handlers do not lose it when they overwrite the stage.
          let resolvedAgent = selectedAgent === 'AutoRouter' ? null : selectedAgent;
          const el = document.createElement('div');
          el.className = 'message loading';
          el.innerHTML = `
            <div class="progress-head">
              <span class="spinner" aria-hidden="true" data-spinner></span>
              <span data-stage>${initialStage}</span>
              <span class="router-badge" data-router-badge style="display:none;"></span>
              <span class="progress-elapsed" data-elapsed>0s</span>
            </div>
            <div data-tools style="margin-top:8px;display:none;font-size:12px;color:var(--muted);"></div>
            <div data-body style="margin-top:10px;white-space:pre-wrap;line-height:1.48;color:var(--text);"></div>
            <div class="meta" data-footer style="display:none;"></div>
          `;
          messages.appendChild(el);
          messages.scrollTop = messages.scrollHeight;
          const stageEl = el.querySelector('[data-stage]');
          const routerBadgeEl = el.querySelector('[data-router-badge]');
          const elapsedEl = el.querySelector('[data-elapsed]');
          const spinner = el.querySelector('[data-spinner]');
          const toolsEl = el.querySelector('[data-tools]');
          const bodyEl = el.querySelector('[data-body]');
          const footerEl = el.querySelector('[data-footer]');
          const tools = new Map();
          const started = Date.now();
          const elapsedTimer = window.setInterval(() => {
            elapsedEl.textContent = `${Math.floor((Date.now() - started) / 1000)}s`;
          }, 500);
          let firstChunkSeen = false;
          let lastRouter = null;
          return {
            el,
            meta(payload) {
              resolvedAgent = payload.agent || resolvedAgent;
              lastRouter = payload.router || null;
              updateRouterBadge(routerBadgeEl, lastRouter);
              if (lastRouter && lastRouter.mode === 'auto') {
                stageEl.textContent = `Auto \u2192 ${resolvedAgent || '?'}`;
              } else {
                stageEl.textContent = `Calling ${resolvedAgent || '?'}`;
              }
            },
            retry(payload) {
              // Gateway switched to a runner-up agent after the first one
              // failed pre-stream. Update the bubble header to reflect that.
              const prev = payload.previous_agent || resolvedAgent || '?';
              const next = payload.selected_agent || '?';
              resolvedAgent = next;
              stageEl.textContent = `Retry: ${prev} failed \u2192 ${next}`;
              if (lastRouter) {
                const method = lastRouter.method || '';
                lastRouter = {
                  ...lastRouter,
                  selected_agent: next,
                  reason: payload.reason || lastRouter.reason,
                  method: method ? (method.endsWith('+retry') ? method : `${method}+retry`) : 'retry',
                };
                updateRouterBadge(routerBadgeEl, lastRouter);
              }
              // Show a small notice in the body that we changed agents, so
              // the final answer has visible provenance.
              const note = document.createElement('div');
              note.className = 'meta';
              note.style.marginBottom = '8px';
              note.style.color = 'var(--muted)';
              note.textContent = `(${prev} returned an error \u2192 retrying with ${next})`;
              bodyEl.parentNode.insertBefore(note, bodyEl);
            },
            toolStarted(name) {
              tools.set(name, 'running');
              toolsEl.style.display = '';
              toolsEl.textContent = Array.from(tools.entries())
                .map(([n, s]) => s === 'running' ? `\u2192 ${n}...` : (s === 'done' ? `\u2713 ${n}` : `\u2717 ${n}`))
                .join('  ');
            },
            toolFinished(name, error) {
              tools.set(name, error ? 'error' : 'done');
              toolsEl.textContent = Array.from(tools.entries())
                .map(([n, s]) => s === 'running' ? `\u2192 ${n}...` : (s === 'done' ? `\u2713 ${n}` : `\u2717 ${n}`))
                .join('  ');
              messages.scrollTop = messages.scrollHeight;
            },
            chunk(text) {
              if (!firstChunkSeen) {
                firstChunkSeen = true;
                stageEl.textContent = 'Streaming response';
              }
              bodyEl.textContent += text;
              messages.scrollTop = messages.scrollHeight;
            },
            done(payload) {
              window.clearInterval(elapsedTimer);
              spinner.style.visibility = 'hidden';
              el.className = 'message';
              stageEl.textContent = resolvedAgent ? `Done \u00b7 ${resolvedAgent}` : 'Done';
              const elapsed = Math.floor((Date.now() - started) / 1000);
              const parts = [];
              if (payload.usage && payload.usage.total_tokens != null) {
                parts.push(`${payload.usage.total_tokens} tokens`);
              }
              parts.push(`${elapsed}s`);
              if (resolvedAgent) parts.push(resolvedAgent);
              parts.push(...routerFooterParts(lastRouter));
              footerEl.textContent = parts.join(' \u00b7 ');
              footerEl.style.display = '';
              if (!bodyEl.textContent) {
                bodyEl.textContent = '(no response text)';
              }
            },
            error(payload) {
              window.clearInterval(elapsedTimer);
              spinner.style.visibility = 'hidden';
              el.className = 'message error';
              stageEl.textContent = resolvedAgent ? `Failed \u00b7 ${resolvedAgent}` : 'Failed';
              bodyEl.textContent = payload.message || 'Streaming failed.';
              const errorParts = [];
              if (payload.error_type) errorParts.push(payload.error_type);
              if (resolvedAgent) errorParts.push(resolvedAgent);
              footerEl.textContent = errorParts.join(' \u00b7 ');
              footerEl.style.display = '';
            },
          };
        }

    async function refreshStatus() {
      try {
        const response = await fetch('/api/status');
        const data = await response.json();
        document.getElementById('ready').textContent = data.ready ? 'ready' : 'starting';
        document.getElementById('lastChat').textContent = data.state.last_chat_at || 'none';
      } catch (_err) {
        document.getElementById('ready').textContent = 'offline';
      }
    }

        async function sendPrompt(prompt, agentName) {
          const selectedAgent = agentName || AUTO_AGENT;
          // Don't pre-label the user message with "AutoRouter" -- that name is
          // a routing sentinel, not the agent who will actually answer. The
          // resolved agent shows up on the assistant bubble once the gateway
          // emits its meta event.
          const userMeta = selectedAgent === AUTO_AGENT ? null : selectedAgent;
          addMessage(prompt, 'user', userMeta);
          const bubble = createStreamBubble(selectedAgent);
          send.disabled = true;
          textarea.disabled = true;
          busy.textContent = 'running';
          try {
            const response = await fetch('/api/chat-stream', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({message: prompt, agent: selectedAgent})
            });
            if (!response.ok) {
              let detail = 'Request failed';
              try { const j = await response.json(); detail = j.message || j.error || detail; } catch (_e) {}
              bubble.error({message: detail, error_type: `HTTP ${response.status}`});
              return;
            }
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buf = '';
            let terminated = false;
            while (!terminated) {
              const {value, done} = await reader.read();
              if (done) break;
              buf += decoder.decode(value, {stream: true});
              let idx;
              while ((idx = buf.indexOf('\n\n')) >= 0) {
                const block = buf.slice(0, idx);
                buf = buf.slice(idx + 2);
                const event = parseSSEBlock(block);
                if (!event) continue;
                handleEvent(event, bubble);
                if (event.type === 'done' || event.type === 'error') {
                  terminated = true;
                }
              }
            }
          } catch (err) {
            bubble.error({message: String(err), error_type: 'NetworkError'});
          } finally {
            send.disabled = false;
            textarea.disabled = false;
            busy.textContent = 'idle';
            textarea.focus();
            refreshStatus();
          }
        }

        // Build the small chips appended to the message footer that explain
        // which classifier picked the agent. Kept here (not on the server) so
        // the wording can evolve without re-deploying the gateway.
        function routerFooterParts(router) {
          if (!router || !router.method) return [];
          const out = [];
          out.push(`router: ${routerMethodLabel(router.method)}`);
          if (typeof router.confidence === 'number' && router.confidence > 0) {
            out.push(`conf ${router.confidence.toFixed(2)}`);
          }
          if (Array.isArray(router.candidates) && router.candidates.length > 1) {
            const others = router.candidates
              .slice(1, 3)
              .filter((c) => c && c.agent && c.agent !== router.selected_agent)
              .map((c) => c.agent);
            if (others.length) out.push(`runner-up: ${others.join(', ')}`);
          }
          return out;
        }

        function updateRouterBadge(el, router) {
          if (!el || !router || !router.method || router.method === 'manual') {
            if (el) el.style.display = 'none';
            return;
          }
          el.textContent = `router: ${routerMethodLabel(router.method)}`;
          el.title = router.reason || '';
          el.style.display = 'inline-flex';
        }

        function routerMethodLabel(method) {
          const value = String(method || '').trim();
          const retried = value.endsWith('+retry');
          const base = retried ? value.slice(0, -6) : value;
          const labelMap = {
            keyword: 'keyword',
            bm25: 'BM25',
            bm25_fallback: 'BM25 fallback',
            llm: 'LLM',
            manual: 'manual',
            auto: 'auto',
            retry: 'retry',
          };
          const label = labelMap[base] || base;
          return retried ? `${label} retry` : label;
        }

        function parseSSEBlock(block) {
          let eventType = '';
          const dataLines = [];
          for (const line of block.split('\n')) {
            if (line.startsWith('event:')) {
              eventType = line.slice(6).trim();
            } else if (line.startsWith('data:')) {
              dataLines.push(line.slice(5).trim());
            }
          }
          if (!eventType) return null;
          try {
            return {type: eventType, data: JSON.parse(dataLines.join('\n'))};
          } catch (_e) {
            return null;
          }
        }

        function handleEvent(event, bubble) {
          switch (event.type) {
            case 'meta':           bubble.meta(event.data); break;
            // Gateway emits 'routing-retry' when it falls back to a runner-up
            // because the first agent failed before any text was streamed.
            case 'routing-retry':  bubble.retry(event.data); break;
            case 'routing':        bubble.meta({agent: event.data.selected_agent, router: event.data}); break;
            case 'chunk':          bubble.chunk(event.data.text || ''); break;
            case 'tool_started':   bubble.toolStarted(event.data.name); break;
            case 'tool_finished':  bubble.toolFinished(event.data.name, event.data.error); break;
            case 'done':           bubble.done(event.data); break;
            case 'error':          bubble.error(event.data); break;
          }
        }

        form.addEventListener('submit', (event) => {
          event.preventDefault();
          const prompt = textarea.value.trim();
          if (!prompt) return;
          textarea.value = '';
          sendPrompt(prompt);
        });

    // Quick-buttons are nothing more than prompt templates: they fill the
    // textarea and submit. Routing decisions live entirely on the gateway.
    document.querySelectorAll('[data-prompt]').forEach((button) => {
      button.addEventListener('click', () => {
        sendPrompt(button.dataset.prompt);
      });
    });

        textarea.placeholder = 'Ask anything; the gateway router picks an agent for you...';
        refreshStatus();
    setInterval(refreshStatus, 15000);
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    from agent_chat_ui.__main__ import *  # noqa: F401,F403
