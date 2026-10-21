"""Who is calling the gateway.

A ``Principal`` is produced by an authenticator (see ``jwt_auth.py``) from a
verified token and travels with the request through the service layer. The
gateway never stores tenant identity itself: ``tenant`` is whatever claim the
issuer put in the token, and the policy layer (``policy.py``) maps it to what
the caller may do.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant: str | None = None
    issuer: str | None = None
    scopes: tuple[str, ...] = ()
    claims: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)
    anonymous: bool = False

    @classmethod
    def anonymous_principal(cls) -> "Principal":
        """Used when the gateway runs without authentication (the default)."""
        return cls(subject="anonymous", anonymous=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "tenant": self.tenant,
            "issuer": self.issuer,
            "scopes": list(self.scopes),
            "anonymous": self.anonymous,
        }


__all__ = ["Principal"]
