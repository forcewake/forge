"""A lane token can use only the pending run's explicit native connection root."""

from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
from pydantic import SecretStr
import pytest

from forge.api.lane_auth import LaneAuthService
from forge.api.lane_auth_values import LaneAuthError, PendingDispatch
from forge.api.lane_jwks import JwksCache
from forge.api.lane_settings import LaneAuthSettings, OidcAuthority
from forge.domain.approved_input import ApprovedInput
from forge_protocol import LaneDispatch, RunIdentity
from tests.test_domain_approved_input import spec


class Store:
    def __init__(self):
        self.registered = []

    async def register(self, pending):
        self.registered.append(pending)
        return pending


class Keys(JwksCache):
    def __init__(self, settings):
        super().__init__({a.connection_id: (a.issuer, a.jwks_url) for a in settings.authorities})
        self.private = {
            a.connection_id: rsa.generate_private_key(public_exponent=65537, key_size=2048)
            for a in settings.authorities
        }
        self.requested = []

    async def key(self, connection, kid):
        self.requested.append(connection)
        return self.private[connection].public_key()


def settings():
    return LaneAuthSettings(
        secret_key=SecretStr("synthetic-lane-signing-secret-32-bytes"),
        public_url="https://forge.invalid",
        authorities=(
            OidcAuthority(
                connection_id="gh-one",
                provider="github",
                issuer="https://github-oidc.invalid",
                jwks_url="https://github-oidc.invalid/keys",
            ),
            OidcAuthority(
                connection_id="gl-one",
                provider="gitlab",
                issuer="https://gitlab-one.invalid",
                jwks_url="https://gitlab-one.invalid/keys",
            ),
            OidcAuthority(
                connection_id="gl-two",
                provider="gitlab",
                issuer="https://gitlab-two.invalid",
                jwks_url="https://gitlab-two.invalid/keys",
            ),
        ),
    )


def pending(connection="gl-one", provider="gitlab"):
    identity = RunIdentity(forge_run_id="1" * 32, attempt=1, generation=1)
    frozen = spec()
    brief = ApprovedInput.from_spec(frozen).to_brief(identity, frozen)
    dispatch = LaneDispatch(
        **identity.model_dump(),
        server_url="https://forge.invalid",
        provider=provider,
        repository="owner/project",
        ref="forge/one",
        base_sha=frozen.base_sha,
        approved_digest=brief.approved_digest,
        driver=frozen.driver,
        model=frozen.model,
        timeout_seconds=60,
        nonce="synthetic-random-nonce-at-least32bytes",
    )
    return PendingDispatch(
        dispatch=dispatch,
        connection_id=connection,
        brief=brief,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=600),
        cancellation_generation=0,
    )


def token(config, keys, connection, *, issuer=None):
    authority = next(a for a in config.authorities if a.connection_id == connection)
    now = int(datetime.now(timezone.utc).timestamp())
    return jwt.encode(
        {
            "iss": issuer or authority.issuer,
            "aud": config.public_url,
            "sub": "native-job",
            "jti": "synthetic-jti",
            "iat": now,
            "nbf": now,
            "exp": now + 60,
        },
        keys.private[connection],
        algorithm="RS256",
        headers={"kid": "synthetic-kid"},
    )


async def test_lane_oidc_selects_exact_configured_connection_with_multiple_same_provider_hosts():
    config = settings()
    keys = Keys(config)
    auth = LaneAuthService(Store(), config, jwks_cache=keys)
    for connection in ("gl-one", "gl-two"):
        claims = await auth._oidc_claims(token(config, keys, connection), pending(connection))
        assert claims["iss"] == next(
            a.issuer for a in config.authorities if a.connection_id == connection
        )
    assert keys.requested == ["gl-one", "gl-two"]


async def test_lane_oidc_wrong_host_or_issuer_never_falls_back_to_another_connection():
    config = settings()
    keys = Keys(config)
    auth = LaneAuthService(Store(), config, jwks_cache=keys)
    with pytest.raises(LaneAuthError):
        await auth._oidc_claims(token(config, keys, "gl-one"), pending("gl-two"))
    with pytest.raises(LaneAuthError):
        await auth._oidc_claims(
            token(config, keys, "gl-two", issuer="https://gitlab-one.invalid"), pending("gl-two")
        )
    assert keys.requested == ["gl-two", "gl-two"]


async def test_lane_dispatch_requires_explicit_matching_connection_before_registration():
    store = Store()
    auth = LaneAuthService(store, settings())
    current = pending()
    for connection in ("unknown", "gh-one"):
        with pytest.raises(LaneAuthError):
            await auth.register_pending(
                current.dispatch,
                current.brief,
                connection_id=connection,
                expires_at=current.expires_at,
                gitlab_pipeline_id="42",
            )
    assert not store.registered
    saved = await auth.register_pending(
        current.dispatch,
        current.brief,
        connection_id="gl-one",
        expires_at=current.expires_at,
        gitlab_pipeline_id="42",
    )
    assert saved.connection_id == "gl-one"


def test_supplied_jwks_transport_cannot_replace_configured_connection_trust_roots():
    config = settings()
    keys = JwksCache({"gl-one": ("https://other.invalid", "https://other.invalid/keys")})
    with pytest.raises(ValueError, match="authorities differ"):
        LaneAuthService(Store(), config, jwks_cache=keys)
