"""Connection-scoped native actor authorization plus current collaborator rights."""

from collections.abc import Collection
from urllib.parse import quote

from forge.port_values import Actor, ReadUnavailable, SubjectRef
from forge.providers.github.client import API
from forge.providers.github.repo import malformed, native_id, object_doc, text
from forge.providers.github.transport import TransportFailure


class GitHubActor:
    def __init__(self, api: API, *, bot: Actor, approver_ids: Collection[str]) -> None:
        if (
            bot.provider != "github"
            or not bot.is_bot
            or not bot.native_id.isascii()
            or not bot.native_id.isdecimal()
            or int(bot.native_id) <= 0
        ):
            raise ValueError("an explicit connection-scoped GitHub bot is required")
        if isinstance(approver_ids, (str, bytes)):
            raise ValueError("approver IDs require an explicit collection")
        copied = frozenset(approver_ids)
        if any(
            not isinstance(value, str)
            or not value.isascii()
            or not value.isdecimal()
            or int(value) <= 0
            or str(int(value)) != value
            for value in copied
        ):
            raise ValueError("approvers must be explicit positive native user IDs")
        self.api, self.bot, self.approver_ids = api, bot, copied

    def bot_identity(self) -> Actor:
        return self.bot

    async def is_approver(self, actor: Actor, subject: SubjectRef) -> bool:
        if (
            actor.provider != "github"
            or subject.repo.identity.provider != "github"
            or actor.connection_id != self.bot.connection_id
            or subject.repo.identity.connection_id != self.bot.connection_id
            or actor.is_bot
            or actor.native_id not in self.approver_ids
        ):
            return False
        if (
            not actor.login
            or any(ord(char) < 33 for char in actor.login)
            or any(char in actor.login for char in "/\\%?#")
            or actor.login in {".", ".."}
        ):
            return False
        try:
            reply = await self.api.request(
                subject.repo, "GET", "collaborators/" + quote(actor.login, safe="") + "/permission"
            )
        except TransportFailure as error:
            if error.reason == "resource_not_found":
                return False
            raise ReadUnavailable(
                "GitHub collaborator permission unavailable", error.evidence
            ) from None
        document = object_doc(reply.document)
        user = object_doc(document.get("user"))
        if (
            native_id(user.get("id")) != actor.native_id
            or text(user.get("login")).casefold() != actor.login.casefold()
            or user.get("type") != "User"
        ):
            raise malformed("actor_native_identity_mismatch")
        return document.get("permission") in {"write", "maintain", "admin"}
