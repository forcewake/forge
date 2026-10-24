"""Closed TOML configuration; secrets are read only from explicit environment names."""

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal, Self
import os
import re
import tomllib

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator

from forge.domain.run_spec import RunPolicy
from forge.domain.value import Value
from forge.domain.plan import RepositoryIdentity
from forge.port_values import Actor, RepoRef
from forge.api.lane_jwks import trusted_https_url
from forge.review.native_values import NativeReviewPolicy

Identifier = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")]
EnvName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")]
NativeId = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Neighbor(Value):
    repository: Identifier
    allowed_globs: tuple[str, ...] = ("**",)


class Repository(Value):
    key: Identifier
    native_id: NativeId
    full_name: Annotated[str, Field(min_length=3, max_length=512)]
    bot_id: NativeId
    bot_login: Annotated[str, Field(min_length=1, max_length=128)]
    approver_ids: Annotated[tuple[NativeId, ...], Field(min_length=1)]
    policy: RunPolicy
    native_review: NativeReviewPolicy | None = None
    neighbors: tuple[Neighbor, ...] = ()
    installation_id: Annotated[int, Field(gt=0)] | None = None
    read_token_env: EnvName | None = None
    write_token_env: EnvName | None = None
    lane_workflow: str = ".github/workflows/forge-lane.yml"
    verification_workflows: tuple[str, ...] = ()
    ci_configuration: str = ".gitlab-ci.yml"

    @field_validator("full_name")
    @classmethod
    def display_path(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", value) or any(
            item in (".", "..") for item in value.split("/")
        ):
            raise ValueError("repository path must contain confined native segments")
        return value


class Connection(Value):
    key: Identifier
    provider: Literal["github", "gitlab"]
    api_url: str
    git_url: str
    oidc_issuer: str
    oidc_jwks_url: str
    tls_ca_path: Path | None = None
    webhook_secret_env: EnvName
    app_issuer: str | None = None
    app_private_key_env: EnvName | None = None
    repositories: Annotated[tuple[Repository, ...], Field(min_length=1, max_length=100)]

    @field_validator("api_url", "git_url", "oidc_issuer", "oidc_jwks_url")
    @classmethod
    def trusted_url(cls, value: str) -> str:
        return trusted_https_url(value)

    @model_validator(mode="after")
    def native_credentials(self) -> Self:
        if self.tls_ca_path is not None and not self.tls_ca_path.is_absolute():
            raise ValueError("native CA path must be an explicit absolute path")
        if self.provider == "github":
            if not self.app_issuer or not self.app_private_key_env:
                raise ValueError("GitHub requires explicit App issuer and signing key")
            if any(
                r.installation_id is None or r.read_token_env or r.write_token_env
                for r in self.repositories
            ):
                raise ValueError("GitHub repositories require only selected App installations")
            if any(r.full_name.count("/") != 1 for r in self.repositories):
                raise ValueError("GitHub repository paths require exactly owner/name")
            if len({(r.bot_id, r.bot_login) for r in self.repositories}) != 1:
                raise ValueError("one GitHub App connection has one configured native bot")
        else:
            if self.app_issuer or self.app_private_key_env or self.api_url != self.git_url:
                raise ValueError("GitLab requires one explicit native host and project credentials")
            if any(
                r.installation_id
                or not r.read_token_env
                or not r.write_token_env
                or not r.bot_login.startswith(f"project_{r.native_id}_bot_")
                for r in self.repositories
            ):
                raise ValueError("GitLab requires separate read/write project bot credentials")
        if len({r.native_id for r in self.repositories}) != len(self.repositories):
            raise ValueError("native repositories must be unique within a connection")
        return self

    def ref(self, repo: Repository) -> RepoRef:
        return RepoRef(
            identity=RepositoryIdentity(
                provider=self.provider, connection_id=self.key, repository_id=repo.native_id
            ),
            full_name=repo.full_name,
        )

    def bot(self, repo: Repository) -> Actor:
        return Actor(
            provider=self.provider,
            connection_id=self.key,
            native_id=repo.bot_id,
            login=repo.bot_login,
            is_bot=True,
        )


