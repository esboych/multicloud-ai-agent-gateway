"""Microsoft Entra ID -> AWS STS bridge.

Exchanges an AAD v2.0 access token (obtained via Workload Identity federation)
for short-lived AWS credentials via sts:AssumeRoleWithWebIdentity, and caches
them until shortly before expiry.

Designed to be imported only when an AgentCore agent is configured; the boto3
import lives inside the bridge methods so single-cloud (Foundry-only)
deployments don't pay the boto3 startup cost.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol, runtime_checkable

from multicloud_ai_agent_gateway.core.backend import GatewayError

log = logging.getLogger(__name__)


class AwsBridgeError(GatewayError):
    """Raised when the AAD->AWS exchange fails."""


@runtime_checkable
class _TokenCredential(Protocol):
    """Minimal subset of azure.core.credentials.TokenCredential we depend on."""

    def get_token(self, *scopes: str) -> Any: ...


# A factory that returns a boto3 STS client. Production wiring uses the default;
# tests inject one bound to a mock or a botocore Stubber.
StsClientFactory = Callable[[str], Any]


def _default_sts_client_factory(region: str) -> Any:
    import boto3

    return boto3.client("sts", region_name=region)


class AwsBridgeCredentials:
    """Caches STS credentials from sts:AssumeRoleWithWebIdentity.

    The cache is refreshed automatically when the cached credentials are within
    ``refresh_safety_margin_seconds`` of expiry. Refreshes are serialized so two
    concurrent invokes never trigger two parallel exchanges.
    """

    def __init__(
        self,
        *,
        aad_credential: _TokenCredential,
        scope: str,
        role_arn: str,
        region: str,
        session_name: str = "multicloud-ai-agent-gateway",
        refresh_safety_margin_seconds: int = 60,
        sts_client_factory: StsClientFactory = _default_sts_client_factory,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not scope:
            raise ValueError("scope is required")
        if not role_arn:
            raise ValueError("role_arn is required")
        if not region:
            raise ValueError("region is required")

        self._aad_credential = aad_credential
        self._scope = scope
        self._role_arn = role_arn
        self._region = region
        self._session_name = session_name
        self._margin = timedelta(seconds=refresh_safety_margin_seconds)
        self._sts_client_factory = sts_client_factory
        self._now = clock

        self._lock = asyncio.Lock()
        self._creds: dict[str, Any] | None = None
        self._expires_at: datetime | None = None

    @property
    def region(self) -> str:
        return self._region

    async def get_credentials(self) -> dict[str, Any]:
        """Return a credentials dict valid for the AWS SDK.

        Keys: AccessKeyId, SecretAccessKey, SessionToken, Expiration (datetime).
        """
        async with self._lock:
            if self._needs_refresh():
                await asyncio.to_thread(self._refresh_sync)
            assert self._creds is not None  # for type checkers
            return dict(self._creds)

    def _needs_refresh(self) -> bool:
        if self._creds is None or self._expires_at is None:
            return True
        return self._now() >= (self._expires_at - self._margin)

    def _refresh_sync(self) -> None:
        try:
            access_token = self._aad_credential.get_token(self._scope).token
        except Exception as exc:  # noqa: BLE001 - propagate as bridge error
            raise AwsBridgeError(f"Failed to acquire AAD token for AWS bridge: {exc}") from exc

        sts = self._sts_client_factory(self._region)
        try:
            response = sts.assume_role_with_web_identity(
                RoleArn=self._role_arn,
                RoleSessionName=self._session_name,
                WebIdentityToken=access_token,
            )
        except Exception as exc:  # noqa: BLE001 - boto3 raises ClientError, etc.
            raise AwsBridgeError(f"sts:AssumeRoleWithWebIdentity failed: {exc}") from exc

        creds = response.get("Credentials")
        if not creds:
            raise AwsBridgeError("AssumeRoleWithWebIdentity returned no Credentials")

        expiration = creds.get("Expiration")
        if expiration is None:
            raise AwsBridgeError("AssumeRoleWithWebIdentity response missing Expiration")
        if expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=timezone.utc)

        self._creds = creds
        self._expires_at = expiration
        log.info(
            "Refreshed AWS STS credentials (role=%s, expires=%s)",
            self._role_arn,
            expiration.isoformat(),
        )

    async def aclose(self) -> None:
        close = getattr(self._aad_credential, "close", None)
        if callable(close):
            try:
                await asyncio.to_thread(close)
            except Exception:  # noqa: BLE001 - best-effort
                log.exception("Error closing AAD credential.")
