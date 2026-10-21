"""Bearer-token authentication against one or more OIDC issuers.

The gateway does not own identities. It trusts tokens from configured
issuers and reads the caller's subject, tenant and scopes from claims:

* Microsoft Entra ID: ``iss`` is ``https://login.microsoftonline.com/<dir>/v2.0``,
  tenant is the ``tid`` claim (the directory id) for cross-organisation
  setups, or an app role / custom claim inside one directory.
* Kubernetes ServiceAccount tokens (in-cluster callers): ``iss`` is the AKS
  OIDC issuer URL, the audience is whatever the caller projected
  (``audience: multicloud-ai-agent-gateway``), and the tenant is the
  namespace, reachable through the nested claim path
  ``kubernetes.io/namespace``.

Signing keys come from the issuer's JWKS endpoint (``jwks_uri`` in the
config, or discovered from ``<issuer>/.well-known/openid-configuration``).
Tests inject a ``key_resolver`` and never touch the network.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Callable

import jwt
from jwt import PyJWKClient

from multicloud_ai_agent_gateway.auth.principal import Principal
from multicloud_ai_agent_gateway.core.backend import AuthenticationRequired

log = logging.getLogger(__name__)

CLAIM_PATH_SEPARATOR = "/"


@dataclass(frozen=True)
class IssuerConfig:
    """One trusted token issuer."""

    issuer: str
    audience: tuple[str, ...]
    jwks_uri: str | None = None
    tenant_claim: str = "tid"
    subject_claim: str = "sub"
    scope_claims: tuple[str, ...] = ("scp", "scope", "roles")
    algorithms: tuple[str, ...] = ("RS256",)
    label: str = ""

    def __post_init__(self) -> None:
        if not self.issuer:
            raise ValueError("issuer is required")
        if not self.audience:
            raise ValueError(f"issuer {self.issuer!r}: at least one audience is required")
        if not self.algorithms:
            raise ValueError(f"issuer {self.issuer!r}: at least one algorithm is required")
        if any(alg.lower() == "none" for alg in self.algorithms):
            raise ValueError("algorithm 'none' is not allowed")


# Given an issuer config and the raw token, return the key PyJWT should
# verify the signature with. The default resolver fetches JWKS; tests inject
# a static key.
KeyResolver = Callable[[IssuerConfig, str], Any]


class JwtAuthenticator:
    def __init__(
        self,
        issuers: Iterable[IssuerConfig],
        *,
        key_resolver: KeyResolver | None = None,
        leeway_seconds: float = 30.0,
        http_timeout_seconds: float = 5.0,
    ) -> None:
        configs = list(issuers)
        if not configs:
            raise ValueError("JwtAuthenticator needs at least one issuer")
        self._by_issuer: dict[str, IssuerConfig] = {}
        for cfg in configs:
            key = _normalize_issuer(cfg.issuer)
            if key in self._by_issuer:
                raise ValueError(f"Duplicate issuer in auth config: {cfg.issuer!r}")
            self._by_issuer[key] = cfg
        self._resolve_key = key_resolver or JwksKeyResolver(http_timeout_seconds=http_timeout_seconds)
        self._leeway = float(leeway_seconds)

    @property
    def issuers(self) -> list[IssuerConfig]:
        return list(self._by_issuer.values())

    def authenticate(self, token: str) -> Principal:
        token = (token or "").strip()
        if not token:
            raise AuthenticationRequired("Bearer token is empty.")
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            raise AuthenticationRequired(f"Malformed token: {exc}") from exc

        issuer = str(unverified.get("iss") or "")
        cfg = self._by_issuer.get(_normalize_issuer(issuer))
        if cfg is None:
            raise AuthenticationRequired(f"Token issuer {issuer!r} is not trusted.")

        try:
            key = self._resolve_key(cfg, token)
        except AuthenticationRequired:
            raise
        except Exception as exc:  # noqa: BLE001 - JWKS fetch / parse can fail many ways
            log.warning("Signing key resolution failed for issuer %s: %s", cfg.issuer, exc)
            raise AuthenticationRequired("Could not resolve the token signing key.") from exc

        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=list(cfg.algorithms),
                audience=list(cfg.audience),
                issuer=issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iss"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationRequired("Token has expired.") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthenticationRequired("Token audience is not accepted by this gateway.") from exc
        except jwt.PyJWTError as exc:
            raise AuthenticationRequired(f"Token rejected: {exc}") from exc

        subject = claim_path(claims, cfg.subject_claim)
        if not isinstance(subject, str) or not subject:
            raise AuthenticationRequired(
                f"Token has no usable subject claim {cfg.subject_claim!r}."
            )
        tenant = claim_path(claims, cfg.tenant_claim)
        tenant_str = str(tenant) if tenant not in (None, "") else None

        return Principal(
            subject=subject,
            tenant=tenant_str,
            issuer=issuer,
            scopes=tuple(_scopes_from(claims, cfg.scope_claims)),
            claims=claims,
        )


class JwksKeyResolver:
    """Default ``KeyResolver``: one cached ``PyJWKClient`` per issuer.

    ``jwks_uri`` is taken from the issuer config or discovered once from the
    OIDC metadata document. PyJWT caches the JWK set and refreshes it when a
    token names an unknown ``kid``.
    """

    def __init__(self, *, http_timeout_seconds: float = 5.0, cache_lifespan_seconds: int = 300) -> None:
        self._timeout = float(http_timeout_seconds)
        self._lifespan = int(cache_lifespan_seconds)
        self._clients: dict[str, PyJWKClient] = {}
        self._lock = threading.Lock()

    def __call__(self, cfg: IssuerConfig, token: str) -> Any:
        client = self._client_for(cfg)
        return client.get_signing_key_from_jwt(token).key

    def _client_for(self, cfg: IssuerConfig) -> PyJWKClient:
        key = _normalize_issuer(cfg.issuer)
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                jwks_uri = cfg.jwks_uri or discover_jwks_uri(cfg.issuer, timeout=self._timeout)
                client = PyJWKClient(
                    jwks_uri,
                    cache_keys=True,
                    lifespan=self._lifespan,
                    timeout=self._timeout,
                )
                self._clients[key] = client
            return client


def discover_jwks_uri(issuer: str, *, timeout: float = 5.0) -> str:
    """Read ``jwks_uri`` from ``<issuer>/.well-known/openid-configuration``."""
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - issuer is operator config
        document = json.loads(response.read().decode("utf-8"))
    jwks_uri = document.get("jwks_uri")
    if not isinstance(jwks_uri, str) or not jwks_uri:
        raise ValueError(f"OIDC metadata at {url} has no jwks_uri")
    return jwks_uri


def claim_path(claims: Mapping[str, Any], path: str) -> Any:
    """Look up a claim by name, or by ``/``-separated path for nested claims.

    A top-level key that contains ``/`` wins over the nested interpretation,
    so ``https://aws.amazon.com/tags`` style claims still work. Kubernetes
    ServiceAccount tokens use ``kubernetes.io/namespace`` to reach
    ``claims["kubernetes.io"]["namespace"]``.
    """
    if not path:
        return None
    if path in claims:
        return claims[path]
    if CLAIM_PATH_SEPARATOR not in path:
        return None
    current: Any = claims
    for segment in path.split(CLAIM_PATH_SEPARATOR):
        if not isinstance(current, Mapping) or segment not in current:
            return None
        current = current[segment]
    return current


def _scopes_from(claims: Mapping[str, Any], scope_claims: Iterable[str]) -> list[str]:
    scopes: list[str] = []
    for name in scope_claims:
        value = claim_path(claims, name)
        if isinstance(value, str):
            items = value.split()
        elif isinstance(value, (list, tuple)):
            items = [str(item) for item in value]
        else:
            continue
        for item in items:
            if item and item not in scopes:
                scopes.append(item)
    return scopes


def _normalize_issuer(issuer: str) -> str:
    return issuer.strip().rstrip("/")


__all__ = [
    "IssuerConfig",
    "JwksKeyResolver",
    "JwtAuthenticator",
    "KeyResolver",
    "claim_path",
    "discover_jwks_uri",
]
