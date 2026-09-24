from __future__ import annotations

import json

from pydantic import AliasChoices, BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentEntry(BaseModel):
    """One agent registered with the gateway.

    ``tags`` and ``example_prompts`` are optional metadata consumed by the
    BM25 / LLM routers to score relevance. Adding more tokens here without
    touching code is the supported way to improve auto-routing quality.
    """

    name: str
    backend_id: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    example_prompts: list[str] = Field(default_factory=list)

    foundry_agent_name: str | None = None
    agentcore_runtime_arn: str | None = None
    # backend_id='direct' agents resolve their handler by this key. app.py
    # owns the (key -> async handler) registry; the configmap only names it.
    direct_handler: str | None = None

    @field_validator("backend_id")
    @classmethod
    def _known_backend(cls, value: str) -> str:
        if value not in {"foundry", "agentcore", "direct", "gcp-direct"}:
            raise ValueError(
                "backend_id must be 'foundry', 'agentcore', 'direct' or "
                f"'gcp-direct'; got {value!r}"
            )
        return value


class Settings(BaseSettings):
    """Environment-driven configuration for the gateway pod."""

    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    foundry_project_endpoint: str = Field(
        default="",
        description="Microsoft Foundry project endpoint, e.g. https://<acct>.services.ai.azure.com/api/projects/<project>",
    )

    foundry_dispatcher_path: str | None = Field(
        default=None,
        description=(
            "Optional 'module:attr' spec for a sync function that executes Foundry function-calls. "
            "Example: 'examples.dispatcher_stub:dispatch_function_call'. "
            "If unset, the gateway returns 422 when an agent requests a tool."
        ),
    )

    foundry_max_tool_rounds: int = Field(default=5, ge=1, le=20)
    foundry_request_timeout_seconds: int = Field(default=900, ge=1)
    foundry_model_fallback_enabled: bool = Field(
        default=True,
        description=(
            "If true, Foundry model rate-limit errors can be retried through "
            "the fallback agent mapping in foundry_fallback_agents_json."
        ),
    )
    foundry_fallback_agents_json: str = Field(
        default="{}",
        description=(
            "JSON object mapping primary Foundry agent names to fallback Foundry "
            "agent names, e.g. {'AzureFinOpsAgent':'AzureFinOpsAgentMini'}."
        ),
    )

    # ----- AgentCore / AWS bridge ---------------------------------------
    # All fields below are optional in isolation; build_app() validates that
    # they are all set when at least one agent uses backend_id="agentcore".

    aws_region: str = Field(default="", description="AWS region for AgentCore.")
    aws_role_arn: str = Field(default="", description="IAM role assumed via web identity.")
    aws_bridge_tenant_id: str = Field(default="", description="Entra tenant ID.")
    aws_bridge_client_id: str = Field(
        default="",
        description="Entra App Registration client ID used as token audience.",
    )
    aws_bridge_scope: str = Field(
        default="",
        description="Scope to request from WorkloadIdentityCredential, e.g. api://<client-id>/.default.",
    )
    aws_bridge_token_file: str = Field(
        default="/var/run/secrets/azure/tokens/azure-identity-token",
        description="Projected K8s ServiceAccount token file used by WorkloadIdentityCredential.",
    )
    aws_bridge_session_name: str = Field(default="multicloud-ai-agent-gateway")
    aws_bridge_refresh_margin_seconds: int = Field(default=60, ge=0)
    aws_bridge_propagate_user_id: bool = Field(default=False)

    # ----- Google Cloud direct / Agent Runtime bridge --------------------
    gcp_project_id: str = Field(default="", description="GCP project id for GCP direct agents.")
    gcp_region: str = Field(default="europe-west1", description="Default GCP region.")
    gcp_billing_dataset: str = Field(
        default="",
        description="BigQuery dataset id containing Cloud Billing export tables.",
    )
    gcp_billing_table: str = Field(
        default="",
        description="Optional explicit Cloud Billing export table id. Empty means auto-discover.",
    )
    gcp_bigquery_location: str = Field(default="EU", description="BigQuery job location for GCP billing queries.")

    agents_json: str = Field(
        default="[]",
        description="JSON-encoded list of AgentEntry objects.",
    )

    health_port: int = Field(default=8080, ge=1, le=65535)
    log_level: str = Field(default="INFO")

    auto_router_enabled: bool = Field(
        default=True,
        description=(
            "If true, the gateway exposes a virtual 'AutoRouter' agent and a POST /v1/route "
            "endpoint that classify a prompt to a registered agent. Set to false to disable "
            "routing and force callers to always pass the concrete agent name."
        ),
    )

    # Phase 1 router selection. 'hybrid' is the new default: BM25 over agent
    # metadata with optional LLM tie-break. 'keyword' preserves the legacy
    # 4-agent classifier for rollback. 'bm25' disables the LLM hop entirely.
    router_mode: str = Field(
        default="hybrid",
        description="One of 'keyword', 'bm25', 'hybrid'. See core/hybrid_router.py.",
    )
    router_bm25_absolute_threshold: float = Field(default=0.5, ge=0.0)
    router_bm25_confidence_threshold: float = Field(default=0.15, ge=0.0, le=1.0)

    # Optional LLM tie-breaker. When router_mode='hybrid' and any of
    # router_llm_endpoint / api_key is unset, hybrid degrades to BM25-only
    # (decisions are tagged method='bm25_fallback' when below threshold).
    router_llm_endpoint: str = Field(default="", description="Azure OpenAI endpoint URL.")
    router_llm_deployment: str = Field(default="", description="Azure OpenAI deployment name.")
    router_llm_api_version: str = Field(default="2024-08-01-preview")
    router_llm_api_key: str = Field(default="", description="API key auth; if empty, uses workload identity.")
    router_llm_timeout_seconds: float = Field(default=5.0, gt=0.0)

    # Standard Azure Workload Identity env vars. These intentionally do not
    # use the GATEWAY_ prefix because AKS / azure-identity expect these names.
    azure_client_id: str = Field(
        default="",
        validation_alias=AliasChoices("AZURE_CLIENT_ID", "GATEWAY_AZURE_CLIENT_ID"),
        description="Client ID of the pod's Azure workload identity.",
    )
    azure_tenant_id: str = Field(
        default="",
        validation_alias=AliasChoices("AZURE_TENANT_ID", "GATEWAY_AZURE_TENANT_ID"),
        description="Tenant ID of the pod's Azure workload identity.",
    )
    azure_federated_token_file: str = Field(
        default="",
        validation_alias=AliasChoices(
            "AZURE_FEDERATED_TOKEN_FILE",
            "GATEWAY_AZURE_FEDERATED_TOKEN_FILE",
        ),
        description="Projected ServiceAccount token file for Azure workload identity.",
    )

    @field_validator("router_mode")
    @classmethod
    def _known_router_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"keyword", "bm25", "hybrid"}:
            raise ValueError(
                f"router_mode must be one of 'keyword', 'bm25', 'hybrid'; got {value!r}"
            )
        return normalized

    def aws_bridge_configured(self) -> bool:
        return all(
            (
                self.aws_region,
                self.aws_role_arn,
                self.aws_bridge_tenant_id,
                self.aws_bridge_client_id,
                self.aws_bridge_scope,
            )
        )

    def azure_workload_identity_configured(self) -> bool:
        return all(
            (
                self.azure_tenant_id,
                self.azure_client_id,
                self.azure_federated_token_file,
            )
        )

    def parsed_agents(self) -> list[AgentEntry]:
        try:
            raw = json.loads(self.agents_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"GATEWAY_AGENTS_JSON is not valid JSON: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError("GATEWAY_AGENTS_JSON must encode a JSON array")
        return [AgentEntry.model_validate(item) for item in raw]

    def parsed_foundry_fallback_agents(self) -> dict[str, str]:
        try:
            raw = json.loads(self.foundry_fallback_agents_json or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"GATEWAY_FOUNDRY_FALLBACK_AGENTS_JSON is not valid JSON: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise ValueError("GATEWAY_FOUNDRY_FALLBACK_AGENTS_JSON must encode a JSON object")

        mapping: dict[str, str] = {}
        for primary, fallback in raw.items():
            primary_name = str(primary).strip()
            fallback_name = str(fallback).strip()
            if primary_name and fallback_name:
                mapping[primary_name] = fallback_name
        return mapping


def load_settings() -> Settings:
    return Settings()
