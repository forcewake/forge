"""Encrypted attempt grants; ciphertext is bound to the authenticated lane identity."""

import base64
import secrets
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from forge_protocol import ModelKeyGrant, RunIdentity, canonical_json

from forge.api.lane_auth_values import identity_of


class VaultError(ValueError):
    pass


class ModelKeyVault:
    def __init__(self, key: bytes):
        self.cipher = AESGCM(key)

    def seal(self, identity: RunIdentity, grant: ModelKeyGrant) -> dict[str, Any]:
        body = canonical_json(grant)
        if len(body) > 16384 or identity_of(grant) != identity_of(identity):
            raise VaultError("invalid model grant for lane identity")
        nonce = secrets.token_bytes(12)
        ciphertext = self.cipher.encrypt(nonce, body, canonical_json(identity_of(identity)))
        return {
            "version": 1,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }

    def open(self, identity: RunIdentity, record: dict[str, Any]) -> ModelKeyGrant:
        try:
            if (
                set(record) != {"version", "nonce", "ciphertext"}
                or type(record["version"]) is not int
                or record["version"] != 1
            ):
                raise VaultError("unsupported encrypted grant record")
            if len(record["ciphertext"]) > 32768 or len(record["nonce"]) > 32:
                raise VaultError("encrypted model grant exceeds its limit")
            nonce = base64.b64decode(record["nonce"], validate=True)
            ciphertext = base64.b64decode(record["ciphertext"], validate=True)
            if len(nonce) != 12:
                raise VaultError("invalid encrypted model grant nonce")
            body = self.cipher.decrypt(nonce, ciphertext, canonical_json(identity_of(identity)))
            grant = ModelKeyGrant.model_validate_json(body)
            if identity_of(grant) != identity_of(identity):
                raise VaultError("encrypted model grant identity differs")
            return grant
        except (InvalidTag, ValueError, TypeError, KeyError):
            raise VaultError("cached model grant cannot be authenticated") from None
