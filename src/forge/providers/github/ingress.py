"""Bounded raw-body HMAC ingress without database or provider write authority."""

from collections.abc import Mapping
import hashlib
import hmac
import json
import re
from typing import cast
from uuid import UUID

from forge_protocol import digest

from forge.domain.plan import RepositoryIdentity
from forge.ingress_values import Command, CommandKind, IngressEvent, WebhookUnauthenticated
from forge.port_values import Actor, RepoRef, ReviewRef, SubjectRef, ReadUnavailable
from forge.providers.github.repo import native_id, object_doc, text

MAX_WEBHOOK = 2 * 1024 * 1024
_COMMAND = re.compile(
    r"^/(implement|go|cancel|retry|status|why-blocked|reconcile|pause|resume|steer|fix|ask|approve-revision|amend)(?:[ \t]+([^\r\n]*))?[ \t]*$",
    re.MULTILINE,
)
_EVENTS = frozenset(
    (
        "issues",
        "issue_comment",
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "workflow_run",
    )
)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate webhook JSON key")
        result[key] = value
    return result


def parse_command(body: str) -> tuple[Command, ...]:
    # Only an explicit first-line slash command is executable. Quoted text,
    # code fences and commands embedded in an explanation are never commands.
    first = body.splitlines()[0] if body.splitlines() else ""
    match = _COMMAND.fullmatch(first)
    if match is None:
        return ()
    argument = match[2] or ""
    if len(body.splitlines()) > 1:
        argument = (argument + "\n" + "\n".join(body.splitlines()[1:])).strip()
    if len(argument) > 16_384:
        raise ValueError("command argument exceeds bound")
    return (Command(kind=cast(CommandKind, match[1].replace("-", "_")), argument=argument),)


