from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from multicloud_ai_agent_gateway import __version__
from multicloud_ai_agent_gateway.backends.agentcore import AgentCoreBackend
from multicloud_ai_agent_gateway.backends.direct import DirectBackend, DirectHandler
from multicloud_ai_agent_gateway.backends.direct_handlers import (
    build_aws_cost_mtd_by_service_handler,
    build_demo_echo_handler,
)
from multicloud_ai_agent_gateway.backends.foundry import FoundryBackend
from multicloud_ai_agent_gateway.backends.gcp_direct_handlers import (
    build_gcp_agent_handler,
    build_gcp_asset_inventory_summary_handler,
    build_gcp_cost_mtd_by_service_handler,
)
from multicloud_ai_agent_gateway.core import (
    AgentBackend,
    AgentRegistry,
    AgentSpec,
    BM25Classifier,
    HybridClassifier,
    KeywordClassifier,
    LLMRouter,
    PromptClassifier,
    Router,
    load_dispatcher,
)
from multicloud_ai_agent_gateway.frontends.rest import build_rest_app
from multicloud_ai_agent_gateway.settings import AgentEntry, Settings, load_settings

log = logging.getLogger(__name__)

# Direct (in-process) handlers that need workload-identity-derived AWS
# credentials. Any other direct handler (e.g. demo_echo) runs without the
# bridge, so a laptop with no cloud config can still start the gateway.
AWS_BRIDGE_DIRECT_HANDLERS = frozenset({"aws_cost_mtd_by_service"})


def _direct_handler_key(entry: AgentEntry) -> str:
    return entry.direct_handler or entry.name


def _build_router(settings: Settings) -> Router:
    entries = settings.parsed_agents()
    if not entries:
        raise RuntimeError(
            "No agents configured. Set GATEWAY_AGENTS_JSON to a JSON list of agent entries."
        )

    backends: dict[str, AgentBackend] = {}
    if any(entry.backend_id == "foundry" for entry in entries):
        backends["foundry"] = FoundryBackend(
            settings.foundry_project_endpoint,
            dispatcher=load_dispatcher(settings.foundry_dispatcher_path),
            max_tool_rounds=settings.foundry_max_tool_rounds,
            request_timeout_seconds=settings.foundry_request_timeout_seconds,
            model_fallback_enabled=settings.foundry_model_fallback_enabled,
            fallback_agent_names=settings.parsed_foundry_fallback_agents(),
        )

    # The AWS bridge is shared by the AgentCore (LLM-planned) and direct
    # (classic, zero-LLM) backends -- both need workload-identity-derived
    # AWS credentials. Build it once if any agent on either backend needs it.
    needs_aws_bridge = any(
        entry.backend_id == "agentcore"
        or (
            entry.backend_id == "direct"
            and _direct_handler_key(entry) in AWS_BRIDGE_DIRECT_HANDLERS
        )
        for entry in entries
    )
    aws_bridge = None
    if needs_aws_bridge:
        if not settings.aws_bridge_configured():
            raise RuntimeError(
                "GATEWAY_AGENTS_JSON includes an AWS-bridge backend (agentcore "
                "or direct) but the AWS bridge is not fully configured. "
                "Required env: GATEWAY_AWS_REGION, GATEWAY_AWS_ROLE_ARN, "
                "GATEWAY_AWS_BRIDGE_TENANT_ID, GATEWAY_AWS_BRIDGE_CLIENT_ID, "
                "GATEWAY_AWS_BRIDGE_SCOPE."
            )
        aws_bridge = _build_aws_bridge(settings)

    if any(entry.backend_id == "agentcore" for entry in entries):
        backends["agentcore"] = AgentCoreBackend(
            bridge=aws_bridge,
            propagate_user_id=settings.aws_bridge_propagate_user_id,
        )

    direct_entries = [e for e in entries if e.backend_id == "direct"]
    if direct_entries:
        handlers = _build_direct_handlers(direct_entries, aws_bridge, settings)
        backends["direct"] = DirectBackend(handlers)

    gcp_direct_entries = [e for e in entries if e.backend_id == "gcp-direct"]
    if gcp_direct_entries:
        handlers = _build_gcp_direct_handlers(gcp_direct_entries, settings)
        backends["gcp-direct"] = DirectBackend(handlers)

    specs = [
        AgentSpec(
            name=entry.name,
            backend_id=entry.backend_id,
            description=entry.description,
            remote_id=_remote_id_for(entry),
            tags=tuple(entry.tags),
            example_prompts=tuple(entry.example_prompts),
        )
        for entry in entries
    ]
    classifier = _build_classifier(settings, specs) if settings.auto_router_enabled else None
    return Router(AgentRegistry(specs), backends, classifier=classifier)


