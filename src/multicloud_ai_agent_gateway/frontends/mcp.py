"""Model Context Protocol facade: every agent becomes an MCP tool.

Transport: MCP *Streamable HTTP* on a single path (default ``/mcp``). The
client POSTs one JSON-RPC message per request; this server answers with a
plain JSON body (the spec allows either JSON or an SSE stream per request)
and keeps no session, so there is no ``Mcp-Session-Id`` and ``GET`` on the
path is a 405. That is the smallest conforming server and enough for
Claude, Cursor, VS Code, the MCP Inspector and the official SDKs.

Tools exposed (filtered by the caller's tenant policy):

* one tool per registered agent, named after the agent, taking ``prompt``
  and an optional ``conversation_id``;
* ``AutoRouter`` when auto-routing is enabled;
* ``route_prompt``: the routing decision without invoking anything.

All calls go through ``GatewayService`` like the REST API, so authentication,
tenant policy, sticky conversations and runner-up retry behave identically.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from multicloud_ai_agent_gateway.auth.principal import Principal
from multicloud_ai_agent_gateway.core.backend import (
    AgentReturnedError,
    AutoRouterContinueUnknown,
    BackendFailure,
    ConversationConflict,
    Forbidden,
    GatewayError,
    ToolBudgetExceeded,
    UnknownAgent,
)
from multicloud_ai_agent_gateway.core.routing import AUTO_ROUTER_DESCRIPTION, AUTO_ROUTER_NAME
from multicloud_ai_agent_gateway.core.service import GatewayService
from multicloud_ai_agent_gateway.frontends.common import (
    Authenticator,
    bearer_principal_dependency,
    http_status_for,
)

log = logging.getLogger(__name__)

LATEST_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "multicloud-ai-agent-gateway"
ROUTE_TOOL_NAME = "route_prompt"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

_PROMPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string", "description": "The user's request for the agent."},
        "conversation_id": {
            "type": "string",
            "description": (
                "Opaque id from a previous result to continue that conversation. "
                "Omit to start a new one."
            ),
        },
    },
    "required": ["prompt"],
    "additionalProperties": False,
}

_INVOKE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "output_text": {"type": "string"},
        "conversation_id": {"type": ["string", "null"]},
        "finish_reason": {"type": "string"},
        "agent": {"type": "string"},
        "usage": {"type": ["object", "null"]},
        "tool_events": {"type": "array", "items": {"type": "object"}},
        "routing": {"type": ["object", "null"]},
    },
    "required": ["output_text", "agent"],
}

_ROUTE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string", "description": "Prompt to classify."},
    },
    "required": ["prompt"],
    "additionalProperties": False,
}


def mount_mcp(
    app: FastAPI,
    *,
    service: GatewayService,
    gateway_version: str,
    authenticator: Authenticator | None = None,
    path: str = "/mcp",
) -> None:
    principal_dep = bearer_principal_dependency(authenticator)
    handler = McpHandler(service, gateway_version=gateway_version)

    @app.post(path, tags=["mcp"], include_in_schema=False)
    async def mcp_post(request: Request, principal: Principal = Depends(principal_dep)) -> Response:
        try:
            message = json.loads(await request.body() or b"")
        except json.JSONDecodeError as exc:
            return _rpc_error_response(None, PARSE_ERROR, f"Parse error: {exc}", status=400)

        if isinstance(message, list):
            return _rpc_error_response(
                None,
                INVALID_REQUEST,
                "JSON-RPC batching is not supported by this server; send one message per request.",
                status=400,
            )
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _rpc_error_response(
                None, INVALID_REQUEST, "Expected a JSON-RPC 2.0 message.", status=400
            )

        if "method" not in message:
            # A response from the client (to a server request). We never send
            # requests, so there is nothing to correlate: acknowledge it.
            return Response(status_code=202)

        if "id" not in message or message.get("id") is None:
            await handler.handle_notification(str(message.get("method")), message.get("params"))
            return Response(status_code=202)

        result = await handler.handle_request(
            principal,
            message["id"],
            str(message.get("method")),
            message.get("params"),
        )
        return JSONResponse(result, status_code=200)

    @app.get(path, include_in_schema=False)
    async def mcp_get() -> Response:
        # No server-initiated stream: this server never pushes messages.
        return Response(status_code=405, headers={"Allow": "POST"})

    @app.delete(path, include_in_schema=False)
    async def mcp_delete() -> Response:
        return Response(status_code=405, headers={"Allow": "POST"})


class McpHandler:
    """Protocol logic independent of the HTTP layer (unit-testable)."""

    def __init__(self, service: GatewayService, *, gateway_version: str) -> None:
        self._service = service
        self._version = gateway_version

    async def handle_notification(self, method: str, params: Any) -> None:
        # notifications/initialized, notifications/cancelled, ...: nothing to do
        # for a stateless server, but log unexpected ones for diagnostics.
        if method not in {"notifications/initialized", "notifications/cancelled", "notifications/progress"}:
            log.debug("Ignoring MCP notification %s", method)

    async def handle_request(
        self, principal: Principal, request_id: Any, method: str, params: Any
    ) -> dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        try:
            if method == "initialize":
                result = self.initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.list_tools(principal)}
            elif method == "tools/call":
                result = await self.call_tool(principal, params)
            else:
                return _rpc_error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")
        except _RpcError as exc:
            return _rpc_error(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # noqa: BLE001 - never leak a traceback as HTTP 500
            log.exception("MCP %s failed", method)
            return _rpc_error(request_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # -- methods --------------------------------------------------------------

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = str(params.get("protocolVersion") or "")
        negotiated = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        return {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": self._version},
            "instructions": (
                "Each tool is an AI agent hosted on Azure Foundry, AWS Bedrock AgentCore or "
                "Google Cloud behind this gateway. Call a tool with a natural-language "
                "'prompt'; pass back 'conversation_id' from the result to continue the same "
                f"conversation. '{AUTO_ROUTER_NAME}' picks the agent for you; "
                f"'{ROUTE_TOOL_NAME}' only shows which agent would be picked."
            ),
        }

    def list_tools(self, principal: Principal) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for spec in self._service.list_agents(principal):
            description = spec.description or f"Agent {spec.name} ({spec.backend_id})."
            if spec.example_prompts:
                examples = "; ".join(spec.example_prompts[:3])
                description = f"{description} Example prompts: {examples}"
            tools.append(
                {
                    "name": spec.name,
                    "title": spec.name,
                    "description": description,
                    "inputSchema": _PROMPT_SCHEMA,
                    "outputSchema": _INVOKE_OUTPUT_SCHEMA,
                    "annotations": {"openWorldHint": True},
                }
            )
        if self._service.auto_router_available(principal):
            tools.append(
                {
                    "name": AUTO_ROUTER_NAME,
                    "title": "Auto-routed agent",
                    "description": AUTO_ROUTER_DESCRIPTION,
                    "inputSchema": _PROMPT_SCHEMA,
                    "outputSchema": _INVOKE_OUTPUT_SCHEMA,
                    "annotations": {"openWorldHint": True},
                }
            )
            tools.append(
                {
                    "name": ROUTE_TOOL_NAME,
                    "title": "Route a prompt (dry run)",
                    "description": (
                        "Return which agent the gateway would pick for the prompt, with "
                        "scores and runner-ups, without invoking anything."
                    ),
                    "inputSchema": _ROUTE_INPUT_SCHEMA,
                    "annotations": {"readOnlyHint": True, "idempotentHint": True},
                }
            )
        return tools

    async def call_tool(self, principal: Principal, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            raise _RpcError(INVALID_PARAMS, "tools/call requires a tool 'name'.")
        if not isinstance(arguments, dict):
            raise _RpcError(INVALID_PARAMS, "'arguments' must be an object.")
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise _RpcError(INVALID_PARAMS, "'arguments.prompt' must be a non-empty string.")

        if name == ROUTE_TOOL_NAME:
            return await self._route_tool(principal, prompt)

        conversation_id = arguments.get("conversation_id")
        if conversation_id is not None and not isinstance(conversation_id, str):
            raise _RpcError(INVALID_PARAMS, "'arguments.conversation_id' must be a string.")

        try:
            outcome = await self._service.invoke(principal, name, prompt, conversation_id or None)
        except (UnknownAgent, Forbidden) as exc:
            # Not a tool execution failure: the tool is not callable by this caller.
            raise _RpcError(INVALID_PARAMS, f"Unknown or unavailable tool {name!r}: {exc}") from exc
        except (ConversationConflict, AutoRouterContinueUnknown) as exc:
            raise _RpcError(INVALID_PARAMS, str(exc)) from exc
        except (AgentReturnedError, BackendFailure, ToolBudgetExceeded, GatewayError) as exc:
            status, detail = http_status_for(exc)
            return _tool_error(f"{detail} (gateway status {status})", error_type=type(exc).__name__)

        reply = outcome.reply
        structured = {
            "output_text": reply.output_text,
            "conversation_id": reply.conversation_id,
            "finish_reason": reply.finish_reason,
            "agent": outcome.agent,
            "usage": reply.usage,
            "tool_events": [event.to_dict() for event in reply.tool_events],
            "routing": outcome.decision.to_dict() if outcome.decision is not None else None,
        }
        return {
            "content": [{"type": "text", "text": reply.output_text}],
            "structuredContent": structured,
            "isError": False,
        }

    async def _route_tool(self, principal: Principal, prompt: str) -> dict[str, Any]:
        try:
            decision = await asyncio.to_thread(self._service.route, principal, prompt)
        except GatewayError as exc:
            status, detail = http_status_for(exc)
            return _tool_error(f"{detail} (gateway status {status})", error_type=type(exc).__name__)
        payload = decision.to_dict()
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"{decision.selected_agent} ({decision.method}, confidence {decision.confidence:.2f}): {decision.reason}",
                }
            ],
            "structuredContent": payload,
            "isError": False,
        }


class _RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _tool_error(text: str, *, error_type: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": {"error": {"type": error_type, "message": text}},
        "isError": True,
    }


def _rpc_error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _rpc_error_response(request_id: Any, code: int, message: str, *, status: int) -> JSONResponse:
    return JSONResponse(_rpc_error(request_id, code, message), status_code=status)


__all__ = [
    "LATEST_PROTOCOL_VERSION",
    "ROUTE_TOOL_NAME",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "McpHandler",
    "mount_mcp",
]