class OperatorGrant(Value):
    repository: Identifier
    native_id: NativeId
    login: Annotated[str, Field(min_length=1, max_length=128)]


class Operator(Value):
    key: Identifier
    token_env: EnvName
    grants: Annotated[tuple[OperatorGrant, ...], Field(min_length=1, max_length=100)]


class Gateway(Value):
    url: str
    admin_key_env: EnvName
    policy_path: Path
    artifact_digest: Digest
    settle_seconds: Annotated[int, Field(ge=0, le=3600)] = 30


class RuntimeSettings(Value):
    schema_version: Literal["forge.runtime/1"] = Field(alias="schema")
    database_url_env: EnvName
    secret_key_env: EnvName
    public_url: str
    blob_directory: Path
    lane_requirements_url: str
    lane_requirements_sha256: Digest
    connections: Annotated[tuple[Connection, ...], Field(min_length=1, max_length=64)]
    operators: Annotated[tuple[Operator, ...], Field(min_length=1, max_length=100)]
    gateway: Gateway
    max_active: Annotated[int, Field(gt=0, le=1000)] = 4
    worker_concurrency: Annotated[int, Field(gt=0, le=100)] = 4
    lease_seconds: Annotated[int, Field(ge=15, le=3600)] = 60
    session_ttl_seconds: Annotated[int, Field(ge=30, le=900)] = 300
    oidc_clock_skew_seconds: Annotated[int, Field(ge=0, le=120)] = 30
    oidc_cache_seconds: Annotated[int, Field(ge=10, le=3600)] = 300

    @field_validator("public_url", "lane_requirements_url")
    @classmethod
    def https_url(cls, value: str) -> str:
        return trusted_https_url(value)

    @model_validator(mode="after")
    def scopes(self) -> Self:
        if len({c.key for c in self.connections}) != len(self.connections):
            raise ValueError("connection names must be unique")
        repos = [r for c in self.connections for r in c.repositories]
        keys = {r.key for r in repos}
        if any(r.policy.driver not in ("claude_code", "copilot_cli") for r in repos):
            raise ValueError("runtime supports only the installed first-class drivers")
        if len(keys) != len(repos) or len({o.key for o in self.operators}) != len(self.operators):
            raise ValueError("repository and operator names must be globally unique")
        for repo in repos:
            if any(n.repository not in keys or n.repository == repo.key for n in repo.neighbors):
                raise ValueError("neighbors require another explicitly configured repository")
        for operator in self.operators:
            if any(g.repository not in keys for g in operator.grants) or len(
                {g.repository for g in operator.grants}
            ) != len(operator.grants):
                raise ValueError("operator grants require unique configured repositories")
        return self

    def repositories(self) -> dict[str, tuple[Connection, Repository]]:
        return {r.key: (c, r) for c in self.connections for r in c.repositories}


def secret(name: str, environ: Mapping[str, str], *, minimum: int = 1) -> SecretStr:
    value = environ.get(name, "")
    if not minimum <= len(value.encode()) <= 65536 or "\x00" in value:
        raise ValueError("required configured secret is missing or invalid: " + name)
    return SecretStr(value)


def load(path: Path) -> RuntimeSettings:
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("runtime configuration exceeds its bound")
    try:
        with path.open("rb") as stream:
            body = tomllib.load(stream)
    except (ValueError, UnicodeError):
        raise ValueError("runtime configuration is invalid") from None
    # JSON validation preserves the strict domain value rules while TOML arrays
    # become immutable tuples and decimal policy amounts remain exact text.
    import json

    try:
        settings = RuntimeSettings.model_validate_json(json.dumps(body))
    except (ValidationError, ValueError, TypeError):
        raise ValueError("runtime configuration is invalid") from None
    for value in (settings.blob_directory, settings.gateway.policy_path):
        if not value.is_absolute():
            raise ValueError("runtime data and policy paths must be explicit absolute paths")
    return settings


def environment() -> Mapping[str, str]:
    return os.environ
