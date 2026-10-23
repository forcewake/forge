"""Resolve explicit project neighbor authorization before any model request."""

from collections.abc import Mapping
from typing import Annotated

from pydantic import Field, TypeAdapter, ValidationError

from forge.domain.plan import RepositoryIdentity, ShortText
from forge.port_values import NeighborSnapshot, PlanningSnapshot, ReadUnavailable, RepoRef
from forge.ports import RepoPort

from .limits import PlanningRefused


class NeighborAuthorization(RepositoryIdentity):
    ref: ShortText = "HEAD"
    allowed_globs: tuple[ShortText, ...] = ("**",)


NEIGHBORS: TypeAdapter[list[NeighborAuthorization]] = TypeAdapter(
    Annotated[list[NeighborAuthorization], Field(max_length=16)]
)


async def freeze_snapshot(
    reader: RepoPort,
    primary: NeighborSnapshot,
    raw_neighbors: object,
    catalog: Mapping[tuple[str, str, str], RepoRef],
) -> PlanningSnapshot:
    """raw_neighbors is the project config's neighbors field, never model output.

    Catalog entries resolve identity; presence alone grants no read. An empty
    read-glob tuple remains empty. The caller already froze the primary SHA.
    """
    try:
        # JSON arrays in project config become immutable tuples after strict shape checks.
        if raw_neighbors is None:
            return PlanningSnapshot(primary=primary)
        if not isinstance(raw_neighbors, (list, tuple)):
            raise ValueError("neighbors must be a sequence")
        records = NEIGHBORS.validate_python(raw_neighbors, strict=False)
        trusted_catalog = dict(catalog)
        neighbors = []
        identities = {primary.repo.identity.digest}
        for authorization in records:
            key = (authorization.provider, authorization.connection_id, authorization.repository_id)
            repo = trusted_catalog.get(key)
            if (
                repo is None
                or (
                    repo.identity.provider,
                    repo.identity.connection_id,
                    repo.identity.repository_id,
                )
                != key
            ):
                raise PlanningRefused("neighbor_not_authorized")
            if repo.identity.digest in identities:
                raise PlanningRefused("duplicate_neighbor")
            identities.add(repo.identity.digest)
            sha = await reader.head(repo, authorization.ref)
            if sha is None:
                raise PlanningRefused("neighbor_ref_absent")
            neighbors.append(
                NeighborSnapshot(repo=repo, sha=sha, allowed_globs=authorization.allowed_globs)
            )
        return PlanningSnapshot(primary=primary, neighbors=tuple(neighbors))
    except ReadUnavailable:
        raise PlanningRefused("repository_read_unavailable") from None
    except (ValueError, ValidationError):
        raise PlanningRefused("invalid_neighbor_authorization") from None
