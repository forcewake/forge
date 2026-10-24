"""Native Git ref compare-and-swap using an explicit project-scoped bot token."""

import base64
from pathlib import Path
from urllib.parse import urlsplit

from forge.domain.candidate import ValidatedCandidate
from forge.domain.verdicts import CommitOutcome, Committed, Conflict, Unknown
from forge.port_values import RepoRef
from forge.ports import Clock
from forge.providers.git_commit import GitTarget, publish
from forge.providers.git_process import GitFailure
from forge.providers.gitlab.client import API
from forge.providers.gitlab.repo import GitLabRepo
from forge.providers.gitlab.values import evidence, obj, text, unavailable


class GitLabCommitter:
    def __init__(
        self, api: API, repositories: GitLabRepo, clock: Clock, *, ca_path: Path | None = None
    ) -> None:
        self.api, self.repositories, self.clock = api, repositories, clock
        self.ca_path = ca_path

    async def commit(
        self, repo: RepoRef, candidate: ValidatedCandidate, expected_head: str, marker: str
    ) -> CommitOutcome:
        metadata = obj((await self.api.metadata(repo)).document)
        url = text(metadata.get("http_url_to_repo"))
        connection = urlsplit(self.api.transport.endpoint.url)
        remote = urlsplit(url)
        name = text(metadata.get("path_with_namespace"))
        expected_path = connection.path.rstrip("/") + "/" + name + ".git"
        if (
            remote.scheme != connection.scheme
            or remote.netloc != connection.netloc
            or remote.path != expected_path
        ):
            raise unavailable("repository_git_url_mismatch")
        token = await self.api.write_token(repo)
        authorization = "Basic " + base64.b64encode(("oauth2:" + token).encode()).decode()
        target = GitTarget(url=url, authorization=authorization, ca_path=self.ca_path)
        try:
            result = await publish(
                target, candidate, expected_head, marker, stamp=int(self.clock.now().timestamp())
            )
            return Committed(sha=result, evidence=evidence("git_commit", commit=result))
        except GitFailure as error:
            if error.stale:
                actual = await self.repositories.head(repo, candidate.changeset.branch)
                return Conflict(actual_head=actual, evidence=evidence("git_commit_conflict"))
            return Unknown(evidence=evidence("git_commit", reason=error.reason))
