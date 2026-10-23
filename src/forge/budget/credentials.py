"""Gateway secrets sealed with an explicit identity, purpose and expiry."""

import hashlib
import hmac
import secrets
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import AwareDatetime

from forge_protocol import RunIdentity

from forge.domain.value import Value

Purpose = Literal["implementation", "planning", "closing"]


class CredentialUnavailable(ValueError):
    pass


class KeyScope(Value):
    identity: RunIdentity
    purpose: Purpose
    alias: str
    model: str
    expires_at: AwareDatetime

    @property
    def aad(self) -> bytes:
        return b"forge.engine.gateway-key/1\0" + self.canonical_bytes

    @property
    def aad_digest(self) -> str:
        return hashlib.sha256(self.aad).hexdigest()


class CredentialVault:
    def __init__(self, server_secret: bytes) -> None:
        if len(server_secret) < 32:
            raise ValueError("server secret must contain at least 32 bytes")
        key = hmac.digest(server_secret, b"forge.engine.gateway-key/1/AEAD", "sha256")
        self._cipher = AESGCM(key)

    @staticmethod
    def generate() -> str:
        return "sk-forge-" + secrets.token_urlsafe(32)

    def seal(self, scope: KeyScope, key: str) -> bytes:
        if not key.startswith("sk-") or not 32 <= len(key) <= 256:
            raise CredentialUnavailable("invalid gateway credential")
        nonce = secrets.token_bytes(12)
        return b"\x01" + nonce + self._cipher.encrypt(nonce, key.encode("ascii"), scope.aad)

    def open(self, scope: KeyScope, ciphertext: bytes) -> str:
        try:
            if not 45 <= len(ciphertext) <= 512 or ciphertext[0] != 1:
                raise CredentialUnavailable("unsupported gateway credential record")
            plaintext = self._cipher.decrypt(ciphertext[1:13], ciphertext[13:], scope.aad)
            key = plaintext.decode("ascii")
            if not key.startswith("sk-") or not 32 <= len(key) <= 256:
                raise CredentialUnavailable("invalid gateway credential")
            return key
        except (InvalidTag, UnicodeError, ValueError):
            raise CredentialUnavailable("gateway credential cannot be authenticated") from None
