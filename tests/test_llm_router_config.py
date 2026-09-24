from __future__ import annotations

import azure.identity
import pytest

from multicloud_ai_agent_gateway.app import _build_aad_token_provider
from multicloud_ai_agent_gateway.settings import Settings


def test_llm_token_provider_prefers_pod_workload_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeWorkloadIdentityCredential:
        def __init__(
            self, *, tenant_id: str, client_id: str, token_file_path: str
        ) -> None:
            captured["tenant_id"] = tenant_id
            captured["client_id"] = client_id
            captured["token_file_path"] = token_file_path

    def fake_get_bearer_token_provider(
        credential: FakeWorkloadIdentityCredential, scope: str
    ):
        captured["credential"] = credential
        captured["scope"] = scope
        return lambda: "token"

    monkeypatch.setattr(
        azure.identity, "WorkloadIdentityCredential", FakeWorkloadIdentityCredential
    )
    monkeypatch.setattr(
        azure.identity, "get_bearer_token_provider", fake_get_bearer_token_provider
    )
    monkeypatch.setenv("AZURE_TENANT_ID", "uami-tenant")
    monkeypatch.setenv("AZURE_CLIENT_ID", "uami-client")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/tokens/uami")
    monkeypatch.setenv("GATEWAY_AWS_BRIDGE_TENANT_ID", "bridge-tenant")
    monkeypatch.setenv("GATEWAY_AWS_BRIDGE_CLIENT_ID", "bridge-client")
    monkeypatch.setenv("GATEWAY_AWS_BRIDGE_TOKEN_FILE", "/tokens/bridge")

    provider = _build_aad_token_provider(Settings())

    assert provider is not None
    assert captured["tenant_id"] == "uami-tenant"
    assert captured["client_id"] == "uami-client"
    assert captured["token_file_path"] == "/tokens/uami"
    assert captured["scope"] == "https://cognitiveservices.azure.com/.default"
