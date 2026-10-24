"""Bounded shared-secret GitLab hooks producing facts without write authority."""

from collections.abc import Mapping
import hmac
import json
import re
from typing import cast
from uuid import UUID

from forge.domain.plan import RepositoryIdentity
from forge.ingress_values import Command, CommandKind, IngressEvent
from forge.port_values import Actor, RepoRef, ReviewRef, SubjectRef
from forge.providers.gitlab.values import array, native_id, numeric, obj, text

MAX_WEBHOOK = 2 * 1024 * 1024
EVENTS = {
    "Note Hook": "note",
    "Issue Hook": "issue",
    "Merge Request Hook": "merge_request",
    "Pipeline Hook": "pipeline",
    "Job Hook": "build",
}
COMMAND = re.compile(
    r"/(implement|go|cancel|retry|status|why-blocked|reconcile|pause|resume|steer|fix|ask|approve-revision)(?:[ \t]+([^\r\n]*))?[ \t]*"
)


def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate webhook JSON key")
        result[key] = value
    return result


def command(body: str) -> tuple[Command, ...]:
    lines = body.splitlines()
    match = COMMAND.fullmatch(lines[0] if lines else "")
    if match is None:
        return ()
    argument = ((match[2] or "") + "\n" + "\n".join(lines[1:])).strip()
    if len(argument) > 16_384:
        raise ValueError("command argument exceeds bound")
    return (Command(kind=cast(CommandKind, match[1].replace("-", "_")), argument=argument),)


