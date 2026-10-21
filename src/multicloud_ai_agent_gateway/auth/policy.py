"""Tenant policy: the one thing about tenants the gateway owns.

Identity comes from the token (see ``jwt_auth.py``). This module maps a
tenant to what it may do. Version one is an allow-list of agents per tenant
with a ``*`` wildcard rule; it is loaded from ``GATEWAY_TENANT_POLICY_JSON``::

    {
      "00000000-0000-0000-0000-00000000000a": {"agents": ["AzureFinOpsAgent"]},
      "platform":                             {"agents": ["*"]},
      "*":                                    {"agents": []}
    }

Semantics:

* no policy configured: every caller may use every agent (today's behaviour);
* a policy is configured and the caller's tenant has a rule: that rule applies;
* otherwise the ``*`` rule applies if present;
* otherwise the caller is denied everything. Fail closed, because a tenant
  that is missing from the policy is an operator mistake, not a guest.

``AutoRouter`` is never listed; it is usable whenever at least one concrete
agent is allowed, and only routes among the allowed ones.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from multicloud_ai_agent_gateway.auth.principal import Principal

WILDCARD = "*"


@dataclass(frozen=True)
class TenantRule:
    """``agents`` is ``None`` for "all agents"."""

    agents: frozenset[str] | None = None

    @classmethod
    def from_agents(cls, agents: Iterable[str] | None) -> "TenantRule":
        if agents is None:
            return cls(agents=None)
        names = {str(a).strip() for a in agents if str(a).strip()}
        if WILDCARD in names:
            return cls(agents=None)
        return cls(agents=frozenset(names))

    def allows(self, agent: str) -> bool:
        return self.agents is None or agent in self.agents


class TenantPolicy:
    def __init__(self, rules: Mapping[str, TenantRule]) -> None:
        self._rules: dict[str, TenantRule] = dict(rules)

    @classmethod
    def from_json(cls, text: str | None) -> "TenantPolicy | None":
        """``None`` when the policy is empty, so callers can skip enforcement."""
        raw = json.loads(text) if text and text.strip() else {}
        if not isinstance(raw, dict):
            raise ValueError("tenant policy must be a JSON object keyed by tenant")
        if not raw:
            return None
        rules: dict[str, TenantRule] = {}
        for tenant, body in raw.items():
            key = str(tenant).strip()
            if not key:
                raise ValueError("tenant policy has an empty tenant key")
            if body is None:
                rules[key] = TenantRule.from_agents(None)
                continue
            if not isinstance(body, dict):
                raise ValueError(f"tenant policy rule for {key!r} must be an object")
            agents = body.get("agents")
            if agents is not None and not isinstance(agents, list):
                raise ValueError(f"tenant policy rule for {key!r}: 'agents' must be a list")
            rules[key] = TenantRule.from_agents(agents)
        return cls(rules)

    @property
    def rules(self) -> dict[str, TenantRule]:
        return dict(self._rules)

    def rule_for(self, principal: Principal) -> TenantRule | None:
        if principal.tenant is not None and principal.tenant in self._rules:
            return self._rules[principal.tenant]
        return self._rules.get(WILDCARD)

    def allowed_agents(self, principal: Principal) -> frozenset[str] | None:
        """``None`` means every agent; an empty set means nothing."""
        rule = self.rule_for(principal)
        if rule is None:
            return frozenset()
        return rule.agents

    def is_allowed(self, principal: Principal, agent: str) -> bool:
        rule = self.rule_for(principal)
        return rule is not None and rule.allows(agent)


__all__ = ["TenantPolicy", "TenantRule", "WILDCARD"]
