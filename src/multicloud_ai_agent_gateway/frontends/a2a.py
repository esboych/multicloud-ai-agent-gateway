"""Agent-to-Agent (A2A) protocol facade.

The gateway presents itself as one A2A agent whose *skills* are the
registered agents. Clients discover it through the agent card at
``/.well-known/agent-card.json`` and talk JSON-RPC to ``/a2a``:

* ``message/send``: run the prompt, return a completed ``Task`` with one
  text artifact. The skill (agent) is chosen with ``metadata.agent`` on the
  params or the message (aliases: ``skill``, ``agent_name``); without it the
  gateway auto-routes. ``contextId`` is the gateway conversation id.
* ``message/stream``: the same as Server-Sent Events: a ``Task`` in state
  ``submitted``, ``status-update`` events while working, ``artifact-update``
  chunks, and a final ``status-update`` with ``final: true``.
* ``tasks/get`` reads a finished task back (per replica, bounded memory);
  ``tasks/cancel`` is never possible because tasks complete synchronously.
* ``agent/getAuthenticatedExtendedCard`` returns the card with the skills
  the caller's tenant may actually use.

Push notifications and task resubscription are not supported and answer
with the A2A error codes reserved for that.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from multicloud_ai_agent_gateway.auth.principal import Principal
from multicloud_ai_agent_gateway.core.backend import (
    AgentReturnedError,
    AutoRouterContinueUnknown,
    BackendFailure,
    ConversationConflict,
    Forbidden,
    GatewayError,
    StreamDone,
    StreamError,
    TextChunk,
    ToolBudgetExceeded,
    ToolFinished,
    ToolStarted,
    UnknownAgent,
)
from multicloud_ai_agent_gateway.core.routing import AUTO_ROUTER_DESCRIPTION, AUTO_ROUTER_NAME
from multicloud_ai_agent_gateway.core.service import (
    GatewayService,
    RoutingEvent,
    RoutingRetryEvent,
)
from multicloud_ai_agent_gateway.frontends.common import (
    Authenticator,
    bearer_principal_dependency,
    http_status_for,
)

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "0.3.0"
AGENT_CARD_PATH = "/.well-known/agent-card.json"
LEGACY_AGENT_CARD_PATH = "/.well-known/agent.json"
DEFAULT_RPC_PATH = "/a2a"
AGENT_SELECTOR_KEYS = ("agent", "skill", "agent_name", "skillId", "skill_id")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
PUSH_NOTIFICATIONS_NOT_SUPPORTED = -32003
UNSUPPORTED_OPERATION = -32004
CONTENT_TYPE_NOT_SUPPORTED = -32005


def mount_a2a(
    app: FastAPI,
    *,
    service: GatewayService,
    gateway_version: str,
    public_url: str,
    authenticator: Authenticator | None = None,
    rpc_path: str = DEFAULT_RPC_PATH,
    max_tasks: int = 1000,
) -> None:
    principal_dep = bearer_principal_dependency(authenticator)
    handler = A2AHandler(
        service,
        gateway_version=gateway_version,
        public_url=public_url,
        rpc_path=rpc_path,
        auth_enabled=authenticator is not None,
        max_tasks=max_tasks,
    )

    @app.get(AGENT_CARD_PATH, tags=["a2a"], include_in_schema=False)
    @app.get(LEGACY_AGENT_CARD_PATH, include_in_schema=False)
    async def agent_card() -> JSONResponse:
        # The public card is discovery metadata and is served without auth;
        # it lists every registered agent by name and description.
        return JSONResponse(handler.agent_card(principal=None))

    @app.post(rpc_path, tags=["a2a"], include_in_schema=False)
    async def a2a_rpc(request: Request, principal: Principal = Depends(principal_dep)) -> Response:
        try:
            message = json.loads(await request.body() or b"")
        except json.JSONDecodeError as exc:
            return _rpc_error_response(None, PARSE_ERROR, f"Parse error: {exc}", status=400)
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
            return _rpc_error_response(
                None, INVALID_REQUEST, "Expected a JSON-RPC 2.0 request.", status=400
            )
        request_id = message.get("id")
        method = str(message.get("method"))
        params = message.get("params") if isinstance(message.get("params"), dict) else {}

        if method == "message/stream":
            try:
                prepared = await handler.prepare_stream(principal, params)
            except _RpcError as exc:
                return JSONResponse(_rpc_error(request_id, exc.code, exc.message, exc.data))
            return StreamingResponse(
                _sse(handler.stream_events(principal, request_id, prepared)),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
            )

        return JSONResponse(await handler.handle_request(principal, request_id, method, params))


class A2AHandler:
    def __init__(
        self,
        service: GatewayService,
        *,
        gateway_version: str,
        public_url: str,
        rpc_path: str = DEFAULT_RPC_PATH,
        auth_enabled: bool = False,
        max_tasks: int = 1000,
    ) -> None:
        self._service = service
        self._version = gateway_version
        self._public_url = public_url.rstrip("/")
        self._rpc_path = rpc_path
        self._auth_enabled = auth_enabled
        self._max_tasks = max(1, int(max_tasks))
        self._tasks: OrderedDict[str, _StoredTask] = OrderedDict()

    # -- agent card ---------------------------------------------------------

    def agent_card(self, *, principal: Principal | None) -> dict[str, Any]:
        specs = (
            self._service.router.registry.list()
            if principal is None
            else self._service.list_agents(principal)
        )
        skills = [
            {
                "id": spec.name,
                "name": spec.name,
                "description": spec.description or f"Agent {spec.name} ({spec.backend_id}).",
                "tags": list(spec.tags) or [spec.backend_id],
                "examples": list(spec.example_prompts),
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
            }
            for spec in specs
        ]
        auto_available = (
            self._service.auto_router_enabled
            if principal is None
            else self._service.auto_router_available(principal)
        )
        if auto_available:
            skills.insert(
                0,
                {
                    "id": AUTO_ROUTER_NAME,
                    "name": "Auto-routed agent",
                    "description": AUTO_ROUTER_DESCRIPTION,
                    "tags": ["router"],
                    "examples": [],
                    "inputModes": ["text/plain"],
                    "outputModes": ["text/plain"],
                },
            )
        card: dict[str, Any] = {
            "name": "Multicloud AI Agent Gateway",
            "description": (
                "One A2A endpoint in front of AI agents hosted on Azure Foundry, AWS Bedrock "
                "AgentCore and Google Cloud. Pick a skill with metadata.agent, or omit it to "
                "let the gateway route the message."
            ),
            "url": f"{self._public_url}{self._rpc_path}",
            "preferredTransport": "JSONRPC",
            "version": self._version,
            "protocolVersion": PROTOCOL_VERSION,
            "provider": {"organization": "multicloud-ai-agent-gateway", "url": self._public_url},
            "capabilities": {
                "streaming": True,
                "pushNotifications": False,
                "stateTransitionHistory": False,
            },
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": skills,
            "supportsAuthenticatedExtendedCard": True,
        }
        if self._auth_enabled:
            card["securitySchemes"] = {
                "bearer": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
            }
            card["security"] = [{"bearer": []}]
        return card

    # -- JSON-RPC dispatch ----------------------------------------------------

    async def handle_request(
        self, principal: Principal, request_id: Any, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            if method == "message/send":
                result = await self.message_send(principal, params)
            elif method == "tasks/get":
                result = self.tasks_get(principal, params)
            elif method == "tasks/cancel":
                result = self.tasks_cancel(principal, params)
            elif method == "agent/getAuthenticatedExtendedCard":
                result = self.agent_card(principal=principal)
            elif method == "tasks/resubscribe":
                raise _RpcError(UNSUPPORTED_OPERATION, "Tasks complete synchronously; nothing to resubscribe to.")
            elif method.startswith("tasks/pushNotificationConfig/"):
                raise _RpcError(PUSH_NOTIFICATIONS_NOT_SUPPORTED, "Push notifications are not supported.")
            elif method == "message/stream":
                raise _RpcError(INTERNAL_ERROR, "message/stream must be handled by the SSE path.")
            else:
                raise _RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")
        except _RpcError as exc:
            return _rpc_error(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # noqa: BLE001 - never leak a traceback as HTTP 500
            log.exception("A2A %s failed", method)
            return _rpc_error(request_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # -- message/send ---------------------------------------------------------

    async def message_send(self, principal: Principal, params: dict[str, Any]) -> dict[str, Any]:
        parsed = _parse_send_params(params)
        task_id = _new_id()
        try:
            outcome = await self._service.invoke(
                principal, parsed.agent, parsed.text, parsed.context_id
            )
        except (UnknownAgent, Forbidden, ConversationConflict, AutoRouterContinueUnknown) as exc:
            raise _RpcError(INVALID_PARAMS, str(exc)) from exc
        except (AgentReturnedError, BackendFailure, ToolBudgetExceeded, GatewayError) as exc:
            status, detail = http_status_for(exc)
            task = self._failed_task(
                task_id, parsed, f"{detail} (gateway status {status})", type(exc).__name__
            )
            self._store(task_id, task, principal)
            return task

        reply = outcome.reply
        context_id = reply.conversation_id or parsed.context_id or _new_id()
        agent_message = _agent_message(task_id, context_id, reply.output_text)
        task = {
            "id": task_id,
            "contextId": context_id,
            "kind": "task",
            "status": {"state": "completed", "timestamp": _now_iso(), "message": agent_message},
            "artifacts": [_artifact(task_id, reply.output_text, outcome.agent)],
            "history": [parsed.user_message(task_id, context_id), agent_message],
            "metadata": {
                "agent": outcome.agent,
                "routing": outcome.decision.to_dict() if outcome.decision is not None else None,
                "usage": reply.usage,
                "tool_events": [event.to_dict() for event in reply.tool_events],
            },
        }
        self._store(task_id, task, principal)
        return task

    # -- message/stream ---------------------------------------------------

    async def prepare_stream(self, principal: Principal, params: dict[str, Any]) -> "_PreparedA2AStream":
        parsed = _parse_send_params(params)
        try:
            prepared = await self._service.prepare_stream(
                principal, parsed.agent, parsed.text, parsed.context_id
            )
        except (UnknownAgent, Forbidden, ConversationConflict, AutoRouterContinueUnknown) as exc:
            raise _RpcError(INVALID_PARAMS, str(exc)) from exc
        except GatewayError as exc:
            status, detail = http_status_for(exc)
            raise _RpcError(INTERNAL_ERROR, f"{detail} (gateway status {status})") from exc
        return _PreparedA2AStream(parsed, prepared)

    async def stream_events(
        self, principal: Principal, request_id: Any, prepared: "_PreparedA2AStream"
    ) -> AsyncIterator[dict[str, Any]]:
        parsed = prepared.params
        task_id = _new_id()
        context_id = parsed.context_id or _new_id()
        agent = prepared.stream.target
        chunks: list[str] = []
        routing: dict[str, Any] | None = (
            prepared.stream.decision.to_dict() if prepared.stream.decision is not None else None
        )
        tool_events: list[dict[str, Any]] = []

        def envelope(result: dict[str, Any]) -> dict[str, Any]:
            return {"jsonrpc": "2.0", "id": request_id, "result": result}

        yield envelope(
            {
                "id": task_id,
                "contextId": context_id,
                "kind": "task",
                "status": {"state": "submitted", "timestamp": _now_iso()},
                "history": [parsed.user_message(task_id, context_id)],
                "metadata": {"agent": agent, "routing": routing},
            }
        )

        final_sent = False
        try:
            async for event in prepared.stream.events():
                if isinstance(event, RoutingEvent):
                    routing = event.decision.to_dict()
                    agent = event.decision.selected_agent
                    yield envelope(_status_update(task_id, context_id, "working", metadata={"routing": routing}))
                elif isinstance(event, RoutingRetryEvent):
                    agent = event.selected_agent
                    yield envelope(
                        _status_update(task_id, context_id, "working", metadata={"routing_retry": event.to_dict()})
                    )
                elif isinstance(event, TextChunk):
                    first = not chunks
                    chunks.append(event.text)
                    yield envelope(
                        {
                            "taskId": task_id,
                            "contextId": context_id,
                            "kind": "artifact-update",
                            "artifact": _artifact(task_id, event.text, agent),
                            "append": not first,
                            "lastChunk": False,
                        }
                    )
                elif isinstance(event, (ToolStarted, ToolFinished)):
                    payload = event.to_dict()
                    payload["kind"] = "tool_started" if isinstance(event, ToolStarted) else "tool_finished"
                    tool_events.append(payload)
                    yield envelope(
                        _status_update(task_id, context_id, "working", metadata={"tool_event": payload})
                    )
                elif isinstance(event, StreamError):
                    text = f"{event.error_type}: {event.message}"
                    task = self._failed_task(task_id, parsed, text, event.error_type, context_id=context_id)
                    self._store(task_id, task, principal)
                    yield envelope(
                        _status_update(
                            task_id, context_id, "failed", final=True,
                            message=_agent_message(task_id, context_id, text),
                        )
                    )
                    final_sent = True
                    return
                elif isinstance(event, StreamDone):
                    if event.conversation_id:
                        context_id = event.conversation_id
                    text = "".join(chunks)
                    agent_message = _agent_message(task_id, context_id, text)
                    task = {
                        "id": task_id,
                        "contextId": context_id,
                        "kind": "task",
                        "status": {"state": "completed", "timestamp": _now_iso(), "message": agent_message},
                        "artifacts": [_artifact(task_id, text, agent)],
                        "history": [parsed.user_message(task_id, context_id), agent_message],
                        "metadata": {
                            "agent": agent,
                            "routing": routing,
                            "usage": event.usage,
                            "tool_events": tool_events,
                        },
                    }
                    self._store(task_id, task, principal)
                    yield envelope(
                        {
                            "taskId": task_id,
                            "contextId": context_id,
                            "kind": "artifact-update",
                            "artifact": _artifact(task_id, "", agent),
                            "append": True,
                            "lastChunk": True,
                        }
                    )
                    yield envelope(
                        _status_update(task_id, context_id, "completed", final=True, message=agent_message)
                    )
                    final_sent = True
                    return
        finally:
            if not final_sent:
                # Stream ended without a terminal event (client gone or backend
                # closed early): record what we have so tasks/get is consistent.
                text = "".join(chunks)
                task = self._failed_task(
                    task_id, parsed, "Stream ended before completion.", "StreamProtocolError",
                    context_id=context_id, partial_text=text,
                )
                self._store(task_id, task, principal)

    # -- tasks ----------------------------------------------------------------

    def tasks_get(self, principal: Principal, params: dict[str, Any]) -> dict[str, Any]:
        stored = self._lookup(principal, params)
        task = dict(stored.task)
        history_length = params.get("historyLength")
        if isinstance(history_length, int) and history_length >= 0:
            task["history"] = list(task.get("history") or [])[-history_length:] if history_length else []
        return task

    def tasks_cancel(self, principal: Principal, params: dict[str, Any]) -> dict[str, Any]:
        stored = self._lookup(principal, params)
        raise _RpcError(
            TASK_NOT_CANCELABLE,
            f"Task {stored.task['id']!r} is already {stored.task['status']['state']}.",
        )

    def _lookup(self, principal: Principal, params: dict[str, Any]) -> "_StoredTask":
        task_id = params.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise _RpcError(INVALID_PARAMS, "'id' is required.")
        stored = self._tasks.get(task_id)
        if stored is None:
            raise _RpcError(TASK_NOT_FOUND, f"Task not found: {task_id}")
        if not principal.anonymous and (
            stored.owner_subject != principal.subject or stored.owner_tenant != principal.tenant
        ):
            raise _RpcError(TASK_NOT_FOUND, f"Task not found: {task_id}")
        return stored

    def _store(self, task_id: str, task: dict[str, Any], principal: Principal) -> None:
        self._tasks[task_id] = _StoredTask(
            task=task,
            owner_subject=None if principal.anonymous else principal.subject,
            owner_tenant=None if principal.anonymous else principal.tenant,
            stored_at=time.time(),
        )
        while len(self._tasks) > self._max_tasks:
            self._tasks.popitem(last=False)

    def _failed_task(
        self,
        task_id: str,
        parsed: "_SendParams",
        text: str,
        error_type: str,
        *,
        context_id: str | None = None,
        partial_text: str = "",
    ) -> dict[str, Any]:
        context_id = context_id or parsed.context_id or _new_id()
        agent_message = _agent_message(task_id, context_id, text)
        task: dict[str, Any] = {
            "id": task_id,
            "contextId": context_id,
            "kind": "task",
            "status": {"state": "failed", "timestamp": _now_iso(), "message": agent_message},
            "history": [parsed.user_message(task_id, context_id), agent_message],
            "metadata": {"agent": parsed.agent, "error_type": error_type},
        }
        if partial_text:
            task["artifacts"] = [_artifact(task_id, partial_text, parsed.agent)]
        return task


# -- helpers ------------------------------------------------------------------


class _SendParams:
    def __init__(self, *, text: str, agent: str, context_id: str | None, message: dict[str, Any]) -> None:
        self.text = text
        self.agent = agent
        self.context_id = context_id
        self._message = message

    def user_message(self, task_id: str, context_id: str) -> dict[str, Any]:
        message = dict(self._message)
        message.setdefault("kind", "message")
        message.setdefault("role", "user")
        message.setdefault("messageId", _new_id())
        message["taskId"] = task_id
        message["contextId"] = context_id
        return message


class _PreparedA2AStream:
    def __init__(self, params: _SendParams, stream: Any) -> None:
        self.params = params
        self.stream = stream


class _StoredTask:
    def __init__(
        self,
        *,
        task: dict[str, Any],
        owner_subject: str | None,
        owner_tenant: str | None,
        stored_at: float,
    ) -> None:
        self.task = task
        self.owner_subject = owner_subject
        self.owner_tenant = owner_tenant
        self.stored_at = stored_at


class _RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _parse_send_params(params: dict[str, Any]) -> _SendParams:
    message = params.get("message")
    if not isinstance(message, dict):
        raise _RpcError(INVALID_PARAMS, "'message' is required.")
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts:
        raise _RpcError(INVALID_PARAMS, "'message.parts' must be a non-empty list.")
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = part.get("kind") or part.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif kind == "data" and isinstance(part.get("data"), dict):
            texts.append(json.dumps(part["data"], default=str))
        elif kind == "file":
            raise _RpcError(CONTENT_TYPE_NOT_SUPPORTED, "File parts are not supported; send text.")
    text = "\n".join(t for t in texts if t).strip()
    if not text:
        raise _RpcError(INVALID_PARAMS, "'message.parts' contains no text.")
    if message.get("taskId"):
        raise _RpcError(
            UNSUPPORTED_OPERATION,
            "Continuing an existing task is not supported; start a new message with the "
            "same contextId to continue the conversation.",
        )

    agent = _agent_selector(params.get("metadata")) or _agent_selector(message.get("metadata"))
    context_id = message.get("contextId")
    if context_id is not None and not isinstance(context_id, str):
        raise _RpcError(INVALID_PARAMS, "'message.contextId' must be a string.")
    return _SendParams(
        text=text,
        agent=agent or AUTO_ROUTER_NAME,
        context_id=context_id or None,
        message=message,
    )


def _agent_selector(metadata: Any) -> str | None:
    if not isinstance(metadata, dict):
        return None
    for key in AGENT_SELECTOR_KEYS:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _artifact(task_id: str, text: str, agent: str) -> dict[str, Any]:
    return {
        "artifactId": f"{task_id}-answer",
        "name": "answer",
        "description": f"Answer from {agent}",
        "parts": [{"kind": "text", "text": text}],
    }


def _agent_message(task_id: str, context_id: str, text: str) -> dict[str, Any]:
    return {
        "role": "agent",
        "parts": [{"kind": "text", "text": text}],
        "messageId": _new_id(),
        "taskId": task_id,
        "contextId": context_id,
        "kind": "message",
    }


def _status_update(
    task_id: str,
    context_id: str,
    state: str,
    *,
    final: bool = False,
    message: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status: dict[str, Any] = {"state": state, "timestamp": _now_iso()}
    if message is not None:
        status["message"] = message
    event: dict[str, Any] = {
        "taskId": task_id,
        "contextId": context_id,
        "kind": "status-update",
        "status": status,
        "final": final,
    }
    if metadata:
        event["metadata"] = metadata
    return event


async def _sse(events: AsyncIterator[dict[str, Any]]) -> AsyncIterator[str]:
    async for event in events:
        yield f"data: {json.dumps(event, default=str)}\n\n"


def _rpc_error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _rpc_error_response(request_id: Any, code: int, message: str, *, status: int) -> JSONResponse:
    return JSONResponse(_rpc_error(request_id, code, message), status_code=status)


def _new_id() -> str:
    return uuid.uuid4().hex


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["AGENT_CARD_PATH", "DEFAULT_RPC_PATH", "PROTOCOL_VERSION", "A2AHandler", "mount_a2a"]
