"""The canonical repository identity contract (FND-01, review 05868e9).

Every authority read (project config, base contents, policy) is keyed by a
:class:`RepositoryIdentity` — a PUBLIC value the adapters themselves
produce, never private-attribute probing at the call site. The previous
cache helper recognized GitHub-style ``_owner``/``_repo``; the REAL
``AzureRepositoryReader`` carries ``_project``/``_repo``, so two
repositories of one Azure project fell into the project-id fallback and
could share one policy entry (the review's first remaining defect).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RepositoryIdentity:
    """The canonical identity of one repository on one connection.

    ``tenant`` — the provider tenant/connection discriminator (the GitLab
    base URL, the AzDO org URL, the GitHub API host); two hosts with the
    same owner/repo names are DIFFERENT repositories. ``provider`` — the
    adapter family (``gitlab`` | ``github`` | ``azure_devops``).
    ``native_id`` — the repository's native locator within the tenant
    (GitHub ``owner/repo``; AzDO ``project/repo``; GitLab project id).
    ``display`` — the human locator (may repeat across tenants; never a
    cache key member).
    """

    tenant: str
    provider: str
    native_id: str
    display: str = ""

    def cache_key(self, ref: str, path: str) -> tuple[str, str, str, str, str]:
        """The authority-cache key: identity + requested ref + config path.

        The requested REF is part of the key — a different ref is a
        different authority snapshot, never a cache hit (probe P02 of the
        44cdae review).
        """
        return (self.tenant, self.provider, self.native_id, ref, path)

    def __str__(self) -> str:  # pragma: no cover — display helper
        return f"{self.provider}:{self.native_id}@{self.tenant}"


def repository_identity(reader: object, project_id: int | None = None) -> RepositoryIdentity | None:
    """The reader's canonical identity via its PUBLIC ``identity()``.

    Adapters implement ``identity() -> RepositoryIdentity``; this resolver
    never probes private attributes (the 44cdae helper's failure mode).
    Repository-bound readers take no arguments; project-scoped clients
    (GitLab) take the project id — passed through when given, and a
    TypeError from the no-arg probe of a project-scoped client is the
    signal to retry qualified (never a crash).
    ``None`` = the adapter has not adopted the contract yet — the caller
    must fall back to a FULLY QUALIFIED legacy key (type + repr-stable
    fields), never a bare project id.
    """
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