class GitLabIngress:
    def __init__(
        self,
        connection_id: str,
        secret: bytes,
        *,
        bots: Mapping[str, Actor],
        implement_label: str = "forge",
    ) -> None:
        if (
            not connection_id
            or len(secret) < 32
            or any(
                numeric(key) != key
                or not bot.is_bot
                or bot.provider != "gitlab"
                or bot.connection_id != connection_id
                for key, bot in bots.items()
            )
        ):
            raise ValueError(
                "explicit connection, native project bots and strong hook secret required"
            )
        self.connection_id, self._secret, self.bots = connection_id, secret, dict(bots)
        self.implement_label = implement_label

    def parse(self, headers: Mapping[str, str], body: bytes) -> IngressEvent | None:
        lowered = {key.lower(): value for key, value in headers.items()}
        if len(lowered) != len(headers):
            raise ValueError("ambiguous webhook headers")
        token = lowered.get("x-gitlab-token", "")
        if len(body) > MAX_WEBHOOK or not hmac.compare_digest(token.encode(), self._secret):
            raise ValueError("webhook secret or size refused")
        delivery, event = lowered.get("x-gitlab-event-uuid", ""), lowered.get("x-gitlab-event", "")
        try:
            if str(UUID(delivery)) != delivery.lower() or event not in EVENTS:
                raise ValueError("unsupported event or delivery identity")
            document = obj(
                json.loads(
                    body,
                    object_pairs_hook=pairs,
                    parse_constant=lambda _: (_ for _ in ()).throw(
                        ValueError("nonfinite webhook JSON")
                    ),
                )
            )
            if document.get("object_kind") != EVENTS[event]:
                raise ValueError("hook header and body kind differ")
            return self._event(document, delivery.lower())
        except (UnicodeError, RecursionError, TypeError, KeyError):
            raise ValueError("malformed webhook") from None

    def _repo(self, document: dict[str, object]) -> RepoRef:
        project = obj(document.get("project"))
        ident = native_id(project.get("id"))
        if ident not in self.bots:
            raise ValueError("project is not configured on this connection")
        name = text(project.get("path_with_namespace"))
        if re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", name) is None or any(
            part in (".", "..") for part in name.split("/")
        ):
            raise ValueError("invalid project display name")
        return RepoRef(
            identity=RepositoryIdentity(
                provider="gitlab", connection_id=self.connection_id, repository_id=ident
            ),
            full_name=name,
        )

    def _event(self, document: dict[str, object], delivery: str) -> IngressEvent | None:
        repo = self._repo(document)
        kind = text(document.get("object_kind"))
        user = obj(document.get("user"))
        actor = Actor(
            provider="gitlab",
            connection_id=self.connection_id,
            native_id=native_id(user.get("id")),
            login=text(user.get("username")),
            is_bot=user.get("bot") is True,
        )
        bot = self.bots[repo.identity.repository_id]
        if kind not in ("pipeline", "build") and (
            actor.is_bot
            or actor.native_id == bot.native_id
            or actor.login == bot.login
            or re.fullmatch(r"(?:project|group)_[0-9]+_bot_.+", actor.login)
        ):
            return None
        values: dict[str, object] = {
            "delivery_key": f"gitlab:{self.connection_id}:{delivery}",
            "event": kind,
            "action": kind,
            "repo": repo,
            "actor": actor,
        }
        if kind == "note":
            self._note(document, repo, actor, values)
        elif kind in ("issue", "merge_request"):
            self._subject(document, repo, kind, values)
        else:
            self._execution(document, repo, kind, values)
        return IngressEvent.model_validate(values)

    @staticmethod
    def _target(row: dict[str, object], repo: RepoRef, review: bool) -> SubjectRef | ReviewRef:
        if review:
            if (
                native_id(row.get("source_project_id")) != repo.identity.repository_id
                or native_id(row.get("target_project_id")) != repo.identity.repository_id
            ):
                raise ValueError("fork merge requests cannot become factory targets")
            return ReviewRef(repo=repo, number=int(native_id(row.get("iid"))))
        if native_id(row.get("project_id")) != repo.identity.repository_id:
            raise ValueError("issue project differs from hook project")
        return SubjectRef(repo=repo, number=int(native_id(row.get("iid"))))

    def _note(
        self, document: dict[str, object], repo: RepoRef, actor: Actor, values: dict[str, object]
    ) -> None:
        row = obj(document.get("object_attributes"))
        is_review = row.get("noteable_type") == "MergeRequest"
        if not is_review and row.get("noteable_type") != "Issue":
            raise ValueError("unsupported native note target")
        target = obj(document.get("merge_request" if is_review else "issue"))
        if (
            native_id(row.get("author_id")) != actor.native_id
            or native_id(row.get("noteable_id")) != native_id(target.get("id"))
            or native_id(row.get("project_id")) != repo.identity.repository_id
        ):
            raise ValueError("note author or target differs from native hook identity")
        values["target"] = self._target(target, repo, is_review)
        values["native_id"] = native_id(row.get("id"))
        action = text(row.get("action"))
        values["action"] = action
        if row.get("system") is True:
            return
        if action == "create":
            values["commands"] = command(text(row.get("note"), empty=True))

    def _subject(
        self, document: dict[str, object], repo: RepoRef, kind: str, values: dict[str, object]
    ) -> None:
        row = obj(document.get("object_attributes"))
        values["target"] = self._target(row, repo, kind == "merge_request")
        values["native_id"] = native_id(row.get("id"))
        action = text(row.get("action"))
        values["action"] = action
        if kind != "issue":
            if action == "update" and row.get("last_commit") is not None:
                values["head_sha"] = text(obj(row["last_commit"]).get("id"))
            return
        changes = obj(document.get("changes", {}))
        label_change = changes.get("labels")
        if label_change is None:
            if action == "open" and self.implement_label in {
                text(obj(label).get("title")) for label in array(row.get("labels", []))
            }:
                values["commands"] = (Command(kind="implement"),)
            return
        labels = obj(label_change)
        before = {text(obj(label).get("title")) for label in array(labels.get("previous"))}
        after = {text(obj(label).get("title")) for label in array(labels.get("current"))}
        if (self.implement_label in before) != (self.implement_label in after):
            values["commands"] = (
                Command(kind="implement" if self.implement_label in after else "cancel"),
            )

    @staticmethod
    def _execution(
        document: dict[str, object], repo: RepoRef, kind: str, values: dict[str, object]
    ) -> None:
        if kind == "pipeline":
            row = obj(document.get("object_attributes"))
            ident = native_id(row.get("id"))
            if (
                row.get("project_id") is not None
                and native_id(row.get("project_id")) != repo.identity.repository_id
            ):
                raise ValueError("pipeline project differs from hook project")
            values["native_id"], values["native_attempt"] = ident, int(ident)
            values["action"] = text(row.get("status"))
        else:
            if native_id(document.get("project_id")) != repo.identity.repository_id:
                raise ValueError("job project differs from hook project")
            values["native_id"] = native_id(document.get("pipeline_id"))
            values["native_attempt"] = int(native_id(document.get("build_id")))
            values["action"] = text(document.get("build_status"))
