"""Connection-scoped numeric approvers plus current native project permission."""

from collections.abc import Collection
from datetime import date

from forge.port_values import Actor, ReadUnavailable, SubjectRef
from forge.providers.gitlab.client import API
from forge.providers.gitlab.transport import Failure
from forge.providers.gitlab.values import native_id, numeric, obj, unavailable


class GitLabActor:
    def __init__(self, api: API, *, bot: Actor, approver_ids: Collection[str]) -> None:
        if (
            bot.provider != "gitlab"
            or not bot.is_bot
            or bot.connection_id != api.connection_id
            or isinstance(approver_ids, (str, bytes))
        ):
            raise ValueError("explicit native bot and numeric allowlist required")
        self.api, self.bot = api, bot
        self.approvers = frozenset(numeric(value) for value in approver_ids)

    def bot_identity(self) -> Actor:
        return self.bot

    async def is_approver(self, actor: Actor, subject: SubjectRef) -> bool:
        if (
            actor.provider != "gitlab"
            or actor.connection_id != self.api.connection_id
            or subject.repo.identity.connection_id != self.api.connection_id
            or actor.is_bot
            or actor.native_id == self.bot.native_id
            or actor.native_id not in self.approvers
        ):
            return False
        try:
            reply = await self.api.request(
                subject.repo, "GET", "members/all/" + numeric(actor.native_id)
            )
        except Failure as error:
            if error.reason == "resource_not_found":
                return False
            raise ReadUnavailable("membership_unavailable", error.evidence) from None
        row = obj(reply.document)
        if (
            native_id(row.get("id")) != actor.native_id
            or row.get("username") != actor.login
            or row.get("state") != "active"
        ):
            raise unavailable("member_identity_mismatch")
        level = row.get("access_level")
        expires = row.get("expires_at")
        if expires is not None:
            try:
                if not isinstance(expires, str) or date.fromisoformat(expires) <= date.today():
                    return False
            except ValueError:
                raise unavailable("invalid_membership_expiry") from None
        return type(level) is int and 30 <= level <= 50
