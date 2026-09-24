"""DirectBackend: deterministic agents that never call an LLM.

While ``FoundryBackend`` and ``AgentCoreBackend`` proxy to upstream LLM agents
(Azure Foundry / AWS Bedrock), this backend invokes a plain async Python
callable in-process. The callable is a 'handler': it reads the prompt
(usually loosely), calls cloud APIs, and returns a finished ``AgentReply``.

Why this exists: classic deterministic agents do not consume LLM tokens.
That makes them the right fallback when Bedrock / Foundry token quotas are
exhausted, and the right primary for hot-path buttons whose meaning is
unambiguous (e.g. 'Current AWS costs', which always means MTD breakdown).

Handlers are resolved via a registry keyed by the agent's ``remote_id``
(taken from ``AgentSpec.remote_id``). app.py builds that registry once at
startup so new handlers are explicit Python references, not strings parsed
from configmaps at request time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

from multicloud_ai_agent_gateway.core.backend import (
    AgentEvent,
    AgentReply,
    StreamDone,
    TextChunk,
    UnknownAgent,
)

# Handler signature: async (prompt, conversation_id) -> AgentReply.
# Auth / clients are captured in the closure when the handler is constructed
# (see direct_handlers.py), so the backend itself stays stateless.
DirectHandler = Callable[[str, str | None], Awaitable[AgentReply]]


class DirectBackend:
    """Dispatch to in-process async handlers keyed by ``remote_id``."""

    backend_id = "direct"

    def __init__(self, handlers: dict[str, DirectHandler]) -> None:
        if not handlers:
            raise ValueError("DirectBackend requires at least one handler.")
        self._handlers = dict(handlers)

    @property
    def handlers(self) -> dict[str, DirectHandler]:
        return dict(self._handlers)

    async def invoke(
        self,
        remote_id: str,
        prompt: str,
        *,
        conversation_id: str | None = None,
    ) -> AgentReply:
        handler = self._handlers.get(remote_id)
        if handler is None:
            raise UnknownAgent(remote_id)
        return await handler(prompt, conversation_id)

    async def stream(
        self,
        remote_id: str,
        prompt: str,
        *,
        conversation_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Trivial stream: run the handler, emit its text in one chunk, done.

        Classic handlers are fast (one API call) so we don't bother with
        token-by-token streaming -- one chunk + done is enough for the
        chat-ui to render the same way as a real streaming response.
        """
        reply = await self.invoke(remote_id, prompt, conversation_id=conversation_id)
        if reply.output_text:
            yield TextChunk(text=reply.output_text)
        yield StreamDone(
            conversation_id=reply.conversation_id,
            usage=reply.usage,
            finish_reason=reply.finish_reason,
        )

    async def aclose(self) -> None:
        # Handlers own their own resources (boto3 clients are cheap, no
        # persistent connections). Nothing to close here.
        return None


__all__: list[str] = ["DirectBackend", "DirectHandler"]
