"""Pieces shared by the protocol facades (REST, MCP, A2A).

* ``bearer_principal_dependency``: FastAPI dependency that turns the
  ``Authorization: Bearer`` header into a ``Principal`` through the configured
  authenticator, or returns the anonymous principal when authentication is
  off.
* ``http_error_for``: one table from ``GatewayError`` subclasses to HTTP
  status codes, so every facade reports the same thing for the same failure.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Protocol, runtime_checkable

from fastapi import HTTPException, Request

from multicloud_ai_agent_gateway.auth.principal import Principal
from multicloud_ai_agent_gateway.core.backend import (
    AgentReturnedError,
    AuthenticationRequired,
    AutoRouterContinueUnknown,
    AutoRouterNotConfigured,
    BackendFailure,
    ConversationConflict,
    Forbidden,
    GatewayError,
    NoDispatcherConfigured,
    ToolBudgetExceeded,
    UnknownAgent,
)

WWW_AUTHENTICATE = 'Bearer realm="multicloud-ai-agent-gateway"'


@runtime_checkable
class Authenticator(Protocol):
    """Anything that turns a bearer token into a Principal (or raises
    ``AuthenticationRequired``). ``JwtAuthenticator`` is the production one."""

    def authenticate(self, token: str) -> Principal: ...


def bearer_token_from_header(header_value: str | None) -> str | None:
    if not header_value:
        return None
    scheme, _, token = header_value.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def bearer_principal_dependency(
    authenticator: Authenticator | None,
) -> Callable[[Request], Awaitable[Principal]]:
    async def dependency(request: Request) -> Principal:
        if authenticator is None:
            return Principal.anonymous_principal()
        token = bearer_token_from_header(request.headers.get("authorization"))
        if token is None:
            raise HTTPException(
                status_code=401,
                detail="Bearer token required.",
                headers={"WWW-Authenticate": WWW_AUTHENTICATE},
            )
        try:
            # JWKS fetches and signature checks are blocking; keep the loop free.
            return await asyncio.to_thread(authenticator.authenticate, token)
        except AuthenticationRequired as exc:
            raise HTTPException(
                status_code=401,
                detail=str(exc),
                headers={"WWW-Authenticate": WWW_AUTHENTICATE},
            ) from exc

    return dependency


def http_status_for(exc: Exception) -> tuple[int, str]:
    """(status, detail) for an exception raised by the service layer."""
    if isinstance(exc, UnknownAgent):
        return 404, str(exc)
    if isinstance(exc, (NoDispatcherConfigured, AutoRouterNotConfigured)):
        return 422, str(exc)
    if isinstance(exc, ToolBudgetExceeded):
        return 504, str(exc)
    if isinstance(exc, AuthenticationRequired):
        return 401, str(exc)
    if isinstance(exc, Forbidden):
        return 403, str(exc)
    if isinstance(exc, ConversationConflict):
        return 409, str(exc)
    if isinstance(exc, AutoRouterContinueUnknown):
        return 400, str(exc)
    if isinstance(exc, AgentReturnedError):
        return 502, f"agent_returned_error: {exc.error_type}: {exc}"
    if isinstance(exc, BackendFailure):
        return 502, f"backend_error: {exc.cause_type}"
    if isinstance(exc, GatewayError):
        return 502, f"backend_error: {type(exc).__name__}"
    return 502, f"backend_error: {type(exc).__name__}"


def http_error_for(exc: Exception) -> HTTPException:
    status, detail = http_status_for(exc)
    headers = {"WWW-Authenticate": WWW_AUTHENTICATE} if status == 401 else None
    return HTTPException(status_code=status, detail=detail, headers=headers)


__all__ = [
    "WWW_AUTHENTICATE",
    "Authenticator",
    "bearer_principal_dependency",
    "bearer_token_from_header",
    "http_error_for",
    "http_status_for",
]
