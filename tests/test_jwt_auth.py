"""JwtAuthenticator against locally generated keys. No network: the key
resolver is injected."""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from multicloud_ai_agent_gateway.auth import IssuerConfig, JwtAuthenticator, claim_path
from multicloud_ai_agent_gateway.core.backend import AuthenticationRequired

ENTRA_ISSUER = "https://login.microsoftonline.com/11111111-1111-1111-1111-111111111111/v2.0"
AKS_ISSUER = "https://eastus2.oic.prod-aks.azure.com/tenant/cluster/"
GATEWAY_AUD = "api://multicloud-ai-agent-gateway"


@pytest.fixture(scope="module")
def keypair() -> tuple[bytes, Any]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return pem, private.public_key()


@pytest.fixture(scope="module")
def other_keypair() -> tuple[bytes, Any]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return pem, private.public_key()


def _resolver_for(public_key: Any):
    def resolve(cfg: IssuerConfig, token: str) -> Any:
        return public_key

    return resolve


def _authenticator(public_key: Any, **overrides: Any) -> JwtAuthenticator:
    issuers = [
        IssuerConfig(issuer=ENTRA_ISSUER, audience=(GATEWAY_AUD,), label="entra"),
        IssuerConfig(
            issuer=AKS_ISSUER,
            audience=("multicloud-ai-agent-gateway",),
            tenant_claim="kubernetes.io/namespace",
            label="aks",
        ),
    ]
    return JwtAuthenticator(issuers, key_resolver=_resolver_for(public_key), **overrides)


def _token(pem: bytes, claims: dict[str, Any], *, headers: dict[str, Any] | None = None) -> str:
    base = {"iat": int(time.time()), "exp": int(time.time()) + 300}
    base.update(claims)
    return jwt.encode(base, pem, algorithm="RS256", headers=headers or {"kid": "k1"})


def test_entra_token_yields_principal_with_tenant_and_scopes(keypair) -> None:
    pem, public = keypair
    token = _token(
        pem,
        {
            "iss": ENTRA_ISSUER,
            "aud": GATEWAY_AUD,
            "sub": "sp-object-id",
            "tid": "11111111-1111-1111-1111-111111111111",
            "scp": "agents.invoke agents.read",
            "roles": ["FinOps.Reader"],
        },
    )
    principal = _authenticator(public).authenticate(token)
    assert principal.subject == "sp-object-id"
    assert principal.tenant == "11111111-1111-1111-1111-111111111111"
    assert principal.issuer == ENTRA_ISSUER
    assert principal.scopes == ("agents.invoke", "agents.read", "FinOps.Reader")
    assert not principal.anonymous


def test_kubernetes_serviceaccount_token_maps_namespace_to_tenant(keypair) -> None:
    pem, public = keypair
    token = _token(
        pem,
        {
            "iss": AKS_ISSUER.rstrip("/"),  # AKS tokens omit the trailing slash
            "aud": ["multicloud-ai-agent-gateway"],
            "sub": "system:serviceaccount:team-a:billing-bot",
            "kubernetes.io": {
                "namespace": "team-a",
                "serviceaccount": {"name": "billing-bot", "uid": "abc"},
            },
        },
    )
    principal = _authenticator(public).authenticate(token)
    assert principal.tenant == "team-a"
    assert principal.subject == "system:serviceaccount:team-a:billing-bot"
    assert principal.scopes == ()


def test_wrong_audience_rejected(keypair) -> None:
    pem, public = keypair
    token = _token(pem, {"iss": ENTRA_ISSUER, "aud": "api://something-else", "sub": "x"})
    with pytest.raises(AuthenticationRequired, match="audience"):
        _authenticator(public).authenticate(token)


def test_expired_token_rejected(keypair) -> None:
    pem, public = keypair
    token = _token(
        pem, {"iss": ENTRA_ISSUER, "aud": GATEWAY_AUD, "sub": "x", "exp": int(time.time()) - 600}
    )
    with pytest.raises(AuthenticationRequired, match="expired"):
        _authenticator(public).authenticate(token)


