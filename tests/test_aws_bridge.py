from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from multicloud_ai_agent_gateway.auth.aws_bridge import AwsBridgeCredentials, AwsBridgeError


@dataclass
class _Token:
    token: str
    expires_on: int = 0


@dataclass
class _FakeAadCredential:
    token_value: str = "aad-token-1"
    raises: Exception | None = None
    calls: int = 0

    def get_token(self, *scopes: str) -> _Token:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return _Token(token=f"{self.token_value}-{self.calls}")

    def close(self) -> None:
        pass


@dataclass
class _FakeStsClient:
    creds_chain: list[dict[str, Any]] = field(default_factory=list)
    raises: Exception | None = None
    received_calls: list[dict[str, Any]] = field(default_factory=list)

    def assume_role_with_web_identity(self, **kwargs: Any) -> dict[str, Any]:
        self.received_calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        if not self.creds_chain:
            raise AssertionError("no canned credentials left")
        return {"Credentials": self.creds_chain.pop(0)}


def _creds(expires_at: datetime, key: str = "AKIA-EXAMPLE") -> dict[str, Any]:
    return {
        "AccessKeyId": key,
        "SecretAccessKey": "secret",
        "SessionToken": "session",
        "Expiration": expires_at,
    }


def _build_bridge(
    *,
    aad: _FakeAadCredential,
    sts: _FakeStsClient,
    now: datetime,
    margin: int = 60,
) -> tuple[AwsBridgeCredentials, list[datetime]]:
    clock_calls: list[datetime] = []

    def clock() -> datetime:
        clock_calls.append(now)
        return now

    bridge = AwsBridgeCredentials(
        aad_credential=aad,
        scope="api://fake-app/.default",
        role_arn="arn:aws:iam::123:role/r",
        region="us-west-2",
        refresh_safety_margin_seconds=margin,
        sts_client_factory=lambda region: sts,
        clock=clock,
    )
    return bridge, clock_calls


@pytest.mark.asyncio
async def test_first_call_triggers_refresh_and_returns_creds() -> None:
    now = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
    aad = _FakeAadCredential()
    sts = _FakeStsClient(creds_chain=[_creds(now + timedelta(hours=1))])
    bridge, _ = _build_bridge(aad=aad, sts=sts, now=now)

    creds = await bridge.get_credentials()

    assert creds["AccessKeyId"] == "AKIA-EXAMPLE"
    assert aad.calls == 1
    assert sts.received_calls == [
        {
            "RoleArn": "arn:aws:iam::123:role/r",
            "RoleSessionName": "multicloud-ai-agent-gateway",
            "WebIdentityToken": "aad-token-1-1",
        }
    ]


@pytest.mark.asyncio
async def test_subsequent_call_within_margin_uses_cache() -> None:
    now = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
    aad = _FakeAadCredential()
    sts = _FakeStsClient(creds_chain=[_creds(now + timedelta(hours=1))])
    bridge, _ = _build_bridge(aad=aad, sts=sts, now=now)

    await bridge.get_credentials()
    await bridge.get_credentials()
    await bridge.get_credentials()

    assert aad.calls == 1
    assert len(sts.received_calls) == 1


@pytest.mark.asyncio
async def test_refreshes_when_inside_safety_margin() -> None:
    now = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
    aad = _FakeAadCredential()
    sts = _FakeStsClient(
        creds_chain=[
            _creds(now + timedelta(seconds=30), key="AKIA-FIRST"),
            _creds(now + timedelta(hours=1), key="AKIA-SECOND"),
        ]
    )
    bridge, _ = _build_bridge(aad=aad, sts=sts, now=now, margin=60)

    first = await bridge.get_credentials()
    second = await bridge.get_credentials()

    assert first["AccessKeyId"] == "AKIA-FIRST"
    assert second["AccessKeyId"] == "AKIA-SECOND"
    assert aad.calls == 2
    assert len(sts.received_calls) == 2


@pytest.mark.asyncio
async def test_aad_failure_is_wrapped() -> None:
    aad = _FakeAadCredential(raises=RuntimeError("aad-down"))
    sts = _FakeStsClient(creds_chain=[])
    bridge, _ = _build_bridge(
        aad=aad,
        sts=sts,
        now=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(AwsBridgeError, match="AAD"):
        await bridge.get_credentials()


@pytest.mark.asyncio
async def test_sts_failure_is_wrapped() -> None:
    aad = _FakeAadCredential()
    sts = _FakeStsClient(raises=RuntimeError("AccessDenied"))
    bridge, _ = _build_bridge(
        aad=aad,
        sts=sts,
        now=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(AwsBridgeError, match="AssumeRoleWithWebIdentity"):
        await bridge.get_credentials()


@pytest.mark.asyncio
async def test_naive_expiration_is_treated_as_utc() -> None:
    now = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
    naive = (now + timedelta(hours=1)).replace(tzinfo=None)
    aad = _FakeAadCredential()
    sts = _FakeStsClient(
        creds_chain=[
            {
                "AccessKeyId": "k",
                "SecretAccessKey": "s",
                "SessionToken": "t",
                "Expiration": naive,
            }
        ]
    )
    bridge, _ = _build_bridge(aad=aad, sts=sts, now=now)

    await bridge.get_credentials()
    await bridge.get_credentials()  # would fail with TZ comparison error if naive leaked through

    assert aad.calls == 1


@pytest.mark.asyncio
async def test_constructor_validates_required_fields() -> None:
    aad = _FakeAadCredential()
    with pytest.raises(ValueError, match="scope"):
        AwsBridgeCredentials(aad_credential=aad, scope="", role_arn="x", region="r")
    with pytest.raises(ValueError, match="role_arn"):
        AwsBridgeCredentials(aad_credential=aad, scope="s", role_arn="", region="r")
    with pytest.raises(ValueError, match="region"):
        AwsBridgeCredentials(aad_credential=aad, scope="s", role_arn="r", region="")


@pytest.mark.asyncio
async def test_aclose_invokes_underlying_close() -> None:
    aad = _FakeAadCredential()
    closes: list[int] = []

    def closer() -> None:
        closes.append(1)

    aad.close = closer  # type: ignore[method-assign]
    bridge = AwsBridgeCredentials(
        aad_credential=aad,
        scope="s",
        role_arn="r",
        region="us-west-2",
        sts_client_factory=lambda _r: _FakeStsClient(),
    )
    await bridge.aclose()
    assert closes == [1]