def _build_classifier(settings: Settings, specs: list[AgentSpec]) -> PromptClassifier:
    mode = settings.router_mode
    if mode == "keyword":
        return KeywordClassifier()
    bm25 = BM25Classifier(specs)
    if mode == "bm25":
        return bm25
    # mode == "hybrid": attach LLM only if fully configured. Otherwise hybrid
    # degrades gracefully to BM25 with method='bm25_fallback' tagging on
    # ambiguous prompts so the UI can show the model wasn't consulted.
    llm = _try_build_llm_router(settings, specs)
    if llm is None:
        log.info(
            "router_mode=hybrid but LLM is not configured; running BM25-only with "
            "fallback tagging. Set GATEWAY_ROUTER_LLM_ENDPOINT + DEPLOYMENT to enable."
        )
    return HybridClassifier(
        bm25,
        llm=llm,
        absolute_threshold=settings.router_bm25_absolute_threshold,
        confidence_threshold=settings.router_bm25_confidence_threshold,
    )


def _try_build_llm_router(settings: Settings, specs: list[AgentSpec]) -> LLMRouter | None:
    endpoint = settings.router_llm_endpoint.strip()
    deployment = settings.router_llm_deployment.strip()
    if not endpoint or not deployment:
        return None
    try:
        from multicloud_ai_agent_gateway.core import AzureOpenAIClient
    except ImportError:  # pragma: no cover - only when core package is broken
        log.warning("LLM router unavailable: AzureOpenAIClient import failed.")
        return None

    api_key = settings.router_llm_api_key.strip() or None
    token_provider = None
    if api_key is None:
        token_provider = _build_aad_token_provider(settings)
        if token_provider is None:
            log.warning(
                "LLM router not configured: neither router_llm_api_key nor a workload "
                "identity credential is available; hybrid will run BM25-only."
            )
            return None

    try:
        client = AzureOpenAIClient(
            endpoint=endpoint,
            deployment=deployment,
            api_version=settings.router_llm_api_version,
            api_key=api_key,
            azure_ad_token_provider=token_provider,
        )
    except Exception as exc:  # noqa: BLE001 - third-party SDK can throw anything
        log.warning("LLM router init failed (%s); hybrid will run BM25-only.", exc)
        return None

    return LLMRouter(specs, client, timeout_seconds=settings.router_llm_timeout_seconds)


def _build_aad_token_provider(settings: Settings):
    """Workload-identity-backed bearer-token provider for Azure Cognitive Services."""
    if settings.azure_workload_identity_configured():
        tenant_id = settings.azure_tenant_id
        client_id = settings.azure_client_id
        token_file_path = settings.azure_federated_token_file
    elif settings.aws_bridge_tenant_id and settings.aws_bridge_client_id:
        # Fallback for local / older deployments where only the bridge App
        # Registration is configured. The AKS deployment uses AZURE_* so the
        # LLM router authenticates as the same UAMI that already has Foundry /
        # Azure OpenAI data-plane roles.
        tenant_id = settings.aws_bridge_tenant_id
        client_id = settings.aws_bridge_client_id
        token_file_path = settings.aws_bridge_token_file
    else:
        return None
    try:
        from azure.identity import WorkloadIdentityCredential, get_bearer_token_provider
    except ImportError:  # pragma: no cover
        return None
    try:
        credential = WorkloadIdentityCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            token_file_path=token_file_path,
        )
        return get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("AAD token provider for LLM router failed: %s", exc)
        return None


