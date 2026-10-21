from __future__ import annotations

import json

import pytest

from multicloud_ai_agent_gateway.settings import AgentEntry, Settings


def _make_settings(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings()


def test_defaults_when_env_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _make_settings({}, monkeypatch)
    assert settings.foundry_max_tool_rounds == 5
    assert settings.foundry_request_timeout_seconds == 900
    assert settings.health_port == 8080
    assert settings.parsed_agents() == []


def test_reads_standard_azure_workload_identity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _make_settings(
        {
            "AZURE_CLIENT_ID": "uami-client",
            "AZURE_TENANT_ID": "tenant",
            "AZURE_FEDERATED_TOKEN_FILE": "/var/run/token",
        },
        monkeypatch,
    )

    assert settings.azure_client_id == "uami-client"
    assert settings.azure_tenant_id == "tenant"
    assert settings.azure_federated_token_file == "/var/run/token"
    assert settings.azure_workload_identity_configured()


def test_parses_agent_list_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        [
            {
                "name": "AzureFinOpsAgent",
                "backend_id": "foundry",
                "description": "Read-only.",
                "foundry_agent_name": "AzureFinOpsAgent",
            }
        ]
    )
    settings = _make_settings({"GATEWAY_AGENTS_JSON": payload}, monkeypatch)
    [entry] = settings.parsed_agents()
    assert entry.name == "AzureFinOpsAgent"
    assert entry.backend_id == "foundry"
    assert entry.foundry_agent_name == "AzureFinOpsAgent"


def test_accepts_gcp_direct_backend_id() -> None:
    entry = AgentEntry.model_validate(
        {
            "name": "GcpFinOpsClassicAgent",
            "backend_id": "gcp-direct",
            "direct_handler": "gcp_cost_mtd_by_service",
        }
    )
    assert entry.backend_id == "gcp-direct"
    assert entry.direct_handler == "gcp_cost_mtd_by_service"


def test_parses_foundry_fallback_agent_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _make_settings(
        {
            "GATEWAY_FOUNDRY_FALLBACK_AGENTS_JSON": json.dumps(
                {
                    "AzureFinOpsAgent": "AzureFinOpsAgentMini",
                    "AzureMLTrainingAgent": "AzureMLTrainingAgentMini",
                }
            )
        },
        monkeypatch,
    )

    assert settings.parsed_foundry_fallback_agents() == {
        "AzureFinOpsAgent": "AzureFinOpsAgentMini",
        "AzureMLTrainingAgent": "AzureMLTrainingAgentMini",
    }


def test_rejects_invalid_backend_id() -> None:
    with pytest.raises(ValueError, match="backend_id"):
        AgentEntry.model_validate({"name": "X", "backend_id": "vertex"})


def test_invalid_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _make_settings({"GATEWAY_AGENTS_JSON": "{not json"}, monkeypatch)
    with pytest.raises(ValueError, match="not valid JSON"):
        settings.parsed_agents()


def test_non_array_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _make_settings({"GATEWAY_AGENTS_JSON": '{"name":"x"}'}, monkeypatch)
    with pytest.raises(ValueError, match="JSON array"):
        settings.parsed_agents()


def test_auth_and_store_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _make_settings({}, monkeypatch)
    assert settings.auth_mode == "none"
    assert settings.parsed_auth_issuers() == []
    assert settings.conversation_store == "memory"
    assert settings.mcp_enabled and settings.a2a_enabled
    assert settings.foundry_fallback_replay_tool_results is True
    assert settings.foundry_stream_fallback_holdback_chars == 0


def test_parses_auth_issuers(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        [
            {"issuer": "https://login.microsoftonline.com/t/v2.0", "audience": "api://gw"},
            {
                "issuer": "https://oidc.example/aks",
                "audience": ["gw", "gw2"],
                "tenant_claim": "kubernetes.io/namespace",
                "algorithms": ["RS256", "ES256"],
            },
        ]
    )
    settings = _make_settings(
        {"GATEWAY_AUTH_MODE": "JWT", "GATEWAY_AUTH_ISSUERS_JSON": payload}, monkeypatch
    )
    assert settings.auth_mode == "jwt"
    entra, aks = settings.parsed_auth_issuers()
    assert entra.audiences() == ("api://gw",)
    assert aks.audiences() == ("gw", "gw2")
    assert aks.tenant_claim == "kubernetes.io/namespace"
    assert aks.algorithms == ["RS256", "ES256"]


def test_rejects_bad_auth_config(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError):
        _make_settings({"GATEWAY_AUTH_MODE": "basic"}, monkeypatch)
    monkeypatch.delenv("GATEWAY_AUTH_MODE")
    settings = _make_settings({"GATEWAY_AUTH_ISSUERS_JSON": "{oops"}, monkeypatch)
    with pytest.raises(ValueError):
        settings.parsed_auth_issuers()
    settings = _make_settings(
        {"GATEWAY_AUTH_ISSUERS_JSON": '[{"issuer": "https://x", "audience": ""}]'}, monkeypatch
    )
    with pytest.raises(ValueError):
        settings.parsed_auth_issuers()


def test_rejects_unknown_conversation_store(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError):
        _make_settings({"GATEWAY_CONVERSATION_STORE": "redis"}, monkeypatch)


def test_aws_bridge_uses_client_secret_credential_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from multicloud_ai_agent_gateway.app import _build_aws_bridge

    settings = _make_settings(
        {
            "GATEWAY_AWS_REGION": "us-east-1",
            "GATEWAY_AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/x",
            "GATEWAY_AWS_BRIDGE_TENANT_ID": "tenant",
            "GATEWAY_AWS_BRIDGE_CLIENT_ID": "client",
            "GATEWAY_AWS_BRIDGE_SCOPE": "api://x/.default",
            "GATEWAY_AWS_BRIDGE_CLIENT_SECRET": "dev-secret",
        },
        monkeypatch,
    )
    bridge = _build_aws_bridge(settings)
    assert type(bridge._aad_credential).__name__ == "ClientSecretCredential"

    monkeypatch.delenv("GATEWAY_AWS_BRIDGE_CLIENT_SECRET")
    bridge = _build_aws_bridge(Settings())
    assert type(bridge._aad_credential).__name__ == "WorkloadIdentityCredential"
