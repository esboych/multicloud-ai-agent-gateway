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
