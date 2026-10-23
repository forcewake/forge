"""Selected-repository App publication through one native expected-head Git path."""

import base64

from forge.domain.candidate import ValidatedCandidate
from forge.domain.verdicts import CommitOutcome, Committed, Conflict, Sha, Unknown
from forge.port_values import Actor, ReadUnavailable, RepoRef
from forge.ports import Clock, RepoPort
from forge.providers.git_commit import GitTarget, factory_branch, publish
from forge.providers.git_process import GitFailure
from forge.providers.github.auth import AppAuth
from forge.providers.github.client import API
from forge.providers.github.repo import branch_path, evidence, list_doc, object_doc
from forge.providers.github.transport import TransportFailure


class GitHubCommitter:
    def __init__(
        self, api: API, auth: AppAuth, repositories: RepoPort, clock: Clock, *, bot: Actor
    ):
        if (
            not bot.is_bot
            or bot.provider != "github"
            or bot.connection_id != auth.connection_id
            or not bot.native_id.isascii()
            or not bot.native_id.isdecimal()
            or int(bot.native_id) <= 0
            or str(int(bot.native_id)) != bot.native_id
        ):
            raise ValueError("publication requires an explicit matching native bot")
        self.api, self.auth, self.repositories, self.clock, self.bot = (
            api,
            auth,
            repositories,
            clock,
            bot,
        )

    async def commit(
        self, repo: RepoRef, candidate: ValidatedCandidate, expected_head: Sha, marker: str
    ) -> CommitOutcome:
        try:
            branch = factory_branch(candidate.changeset.branch)
            metadata = object_doc((await self.api.metadata(repo)).document)
            if (
                not isinstance(metadata.get("default_branch"), str)
                or not metadata["default_branch"]
                or metadata["default_branch"] == branch
                or metadata.get("archived") is not False
                or metadata.get("disabled") is not False
            ):
                return Unknown(evidence=evidence("commit", reason="repository_not_writable"))
            reply = await self.api.request(repo, "GET", "branches/" + branch_path(branch))
            branch_info = object_doc(reply.document)
            actual = object_doc(branch_info.get("commit")).get("sha")
            if branch_info.get("name") != branch or branch_info.get("protected") is not False:
                return Unknown(evidence=evidence("commit", reason="branch_not_writable"))
            if actual != expected_head:
                return Conflict(
                    actual_head=await self.repositories.head(repo, branch),
                    evidence=evidence("commit", reply.request_id, reason="expected_head_mismatch"),
                )
            rules = await self.api.request(repo, "GET", "rules/branches/" + branch_path(branch))
            if list_doc(rules.document):
                return Unknown(evidence=evidence("commit", reason="branch_rules_present"))
            token = await self.auth.token(repo, write=True)
            credential = base64.b64encode(("x-access-token:" + token.value).encode()).decode()
            # The credential is repository-scoped; a rename/rebind between this
            # metadata observation and Git authentication cannot grant another repo.
            target = GitTarget(
                self.api.git_url.rstrip("/") + "/" + str(metadata["full_name"]) + ".git",
                "Basic " + credential,
                author=self.bot.login,
                email=f"{self.bot.native_id}+{self.bot.login}@users.noreply.github.com",
            )
            now = self.clock.now()
            if now.utcoffset() is None:
                raise ValueError("publication clock must be aware")
            sha = await publish(
                target, candidate, expected_head, marker, stamp=int(now.timestamp())
            )
            return Committed(sha=sha, evidence=evidence("commit", reason="native_ref_accepted"))
        except GitFailure as error:
            if error.stale:
                try:
                    actual_head = await self.repositories.head(repo, candidate.changeset.branch)
                except ReadUnavailable:
                    actual_head = None
                return Conflict(
                    actual_head=actual_head, evidence=evidence("commit", reason=error.reason)
                )
            return Unknown(evidence=evidence("commit", reason=error.reason))
        except TransportFailure as error:
            return Unknown(evidence=error.evidence)
        except (ReadUnavailable, ValueError, OSError):
            return Unknown(evidence=evidence("commit", reason="publication_unavailable"))