class GitHubIngress:
    def __init__(
        self,
        connection_id: str,
        secret: bytes,
        *,
        repositories: Mapping[str, int],
        bot: Actor,
        implement_label: str = "forge",
    ) -> None:
        if (
            len(secret) < 32
            or not connection_id
            or bot.connection_id != connection_id
            or bot.provider != "github"
            or not bot.is_bot
        ):
            raise ValueError("explicit connection, bot and strong webhook secret are required")
        if any(
            not key.isascii()
            or not key.isdecimal()
            or int(key) <= 0
            or type(value) is not int
            or value <= 0
            for key, value in repositories.items()
        ):
            raise ValueError("trusted native repository and installation IDs are required")
        self.connection_id, self._secret, self.repositories, self.bot = (
            connection_id,
            secret,
            dict(repositories),
            bot,
        )
        self.implement_label = implement_label

    def parse(self, headers: Mapping[str, str], body: bytes) -> IngressEvent | None:
        lowered = {key.lower(): value for key, value in headers.items()}
        signature = lowered.get("x-hub-signature-256", "")
        if len(body) > MAX_WEBHOOK:
            raise ValueError("webhook exceeds size bound")
        expected = "sha256=" + hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        if not re.fullmatch(r"sha256=[0-9a-f]{64}", signature) or not hmac.compare_digest(
            signature, expected
        ):
            raise WebhookUnauthenticated("webhook signature refused")
        delivery, event = lowered.get("x-github-delivery", ""), lowered.get("x-github-event", "")
        try:
            if str(UUID(delivery)) != delivery.lower() or event not in _EVENTS:
                raise ValueError("unsupported event or delivery identity")
            document = object_doc(
                json.loads(
                    body,
                    object_pairs_hook=_pairs,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")),
                )
            )
            return self._event(document, event, delivery.lower())
        except (UnicodeError, json.JSONDecodeError, TypeError, KeyError, ReadUnavailable) as error:
            raise ValueError("malformed webhook") from error

    def _event(self, doc: dict[str, object], event: str, delivery: str) -> IngressEvent | None:
        repository, installation, sender = (
            object_doc(doc.get("repository")),
            object_doc(doc.get("installation")),
            object_doc(doc.get("sender")),
        )
        repo_id, install_id = (
            native_id(repository.get("id")),
            int(native_id(installation.get("id"))),
        )
        if self.repositories.get(repo_id) != install_id:
            raise ValueError("webhook installation or repository scope refused")
        full_name = text(repository.get("full_name"))
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name) is None:
            raise ValueError("invalid repository display path")
        actor = Actor(
            provider="github",
            connection_id=self.connection_id,
            native_id=native_id(sender.get("id")),
            login=text(sender.get("login")),
            is_bot=sender.get("type") == "Bot",
        )
        if event != "workflow_run" and (
            actor.is_bot
            or actor.native_id == self.bot.native_id
            or actor.login.casefold() == self.bot.login.casefold()
        ):
            return None
        repo = RepoRef(
            identity=RepositoryIdentity(
                provider="github", connection_id=self.connection_id, repository_id=repo_id
            ),
            full_name=full_name,
        )
        action = text(doc.get("action"))
        values: dict[str, object] = {
            "delivery_key": f"github:{self.connection_id}:{delivery}",
            "event": event,
            "action": action,
            "repo": repo,
            "actor": actor,
        }
        if event == "workflow_run":
            self._workflow(doc, repo, values)
        elif event.startswith("pull_request"):
            pull = object_doc(doc.get("pull_request"))
            values["target"] = ReviewRef(repo=repo, number=int(native_id(pull.get("number"))))
            if event == "pull_request" and action == "synchronize":
                head = object_doc(pull.get("head"))
                if native_id(object_doc(head.get("repo")).get("id")) != repo_id:
                    raise ValueError("native factory review cannot use a fork head")
                values["head_sha"] = text(head.get("sha"))
            self._content(doc, event, action, values)
        else:
            issue = object_doc(doc.get("issue"))
            number = int(native_id(issue.get("number")))
            values["target"] = (
                ReviewRef(repo=repo, number=number)
                if issue.get("pull_request") is not None
                else SubjectRef(repo=repo, number=number)
            )
            self._content(doc, event, action, values)
        return IngressEvent.model_validate(values)

    def _content(
        self, doc: dict[str, object], event: str, action: str, values: dict[str, object]
    ) -> None:
        if event in ("issue_comment", "pull_request_review_comment") and action == "created":
            comment = object_doc(doc.get("comment"))
            self._author(comment, values)
            values["native_id"] = native_id(comment.get("id"))
            values["commands"] = parse_command(text(comment.get("body"), empty=True))
        elif event == "pull_request_review" and action == "submitted":
            review = object_doc(doc.get("review"))
            self._author(review, values)
            values["native_id"] = native_id(review.get("id"))
            values["commands"] = (
                parse_command(text(review.get("body"), empty=True))
                if review.get("body") is not None
                else ()
            )
        elif (
            event == "issues"
            and action == "labeled"
            and text(object_doc(doc.get("label")).get("name")).casefold()
            == self.implement_label.casefold()
        ):
            values["commands"] = (Command(kind="implement"),)
            target = values["target"]
            assert isinstance(target, (SubjectRef, ReviewRef))
            values["native_id"] = "issue-label:" + digest(
                {
                    "number": target.number,
                    "updated_at": object_doc(doc.get("issue")).get("updated_at"),
                }
            )
        elif event == "issues" and action in ("edited", "unlabeled"):
            target = values["target"]
            if not isinstance(target, SubjectRef):
                return
            if (
                action == "unlabeled"
                and text(object_doc(doc.get("label")).get("name")).casefold()
                != self.implement_label.casefold()
            ):
                return
            issue = object_doc(doc.get("issue"))
            version = text(issue.get("updated_at"))
            values["native_id"] = "issue:" + digest(
                {
                    "number": target.number,
                    "action": action,
                    "version": version,
                    "title": text(issue.get("title")),
                    "body": issue.get("body"),
                }
            )
            values["issue_action"] = action
            values["commands"] = (Command(kind="implement" if action == "edited" else "cancel"),)

    @staticmethod
    def _author(row: dict[str, object], values: dict[str, object]) -> None:
        actor = values["actor"]
        assert isinstance(actor, Actor)
        user = object_doc(row.get("user"))
        if (
            native_id(user.get("id")) != actor.native_id
            or text(user.get("login")).casefold() != actor.login.casefold()
            or user.get("type") != "User"
        ):
            raise ValueError("comment sender and author differ")

    @staticmethod
    def _workflow(doc: dict[str, object], repo: RepoRef, values: dict[str, object]) -> None:
        row = object_doc(doc.get("workflow_run"))
        if (
            row.get("repository") is not None
            and native_id(object_doc(row.get("repository")).get("id"))
            != repo.identity.repository_id
        ) or native_id(
            object_doc(row.get("head_repository")).get("id")
        ) != repo.identity.repository_id:
            raise ValueError("workflow repository mismatch")
        values["native_id"], values["native_attempt"] = (
            native_id(row.get("id")),
            int(native_id(row.get("run_attempt"))),
        )
        # This is an observation hint only. CIPort re-reads the native path,
        # branch, SHA and durable nonce binding before accepting any result.
        match = re.fullmatch(
            r"forge-lane:[\w.-]+:[0-9]+:[0-9]+:([\w.-]+)", text(row.get("display_title"))
        )
        if match and row.get("event") == "workflow_dispatch":
            values["correlation_id"] = match[1]