def test_unknown_issuer_rejected(keypair) -> None:
    pem, public = keypair
    token = _token(pem, {"iss": "https://evil.example/v2.0", "aud": GATEWAY_AUD, "sub": "x"})
    with pytest.raises(AuthenticationRequired, match="not trusted"):
        _authenticator(public).authenticate(token)


def test_token_signed_by_another_key_rejected(keypair, other_keypair) -> None:
    other_pem, _ = other_keypair
    _, public = keypair
    token = _token(other_pem, {"iss": ENTRA_ISSUER, "aud": GATEWAY_AUD, "sub": "x"})
    with pytest.raises(AuthenticationRequired, match="rejected"):
        _authenticator(public).authenticate(token)


def test_hs256_token_against_rs256_issuer_rejected(keypair) -> None:
    # Algorithm confusion guard: the issuer config pins RS256.
    _, public = keypair
    token = jwt.encode(
        {"iss": ENTRA_ISSUER, "aud": GATEWAY_AUD, "sub": "x", "exp": int(time.time()) + 60},
        "shared-secret-that-is-long-enough-for-hmac-sha256",
        algorithm="HS256",
    )
    with pytest.raises(AuthenticationRequired):
        _authenticator(public).authenticate(token)


def test_malformed_token_rejected(keypair) -> None:
    _, public = keypair
    with pytest.raises(AuthenticationRequired, match="Malformed"):
        _authenticator(public).authenticate("not.a.jwt")
    with pytest.raises(AuthenticationRequired, match="empty"):
        _authenticator(public).authenticate("   ")


def test_missing_subject_rejected(keypair) -> None:
    pem, public = keypair
    token = _token(pem, {"iss": ENTRA_ISSUER, "aud": GATEWAY_AUD})
    with pytest.raises(AuthenticationRequired, match="subject"):
        _authenticator(public).authenticate(token)


def test_key_resolver_failure_becomes_auth_error(keypair) -> None:
    pem, _ = keypair
    token = _token(pem, {"iss": ENTRA_ISSUER, "aud": GATEWAY_AUD, "sub": "x"})

    def broken(cfg: IssuerConfig, token: str) -> Any:
        raise RuntimeError("jwks down")

    authenticator = JwtAuthenticator(
        [IssuerConfig(issuer=ENTRA_ISSUER, audience=(GATEWAY_AUD,))], key_resolver=broken
    )
    with pytest.raises(AuthenticationRequired, match="signing key"):
        authenticator.authenticate(token)


def test_tenant_claim_missing_gives_none(keypair) -> None:
    pem, public = keypair
    token = _token(pem, {"iss": ENTRA_ISSUER, "aud": GATEWAY_AUD, "sub": "x"})
    principal = _authenticator(public).authenticate(token)
    assert principal.tenant is None


def test_claim_path_direct_key_wins_over_nested() -> None:
    claims = {"https://aws.amazon.com/tags": {"principal_tags": {"t": ["1"]}}, "a": {"b": "c"}}
    assert claim_path(claims, "https://aws.amazon.com/tags") == {"principal_tags": {"t": ["1"]}}
    assert claim_path(claims, "a/b") == "c"
    assert claim_path(claims, "a/missing") is None
    assert claim_path(claims, "zzz") is None
    assert claim_path(claims, "") is None


def test_issuer_config_validation() -> None:
    with pytest.raises(ValueError):
        IssuerConfig(issuer="", audience=("a",))
    with pytest.raises(ValueError):
        IssuerConfig(issuer="https://x", audience=())
    with pytest.raises(ValueError):
        IssuerConfig(issuer="https://x", audience=("a",), algorithms=("none",))


def test_authenticator_rejects_duplicate_issuers() -> None:
    with pytest.raises(ValueError):
        JwtAuthenticator(
            [
                IssuerConfig(issuer="https://x/", audience=("a",)),
                IssuerConfig(issuer="https://x", audience=("a",)),
            ],
            key_resolver=lambda cfg, token: None,
        )
    with pytest.raises(ValueError):
        JwtAuthenticator([], key_resolver=lambda cfg, token: None)
