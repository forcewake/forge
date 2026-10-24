"""Explicit native connection trust for lane identity; no provider defaults."""

from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator

from forge.domain.value import Value
from forge.api.lane_jwks import trusted_https_url


class OidcAuthority(Value):
    connection_id: Annotated[str, Field(min_length=1, max_length=64)]
    provider: Literal["github", "gitlab"]
    issuer: str
    jwks_url: str

    @field_validator("issuer", "jwks_url")
    @classmethod
    def https_url(cls, value: str) -> str:
        return trusted_https_url(value)


class LaneAuthSettings(Value):
    secret_key: SecretStr
    public_url: str
    authorities: Annotated[tuple[OidcAuthority, ...], Field(min_length=1, max_length=64)]
    session_ttl_seconds: Annotated[int, Field(ge=30, le=900)] = 300
    clock_skew_seconds: Annotated[int, Field(ge=0, le=120)] = 30
    jwks_cache_seconds: Annotated[int, Field(ge=10, le=3600)] = 300

    @field_validator("public_url")
    @classmethod
    def https_url(cls, value: str) -> str:
        return trusted_https_url(value)

    @model_validator(mode="after")
    def trust_roots(self) -> Self:
        if len(self.secret_key.get_secret_value().encode()) < 32:
            raise ValueError("lane signing key must contain at least 32 bytes")
        if len({a.connection_id for a in self.authorities}) != len(self.authorities):
            raise ValueError("each lane connection requires one explicit OIDC authority")
        return self
