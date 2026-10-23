"""Canonical repository identity independent of display spelling."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RepositoryIdentity:
    "The canonical identity of one repository on one connection."

    tenant: str
    provider: str
    native_id: str
    display: str = ""

    def cache_key(self, ref: str, path: str) -> tuple[str, str, str, str, str]:
        """The authority-cache key: identity + requested ref + config path."""
        return (self.tenant, self.provider, self.native_id, ref, path)

    def __str__(self) -> str:  # pragma: no cover — display helper
        return f"{self.provider}:{self.native_id}@{self.tenant}"


def repository_identity(reader: object, project_id: int | None = None) -> RepositoryIdentity | None:
    """The reader's canonical identity via its PUBLIC ``identity``."""
    method = getattr(reader, "identity", None)
    if not callable(method):
        return None
    try:
        identity = method()
    except TypeError:
        # project-scoped signature — retry with the project id
        if project_id is None:
            return None
        try:
            identity = method(project_id)
        except TypeError:
            return None
    if isinstance(identity, RepositoryIdentity):
        return identity
    return None