def _build_aws_bridge(settings: Settings):
    from azure.identity import WorkloadIdentityCredential

    from multicloud_ai_agent_gateway.auth.aws_bridge import AwsBridgeCredentials

    credential = WorkloadIdentityCredential(
        tenant_id=settings.aws_bridge_tenant_id,
        client_id=settings.aws_bridge_client_id,
        token_file_path=settings.aws_bridge_token_file,
    )
    return AwsBridgeCredentials(
        aad_credential=credential,
        scope=settings.aws_bridge_scope,
        role_arn=settings.aws_role_arn,
        region=settings.aws_region,
        session_name=settings.aws_bridge_session_name,
        refresh_safety_margin_seconds=settings.aws_bridge_refresh_margin_seconds,
    )


def _remote_id_for(entry: AgentEntry) -> str:
    if entry.backend_id == "foundry":
        return entry.foundry_agent_name or entry.name
    if entry.backend_id == "agentcore":
        if not entry.agentcore_runtime_arn:
            raise ValueError(f"Agent {entry.name!r} requires agentcore_runtime_arn for AgentCore backend.")
        return entry.agentcore_runtime_arn
    if entry.backend_id == "direct":
        # remote_id for direct agents is the handler key the DirectBackend
        # dispatches on. Falling back to the agent name keeps configs short
        # when the agent name already matches a registered handler.
        return entry.direct_handler or entry.name
    if entry.backend_id == "gcp-direct":
        return entry.direct_handler or entry.name
    raise ValueError(f"Unsupported backend_id: {entry.backend_id!r}")


def _build_direct_handlers(
    entries: list[AgentEntry],
    aws_bridge,
    settings: Settings,
) -> dict[str, DirectHandler]:
    """Instantiate the DirectHandler closures for every direct agent.

    Add a new classic agent by writing a build_*_handler in
    backends/direct_handlers.py, then extending the if/elif below.
    """
    handlers: dict[str, DirectHandler] = {}
    for entry in entries:
        key = _direct_handler_key(entry)
        if key in handlers:
            continue  # multiple agents may share one handler
        if key == "demo_echo":
            handlers[key] = build_demo_echo_handler()
        elif key == "aws_cost_mtd_by_service":
            if aws_bridge is None:
                raise RuntimeError(
                    f"Handler {key!r} needs the AWS bridge but it was not built."
                )
            handlers[key] = build_aws_cost_mtd_by_service_handler(
                aws_bridge, region=settings.aws_region or "us-east-1"
            )
        else:
            raise RuntimeError(
                f"Agent {entry.name!r} requests unknown direct handler {key!r}."
            )
    return handlers


def _build_gcp_direct_handlers(
    entries: list[AgentEntry],
    settings: Settings,
) -> dict[str, DirectHandler]:
    """Instantiate GCP deterministic handlers used by the gcp-direct backend."""
    handlers: dict[str, DirectHandler] = {}
    for entry in entries:
        key = entry.direct_handler or entry.name
        if key in handlers:
            continue
        if key == "gcp_agent":
            cost_handler = build_gcp_cost_mtd_by_service_handler(
                project_id=settings.gcp_project_id,
                billing_dataset=settings.gcp_billing_dataset,
                billing_table=settings.gcp_billing_table,
                location=settings.gcp_bigquery_location,
            )
            inventory_handler = build_gcp_asset_inventory_summary_handler(
                project_id=settings.gcp_project_id,
            )
            handlers[key] = build_gcp_agent_handler(
                cost_handler=cost_handler,
                inventory_handler=inventory_handler,
            )
        elif key == "gcp_cost_mtd_by_service":
            handlers[key] = build_gcp_cost_mtd_by_service_handler(
                project_id=settings.gcp_project_id,
                billing_dataset=settings.gcp_billing_dataset,
                billing_table=settings.gcp_billing_table,
                location=settings.gcp_bigquery_location,
            )
        elif key == "gcp_asset_inventory_summary":
            handlers[key] = build_gcp_asset_inventory_summary_handler(
                project_id=settings.gcp_project_id,
            )
        else:
            raise RuntimeError(
                f"Agent {entry.name!r} requests unknown GCP direct handler {key!r}."
            )
    return handlers


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    logging.basicConfig(level=settings.log_level.upper())
    router = _build_router(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("Multicloud AI Agent Gateway starting (version=%s)", __version__)
        try:
            yield
        finally:
            log.info("Multicloud AI Agent Gateway shutting down")
            await router.aclose()

    app = build_rest_app(router=router, gateway_version=__version__)
    app.router.lifespan_context = lifespan
    return app
