# Build context = repository root.
#
#   docker build -f aks/agent-chat-ui/Dockerfile -t agent-chat-ui:dev .
#
# The chat UI is standard-library only (http.server + urllib), so the image
# copies the package and nothing else: no pip, no cloud SDKs.

FROM python:3.12.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY src/agent_chat_ui ./agent_chat_ui

RUN useradd --create-home --uid 10001 appuser
USER 10001

EXPOSE 8080

CMD ["python", "-m", "agent_chat_ui"]
