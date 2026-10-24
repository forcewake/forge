"""Operator credentials resolve trusted per-repository native human identities."""

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import hmac

from forge.port_values import Actor
from forge.runtime.settings import RuntimeSettings, secret


class NotAuthorized(ValueError):
    pass


@dataclass(frozen=True)
class Principal:
    key: str
    actors: Mapping[str, Actor]

    def actor(self, repository: str) -> Actor:
        actor = self.actors.get(repository)
        if actor is None:
            raise NotAuthorized("repository access refused")
        return actor


class Operators:
    def __init__(self, config: RuntimeSettings, environ: Mapping[str, str]) -> None:
        repositories = config.repositories()
        credentials: list[tuple[bytes, Principal]] = []
        for operator in config.operators:
            token = secret(operator.token_env, environ, minimum=32).get_secret_value()
            if any(ord(char) < 33 or ord(char) > 126 for char in token):
                raise ValueError("operator credential must be an opaque ASCII token")
            actors = {}
            for grant in operator.grants:
                connection, repository = repositories[grant.repository]
                if (
                    grant.native_id == repository.bot_id
                    or grant.login.casefold() == repository.bot_login.casefold()
                ):
                    raise ValueError("operator credentials cannot impersonate the configured bot")
                actors[grant.repository] = Actor(
                    provider=connection.provider,
                    connection_id=connection.key,
                    native_id=grant.native_id,
                    login=grant.login,
                    is_bot=False,
                )
            credentials.append(
                (hashlib.sha256(token.encode()).digest(), Principal(operator.key, actors))
            )
        if len({value[0] for value in credentials}) != len(credentials):
            raise ValueError("operator credentials must be unique")
        self._credentials = tuple(credentials)

    def authenticate(self, authorization: str) -> Principal:
        if not authorization.startswith("Bearer ") or len(authorization) > 65543:
            raise NotAuthorized("operator bearer credential required")
        token = authorization[7:]
        digest = hashlib.sha256(token.encode()).digest()
        matches = [
            principal
            for expected, principal in self._credentials
            if hmac.compare_digest(expected, digest)
        ]
        if len(matches) != 1:
            raise NotAuthorized("operator credential refused")
        return matches[0]
