# Build context = repository root.
#
#   docker build -f aks/multicloud-ai-agent-gateway/Dockerfile \
#     -t multicloud-ai-agent-gateway:dev .
#
# The image ships only this repository. A Foundry tool dispatcher
# (GATEWAY_FOUNDRY_DISPATCHER_PATH, see examples/dispatcher_stub.py) is an
# optional extension point: to bundle one, build a derived image FROM this
# one and `pip install` your dispatcher package there, or add a COPY + pip
# install block below.

FROM python:3.12.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE constraints.txt ./
COPY src ./src
RUN pip install -c constraints.txt . && rm -rf ./src

RUN useradd --create-home --uid 10001 appuser
USER 10001

EXPOSE 8080

CMD ["python", "-m", "multicloud_ai_agent_gateway"]
