"""Create native adapters from explicit server configuration without network I/O."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

import httpx
import ssl

from forge.ports import Clock
from forge.providers.github.auth import AppAuth
from forge.providers.github.client import API as GitHubAPI
from forge.providers.github.transport import (
    Endpoint as GitHubEndpoint,
    Transport as GitHubTransport,
)
from forge.providers.github.actor import GitHubActor
from forge.providers.github.subject import GitHubSubject
from forge.providers.github.repo import GitHubRepo
from forge.providers.github.review import GitHubReview
from forge.providers.github.ci import GitHubCI
from forge.providers.github.write import GitHubWrites
from forge.providers.github.commit import GitHubCommitter
from forge.providers.github.ingress import GitHubIngress
from forge.providers.gitlab.client import API as GitLabAPI, Credentials
from forge.providers.gitlab.transport import (
    Endpoint as GitLabEndpoint,
    Transport as GitLabTransport,
)
from forge.providers.gitlab.actor import GitLabActor
from forge.providers.gitlab.subject import GitLabSubject
from forge.providers.gitlab.repo import GitLabRepo
from forge.providers.gitlab.review import GitLabReview
from forge.providers.gitlab.ci import GitLabCI
from forge.providers.gitlab.write import GitLabWrites
from forge.providers.gitlab.commit import GitLabCommitter
from forge.providers.gitlab.ingress import GitLabIngress
from forge.store.effects import EffectStore
from forge.runtime.routing import NativePorts, Registry
from forge.runtime.settings import Connection, Repository, RuntimeSettings, secret


@dataclass(frozen=True)
class Connections:
    registry: Registry
    ingress: Mapping[str, GitHubIngress | GitLabIngress]
    close: tuple[Callable[[], Awaitable[None]], ...]


def github(
    connection: Connection,
    repository: Repository,
    config: RuntimeSettings,
    environ: Mapping[str, str],
    clock: Clock,
    effects: EffectStore,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[NativePorts, GitHubTransport]:
    http = GitHubTransport(
        GitHubEndpoint(connection.api_url, connection.git_url), transport=transport
    )
    auth = AppAuth(
        connection.key,
        str(connection.app_issuer),
        secret(str(connection.app_private_key_env), environ).get_secret_value(),
        {repository.native_id: int(repository.installation_id or 0)},
        clock,
        http,
    )
    api = GitHubAPI(auth, http)
    bot, ref = connection.bot(repository), connection.ref(repository)
    repo = GitHubRepo(api)
    review = GitHubReview(api, bot=bot)
    ci = GitHubCI(
        api,
        clock,
        dispatch_lookup=effects.dispatch_for_nonce,
        lane_workflow_path=repository.lane_workflow,
        verification_workflow_paths=repository.verification_workflows,
    )
    committer = GitHubCommitter(api, auth, repo, clock, bot=bot)
    writes = GitHubWrites(
        api,
        bot=bot,
        repositories=repo,
        reviews=review,
        ci=ci,
        committer=committer,
        requirements_url=config.lane_requirements_url,
        requirements_sha256=config.lane_requirements_sha256,
    )
    return NativePorts(
        ref,
        GitHubSubject(api, bot=bot),
        repo,
        review,
        ci,
        GitHubActor(api, bot=bot, approver_ids=repository.approver_ids),
        writes,
    ), http


def gitlab(
    connection: Connection,
    repository: Repository,
    config: RuntimeSettings,
    environ: Mapping[str, str],
    clock: Clock,
    effects: EffectStore,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[NativePorts, GitLabTransport]:
    http = GitLabTransport(GitLabEndpoint(connection.api_url), transport=transport)
    bot, ref = connection.bot(repository), connection.ref(repository)
    credentials = Credentials(
        secret(str(repository.read_token_env), environ),
        secret(str(repository.write_token_env), environ),
        bot,
    )
    api = GitLabAPI(connection.key, http, {repository.native_id: credentials})
    repo = GitLabRepo(api)
    review = GitLabReview(api)
    ci = GitLabCI(
        api,
        clock,
        dispatch_lookup=effects.dispatch_for_nonce,
        configuration_path=repository.ci_configuration,
    )
    committer = GitLabCommitter(api, repo, clock)
    writes = GitLabWrites(
        api,
        repositories=repo,
        reviews=review,
        ci=ci,
        committer=committer,
        requirements_url=config.lane_requirements_url,
        requirements_sha256=config.lane_requirements_sha256,
    )
    return NativePorts(
        ref,
        GitLabSubject(api),
        repo,
        review,
        ci,
        GitLabActor(api, bot=bot, approver_ids=repository.approver_ids),
        writes,
    ), http


def assemble_connections(
    config: RuntimeSettings,
    environ: Mapping[str, str],
    clock: Clock,
    effects: EffectStore,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Connections:
    entries: dict[str, NativePorts] = {}
    hooks: dict[str, GitHubIngress | GitLabIngress] = {}
    closers: list[Callable[[], Awaitable[None]]] = []
    for connection in config.connections:
        webhook = (
            secret(connection.webhook_secret_env, environ, minimum=32).get_secret_value().encode()
        )
        for repository in connection.repositories:
            selected = transport
            if selected is None and connection.tls_ca_path is not None:
                selected = httpx.AsyncHTTPTransport(
                    verify=ssl.create_default_context(cafile=str(connection.tls_ca_path))
                )
            maker = github if connection.provider == "github" else gitlab
            native, http = maker(connection, repository, config, environ, clock, effects, selected)
            entries[repository.key] = native
            closers.append(http.close)
        if connection.provider == "github":
            hooks[connection.key] = GitHubIngress(
                connection.key,
                webhook,
                repositories={
                    r.native_id: int(r.installation_id or 0) for r in connection.repositories
                },
                bot=connection.bot(connection.repositories[0]),
            )
        else:
            hooks[connection.key] = GitLabIngress(
                connection.key,
                webhook,
                bots={r.native_id: connection.bot(r) for r in connection.repositories},
            )
    return Connections(Registry(entries), hooks, tuple(closers))
