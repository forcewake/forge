"""OIDC exchange and short-lived bearer sessions for the lane protocol."""

from __future__ import annotations

import hmac
import secrets
from datetime import datetime, timezone
from typing import Any, Literal

import jwt
from forge_protocol import Brief, LaneDispatch, RunIdentity, SessionRequest, SessionToken
from pydantic import ValidationError

from forge.api.lane_auth_values import (
    LaneAuthError,
    LaneAuthStore,
    PendingDispatch,
    identity_of,
    replay_hash,
)
from forge.api.lane_jwks import JwksCache
from forge.api.lane_settings import LaneAuthSettings

_SESSION_TYPE = "forge-lane-session+jwt"
_DATES = ("exp", "iat", "nbf")


class LaneAuthService:
    def __init__(
        self,
        store: LaneAuthStore,
        settings: LaneAuthSettings,
        *,
        jwks_cache: JwksCache | None = None,
    ):
        self.store = store
        self._secret = settings.secret_key.get_secret_value()
        self.public_url = settings.public_url
        self.ttl, self.skew = settings.session_ttl_seconds, settings.clock_skew_seconds
        self.authorities = {a.connection_id: a for a in settings.authorities}
        roots = {key: (value.issuer, value.jwks_url) for key, value in self.authorities.items()}
        ca_paths = {
            key: value.tls_ca_path
            for key, value in self.authorities.items()
            if value.tls_ca_path is not None
        }
        self.jwks = jwks_cache or JwksCache(
            roots, ttl=settings.jwks_cache_seconds, ca_paths=ca_paths
        )
        if self.jwks.authorities != roots or self.jwks.ca_paths != ca_paths:
            raise ValueError("OIDC cache authorities differ from server configuration")

    @staticmethod
    def new_nonce() -> str:
        return secrets.token_urlsafe(32)

    def model_key_encryption_key(self) -> bytes:
        return hmac.digest(
            self._secret.encode(), b"forge-lane/v1/model-key-vault/AEAD/v1", "sha256"
        )

    async def register_pending(
        self,
        dispatch: LaneDispatch,
        brief: Brief,
        *,
        expires_at: datetime,
        connection_id: str,
        github_workflow_ref: str | None = None,
        github_workflow_claim: Literal["workflow_ref", "job_workflow_ref"] = "job_workflow_ref",
        gitlab_pipeline_id: str | None = None,
    ) -> PendingDispatch:
        """Trusted dispatch composition only; this operation is not an HTTP endpoint."""
        authority = self.authorities.get(connection_id)
        if authority is None or authority.provider != dispatch.provider:
            raise LaneAuthError("dispatch connection has no matching configured OIDC authority")
        if (
            identity_of(dispatch) != identity_of(brief)
            or dispatch.approved_digest != brief.approved_digest
        ):
            raise LaneAuthError("dispatch and approved brief do not match")
        if (
            dispatch.base_sha != brief.base_sha
            or str(dispatch.server_url).rstrip("/") != self.public_url
        ):
            raise LaneAuthError("dispatch target or base does not match approved brief")
        if expires_at.tzinfo is None or expires_at <= datetime.now(timezone.utc):
            raise LaneAuthError("dispatch expiry must be in the future")
        if dispatch.provider == "github" and (
            not github_workflow_ref or len(dispatch.nonce or "") < 32
        ):
            raise LaneAuthError("GitHub dispatch requires a pinned workflow and random nonce")
        if dispatch.provider == "gitlab" and (not gitlab_pipeline_id):
            raise LaneAuthError("GitLab dispatch requires a configured issuer and pipeline id")
        caller_ref = dispatch.ref
        if dispatch.provider == "github":
            if caller_ref.startswith("refs/"):
                if not caller_ref.startswith(("refs/heads/", "refs/tags/")):
                    raise LaneAuthError("GitHub caller ref must be a branch or explicit tag")
            else:
                caller_ref = "refs/heads/" + caller_ref
        return await self.store.register(
            PendingDispatch(
                dispatch=dispatch,
                brief=brief,
                connection_id=connection_id,
                expires_at=expires_at,
                cancellation_generation=0,
                github_workflow_ref=github_workflow_ref,
                github_caller_ref=caller_ref if dispatch.provider == "github" else None,
                github_workflow_claim=github_workflow_claim,
                gitlab_pipeline_id=gitlab_pipeline_id,
            )
        )

    async def exchange(self, request: SessionRequest) -> SessionToken:
        identity = identity_of(request)
        pending = await self.store.load(identity)
        if request.provider != pending.dispatch.provider:
            raise LaneAuthError("OIDC provider does not match dispatch")
        claims = await self._oidc_claims(request.oidc_token, pending)
        binding = self._binding(claims, pending)
        await self.store.claim(
            identity,
            jti_hash=replay_hash(claims["jti"]),
            expires_at=claims["exp"] + self.skew,
            binding=binding,
        )
        now = int(datetime.now(timezone.utc).timestamp())
        claims = {
            **identity.model_dump(),
            "iss": self.public_url,
            "aud": self.public_url + "/lane/v1",
            "sub": f"{identity.forge_run_id}:{identity.attempt}:{identity.generation}",
            "token_use": "lane_session",
            "iat": now,
            "nbf": now,
            "exp": now + self.ttl,
            "jti": secrets.token_urlsafe(24),
        }
        token = jwt.encode(claims, self._secret, algorithm="HS256", headers={"typ": _SESSION_TYPE})
        return SessionToken(
            **identity.model_dump(),
            access_token=token,
            expires_at=datetime.fromtimestamp(now + self.ttl, timezone.utc),
        )

    async def _oidc_claims(self, token: str, pending: PendingDispatch) -> dict[str, Any]:
        header = _header(token, "RS256")
        kid = header.get("kid")
        if not isinstance(kid, str) or not 1 <= len(kid) <= 256:
            raise LaneAuthError("OIDC token requires a signing key id")
        provider = pending.dispatch.provider
        authority = self.authorities.get(pending.connection_id)
        if authority is None or authority.provider != provider:
            raise LaneAuthError("lane connection has no matching configured OIDC authority")
        key = await self.jwks.key(pending.connection_id, kid)
        audience = self.public_url
        if provider == "github":
            audience += "/lane/" + str(pending.dispatch.nonce)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=authority.issuer,
                audience=audience,
                leeway=self.skew,
                options={
                    "require": ["exp", "iat", "nbf", "jti", "sub", "iss", "aud"],
                    "strict_aud": True,
                },
            )
            _numeric_dates(claims)
            if not isinstance(claims["jti"], str) or not 1 <= len(claims["jti"]) <= 256:
                raise LaneAuthError("OIDC token requires a bounded unique id")
            if claims["exp"] - claims["iat"] > 86400:
                raise LaneAuthError("OIDC token lifetime is too long")
            return claims
        except (jwt.PyJWTError, ValueError, TypeError):
            raise LaneAuthError("OIDC token is invalid") from None

    @staticmethod
    def _binding(claims: dict[str, Any], pending: PendingDispatch) -> dict[str, str]:
        if (
            pending.repository_id is not None
            and _provider_id(
                claims, "repository_id" if pending.dispatch.provider == "github" else "project_id"
            )
            != pending.repository_id
        ):
            raise LaneAuthError("OIDC native repository identity does not match dispatch")
        if pending.dispatch.provider == "github":
            if (
                claims.get("repository") != pending.dispatch.repository
                or claims.get(pending.github_workflow_claim) != pending.github_workflow_ref
                or claims.get("ref") != pending.github_caller_ref
            ):
                raise LaneAuthError(
                    "OIDC repository, caller ref or workflow does not match dispatch"
                )
            return {
                "provider_run_id": _provider_id(claims, "run_id"),
                "provider_attempt": _provider_id(claims, "run_attempt"),
            }
        if (
            _provider_id(claims, "pipeline_id") != pending.gitlab_pipeline_id
            or claims.get("project_path") != pending.dispatch.repository
            or claims.get("ref") != pending.dispatch.ref
        ):
            raise LaneAuthError("OIDC project, ref or pipeline does not match dispatch")
        return {"gitlab_job_id": _provider_id(claims, "job_id")}

    async def authenticate_bearer(self, authorization: str) -> RunIdentity:
        if not authorization.startswith("Bearer "):
            raise LaneAuthError("lane bearer token is required")
        token = authorization[7:]
        if _header(token, "HS256").get("typ") != _SESSION_TYPE:
            raise LaneAuthError("wrong lane token type")
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                issuer=self.public_url,
                audience=self.public_url + "/lane/v1",
                options={"require": ["exp", "iat", "nbf", "sub", "jti"], "strict_aud": True},
            )
            _numeric_dates(claims)
            identity = RunIdentity.model_validate(
                {
                    name: claims[name]
                    for name in (
                        "protocol",
                        "forge_run_id",
                        "attempt",
                        "generation",
                    )
                }
            )
            if (
                claims.get("token_use") != "lane_session"
                or claims["sub"]
                != f"{identity.forge_run_id}:{identity.attempt}:{identity.generation}"
            ):
                raise LaneAuthError("wrong lane session binding")
            if claims["exp"] - claims["iat"] > self.ttl:
                raise LaneAuthError("lane session lifetime is too long")
        except (jwt.PyJWTError, ValidationError, KeyError, TypeError, ValueError):
            raise LaneAuthError("lane bearer token is invalid") from None
        await self.store.load(identity)
        return identity


def _header(token: str, algorithm: str) -> dict[str, Any]:
    if len(token) > 32768:
        raise LaneAuthError("lane token is too large")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise LaneAuthError("lane token header is invalid") from None
    if header.get("alg") != algorithm or any(
        name in header for name in ("jku", "x5u", "jwk", "crit")
    ):
        raise LaneAuthError("lane token uses an unsupported signing header")
    return header


def _numeric_dates(claims: dict[str, Any]) -> None:
    if (
        any(type(claims.get(name)) is not int for name in _DATES)
        or claims["exp"] <= claims["iat"]
        or claims["nbf"] > claims["exp"]
    ):
        raise LaneAuthError("lane token dates are invalid")


def _provider_id(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    text = str(value)
    if (
        type(value) not in (str, int)
        or not text.isascii()
        or not text.isdigit()
        or len(text) > 32
        or int(text) < 1
    ):
        raise LaneAuthError("OIDC provider job identity is missing")
    return text
