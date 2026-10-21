from __future__ import annotations

import pytest

from multicloud_ai_agent_gateway.auth import Principal, TenantPolicy, TenantRule


def _p(tenant: str | None, subject: str = "u") -> Principal:
    return Principal(subject=subject, tenant=tenant)


def test_empty_policy_is_none() -> None:
    assert TenantPolicy.from_json("") is None
    assert TenantPolicy.from_json("{}") is None
    assert TenantPolicy.from_json(None) is None


def test_rules_per_tenant_and_wildcard() -> None:
    policy = TenantPolicy.from_json(
        '{"t1": {"agents": ["A"]}, "platform": {"agents": ["*"]}, "*": {"agents": []}}'
    )
    assert policy is not None
    assert policy.is_allowed(_p("t1"), "A")
    assert not policy.is_allowed(_p("t1"), "B")
    assert policy.allowed_agents(_p("t1")) == frozenset({"A"})
    assert policy.allowed_agents(_p("platform")) is None  # all
    assert policy.is_allowed(_p("platform"), "Anything")
    # Unknown tenant falls to the wildcard, which allows nothing here.
    assert policy.allowed_agents(_p("t9")) == frozenset()
    assert not policy.is_allowed(_p("t9"), "A")


def test_fail_closed_without_wildcard() -> None:
    policy = TenantPolicy.from_json('{"t1": {"agents": ["A"]}}')
    assert policy is not None
    assert policy.rule_for(_p("t2")) is None
    assert not policy.is_allowed(_p("t2"), "A")
    assert policy.allowed_agents(_p(None)) == frozenset()


def test_anonymous_uses_wildcard() -> None:
    policy = TenantPolicy.from_json('{"*": {"agents": ["A"]}}')
    assert policy is not None
    anonymous = Principal.anonymous_principal()
    assert policy.is_allowed(anonymous, "A")
    assert not policy.is_allowed(anonymous, "B")


def test_null_rule_means_all() -> None:
    policy = TenantPolicy.from_json('{"t1": null}')
    assert policy is not None
    assert policy.allowed_agents(_p("t1")) is None


def test_invalid_shapes_raise() -> None:
    with pytest.raises(ValueError):
        TenantPolicy.from_json("[]")
    with pytest.raises(ValueError):
        TenantPolicy.from_json('{"t1": "A"}')
    with pytest.raises(ValueError):
        TenantPolicy.from_json('{"t1": {"agents": "A"}}')
    with pytest.raises(ValueError):
        TenantPolicy.from_json('{" ": {"agents": ["A"]}}')


def test_rule_from_agents_strips_and_dedupes() -> None:
    rule = TenantRule.from_agents([" A ", "A", "", "B"])
    assert rule.agents == frozenset({"A", "B"})
    assert TenantRule.from_agents(["*", "A"]).agents is None
