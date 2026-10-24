"""Explicit test connection trust without importing the retired lane store."""

import pytest
from pydantic import SecretStr

from forge.api.lane_settings import LaneAuthSettings, OidcAuthority


@pytest.fixture
def settings(env):
    identity = env.intent.task.subject.repo.identity
    issuer = (
        "https://token.actions.githubusercontent.com"
        if identity.provider == "github"
        else "https://gitlab.example"
    )
    return LaneAuthSettings(
        secret_key=SecretStr("test-session-signing-key-32-bytes-long"),
        public_url="https://forge.example",
        authorities=(
            OidcAuthority(
                connection_id=identity.connection_id,
                provider=identity.provider,
                issuer=issuer,
                jwks_url=(
                    issuer + "/.well-known/jwks"
                    if identity.provider == "github"
                    else issuer + "/oauth/discovery/keys"
                ),
            ),
        ),
    )
