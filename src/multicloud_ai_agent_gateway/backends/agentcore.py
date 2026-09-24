"""AWS Bedrock AgentCore backend.

Calls bedrock-agentcore.invoke_agent_runtime with SigV4-signed STS credentials
obtained from the AAD->AWS bridge. AgentCore handles tools server-side, so the
gateway never sees function_call items here -- tool_events is always empty.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, AsyncIterator, Awaitable, Callable

from multicloud_ai_agent_gateway.auth.aws_bridge import AwsBridgeCredentials
from multicloud_ai_agent_gateway.backends.foundry import _bridge_to_async
from multicloud_ai_agent_gateway.core.backend import (
    AgentEvent,
    AgentReply,
    AgentReturnedError,
    StreamDone,
    StreamError,
    TextChunk,
)

log = logging.getLogger(__name__)


# Top-level keys an AWS-shaped error payload uses. AgentCore agents that catch
# Bedrock/IAM exceptions and JSON-encode them keep one of these.
_AWS_ERROR_TYPE_KEYS: tuple[str, ...] = ("__type", "errorType", "Code", "errorCode")
_AWS_ERROR_MESSAGE_KEYS: tuple[str, ...] = ("message", "Message", "errorMessage")

# Free-text fallback: the response is "<CamelCase>Exception: ..." or
# "<CamelCase>Error: ..." with nothing else interesting in the payload. Anchored
# to the start; the response must be short to avoid swallowing legit answers
# that happen to begin with an exception name.
_AWS_ERROR_TEXT_RE = re.compile(
    r"^([A-Z][A-Za-z0-9]*(?:Exception|Error|Fault))\s*:\s*(.+)$",
    re.DOTALL,
)
_AWS_ERROR_TEXT_MAX_LEN = 800


# Async factory that returns a bedrock-agentcore boto3 client. Production wires
# this to a function that fetches fresh STS credentials and constructs a client;
# tests inject a fake.
ClientFactory = Callable[[], Awaitable[Any]]


async def _build_default_client(bridge: AwsBridgeCredentials) -> Any:
    creds = await bridge.get_credentials()
    import boto3  # deferred to avoid a heavy import on Foundry-only deployments

    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=bridge.region,
    )
    return session.client("bedrock-agentcore")


def _extract_text(body: bytes, content_type: str | None) -> str:
    text = body.decode("utf-8", errors="replace")
    if content_type and "json" in content_type:
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(obj, dict):
            for key in ("output_text", "text", "result", "output", "message"):
                value = obj.get(key)
                if isinstance(value, str):
                    return value
            return json.dumps(obj, default=str)
        if isinstance(obj, list):
            return json.dumps(obj, default=str)
    return text


def detect_agent_error(parsed_json: Any, extracted_text: str) -> tuple[str, str] | None:
    """Detect AWS/AgentCore-shaped error payloads inside an otherwise-200 reply.

    Returns ``(error_type, message)`` if the response looks like an error a
    server-side agent forwarded as JSON instead of failing the HTTP call;
    returns ``None`` for normal responses.

    Two strategies, in order:

    1. Structured keys -- look for ``__type``/``errorType``/``Code``/
       ``errorCode`` at the top level of the JSON object. AWS APIs serialize
       errors this way; AgentCore agents that re-raise Bedrock exceptions
       typically inherit the same shape.
    2. Free-text pattern -- the extracted text is short and starts with a
       CamelCase ``XxxException``/``XxxError``/``XxxFault`` token followed by
       ``:`` and a message. Catches the case where the agent runtime caught
       the exception and stuffed ``str(exc)`` into a ``message`` field.

    The text fallback is bounded by length (`_AWS_ERROR_TEXT_MAX_LEN`) so an
    answer that legitimately *discusses* an exception (e.g. a long FinOps
    explanation) is not misclassified.
    """
    if isinstance(parsed_json, dict):
        for key in _AWS_ERROR_TYPE_KEYS:
            value = parsed_json.get(key)
            if isinstance(value, str) and value:
                error_type = value.split("#")[-1]
                message = next(
                    (
                        parsed_json[mkey]
                        for mkey in _AWS_ERROR_MESSAGE_KEYS
                        if isinstance(parsed_json.get(mkey), str)
                        and parsed_json.get(mkey)
                    ),
                    extracted_text,
                )
                return error_type, str(message)

    snippet = extracted_text.strip()
    if 0 < len(snippet) <= _AWS_ERROR_TEXT_MAX_LEN:
        match = _AWS_ERROR_TEXT_RE.match(snippet)
        if match:
            # Strip the exception-name prefix from the message: callers want
            # ('ThrottlingException', 'Too many tokens') not the duplicated
            # ('ThrottlingException', 'ThrottlingException: Too many tokens').
            return match.group(1), match.group(2).strip()
    return None


def parse_invoke_response(response: dict[str, Any]) -> AgentReply:
    """Pure transform: AgentCore InvokeAgentRuntime response -> AgentReply.

    Extracted so tests can exercise the response-shape handling without the
    boto3 client. Raises ``AgentReturnedError`` when the response payload is a
    server-side error wrapped as JSON (see ``detect_agent_error``).
    """
    body = response.get("response")
    if body is None:
        raw = b""
    elif isinstance(body, (bytes, bytearray)):
        raw = bytes(body)
    elif hasattr(body, "read"):
        raw = body.read() or b""
    else:
        raw = str(body).encode("utf-8")

    content_type = response.get("contentType")
    text = _extract_text(raw, content_type)

    parsed_json: Any = None
    if content_type and "json" in content_type:
        try:
            parsed_json = json.loads(raw.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed_json = None

    detected = detect_agent_error(parsed_json, text)
    if detected is not None:
        error_type, message = detected
        log.warning(
            "AgentCore returned a wrapped error: type=%s message=%r",
            error_type,
            message[:200],
        )
        raise AgentReturnedError(error_type, message)

    return AgentReply(
        output_text=text,
        conversation_id=response.get("runtimeSessionId"),
        tool_events=[],
        usage=None,
        finish_reason="completed",
    )


class AgentCoreBackend:
    """AgentBackend implementation for AWS Bedrock AgentCore Runtime."""

    backend_id = "agentcore"

    def __init__(
        self,
        *,
        bridge: AwsBridgeCredentials | None = None,
        client_factory: ClientFactory | None = None,
        propagate_user_id: bool = False,
    ) -> None:
        if client_factory is None and bridge is None:
            raise ValueError("AgentCoreBackend requires either bridge or client_factory")
        self._bridge = bridge
        self._client_factory = client_factory or (lambda: _build_default_client(bridge))  # type: ignore[arg-type]
        self._propagate_user_id = propagate_user_id

    async def invoke(
        self,
        remote_id: str,
        prompt: str,
        *,
        conversation_id: str | None = None,
    ) -> AgentReply:
        if not remote_id:
            raise ValueError("AgentCore agent requires a non-empty runtime ARN as remote_id.")
        client = await self._client_factory()
        return await asyncio.to_thread(
            self._invoke_sync,
            client=client,
            runtime_arn=remote_id,
            prompt=prompt,
            conversation_id=conversation_id,
        )

    def _invoke_sync(
        self,
        *,
        client: Any,
        runtime_arn: str,
        prompt: str,
        conversation_id: str | None,
    ) -> AgentReply:
        payload = json.dumps({"prompt": prompt}).encode("utf-8")
        kwargs: dict[str, Any] = {
            "agentRuntimeArn": runtime_arn,
            "payload": payload,
            "contentType": "application/json",
            "accept": "application/json",
        }
        if conversation_id:
            kwargs["runtimeSessionId"] = conversation_id

        response = client.invoke_agent_runtime(**kwargs)
        reply = parse_invoke_response(response)
        if reply.conversation_id is None and conversation_id:
            reply = AgentReply(
                output_text=reply.output_text,
                conversation_id=conversation_id,
                tool_events=reply.tool_events,
                usage=reply.usage,
                finish_reason=reply.finish_reason,
            )
        return reply

    async def stream(
        self,
        remote_id: str,
        prompt: str,
        *,
        conversation_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Best-effort streaming: AgentCore Runtime returns a streaming body
        whose semantics depend on the agent implementation. For now we just
        invoke synchronously inside the producer thread, then emit the final
        reply text as one TextChunk + StreamDone. Real chunked streaming for
        Strands-formatted agents is a follow-up.
        """
        if not remote_id:
            raise ValueError("AgentCore agent requires a non-empty runtime ARN as remote_id.")
        client = await self._client_factory()

        def producer(emit: Callable[[AgentEvent], None]) -> None:
            try:
                reply = self._invoke_sync(
                    client=client,
                    runtime_arn=remote_id,
                    prompt=prompt,
                    conversation_id=conversation_id,
                )
            except Exception as exc:  # noqa: BLE001 - report any AWS error
                emit(StreamError(message=str(exc), error_type=type(exc).__name__))
                return
            if reply.output_text:
                emit(TextChunk(text=reply.output_text))
            emit(
                StreamDone(
                    conversation_id=reply.conversation_id,
                    usage=reply.usage,
                    finish_reason=reply.finish_reason,
                )
            )

        async for event in _bridge_to_async(producer):
            yield event

    async def aclose(self) -> None:
        if self._bridge is not None:
            await self._bridge.aclose()
