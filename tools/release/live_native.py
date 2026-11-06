"""One explicit native human actor writes only sandbox issues and command comments."""

import hashlib
import hmac
import json
import ssl
import uuid

import httpx

from forge.port_values import Actor, SubjectRef
from forge.runtime.settings import secret


class Sandbox:
    def __init__(self, runtime, connection, repository, actor_token: str, environ):
        self.runtime, self.connection, self.repository = runtime, connection, repository
        self.native = runtime.connections.registry.named(repository.key)
        context = ssl.create_default_context()
        if connection.tls_ca_path is not None:
            context.load_verify_locations(cafile=str(connection.tls_ca_path))
        headers = (
            {"Authorization": "Bearer " + actor_token}
            if connection.provider == "github"
            else {"PRIVATE-TOKEN": actor_token}
        )
        api = connection.api_url.rstrip("/") + (
            "/api/v4" if connection.provider == "gitlab" else ""
        )
        self.client = httpx.Client(
            base_url=api + "/",
            headers=headers,
            timeout=60,
            follow_redirects=False,
            trust_env=False,
            verify=context,
        )
        self.server = httpx.AsyncClient(
            base_url=runtime.config.public_url + "/",
            timeout=60,
            follow_redirects=False,
            trust_env=False,
            verify=context,
        )
        self.environment = environ
        self.root = ""
        self.repo = self.call(
            "GET",
            ("repositories/" if connection.provider == "github" else "projects/")
            + repository.native_id,
        )
        if str(self.repo["id"]) != repository.native_id:
            raise ValueError("native sandbox repository identity changed")
        self.root = (
            ("repos/" + self.repo["full_name"])
            if connection.provider == "github"
            else ("projects/" + repository.native_id)
        )
        self.user = self.call("GET", "user")
        self.actor = Actor(
            provider=connection.provider,
            connection_id=connection.key,
            native_id=str(self.user["id"]),
            login=self.user["login" if connection.provider == "github" else "username"],
            is_bot=self.user.get("type") == "Bot" or self.user.get("bot") is True,
        )

    def call(self, method: str, path: str, **kwargs):
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        if len(response.content) > 2 * 1024 * 1024:
            raise ValueError("native smoke response exceeds its bound")
        return response.json() if response.content else None

    async def authorize(self):
        bot = self.connection.bot(self.repository)
        if (
            self.actor.is_bot
            or self.actor.native_id == bot.native_id
            or self.actor.login.casefold() == bot.login.casefold()
            or not await self.native.actor.is_approver(
                self.actor, SubjectRef(repo=self.native.ref, number=1)
            )
        ):
            raise ValueError("smoke actor is not a current explicit native sandbox approver")
        matching = [
            operator
            for operator in self.runtime.config.operators
            if any(
                g.repository == self.repository.key
                and g.native_id == self.actor.native_id
                and g.login.casefold() == self.actor.login.casefold()
                for g in operator.grants
            )
        ]
        if len(matching) != 1:
            raise ValueError("smoke needs one matching scoped operator identity")
        self.operator = {
            "Authorization": "Bearer "
            + secret(matching[0].token_env, self.environment).get_secret_value()
        }
        ready = await self.server.get("readyz")
        ready.raise_for_status()
        if ready.json() != {"schema": "checked", "native_budget": "verified"}:
            raise ValueError("real server native guard/readiness is unavailable")

    def issue(self, version: str, scenario: str):
        task = "Create only forge_release_smoke.py with def answer(): returning the literal integer 42. Keep tests/test_forge_release_smoke.py and all CI, dependency and policy files unchanged."
        if scenario == "repair":
            task = "This explicitly approved two-step release acceptance exercise first requires a real candidate with def answer(): returning the literal integer 41. Publish that first candidate so the unchanged independent answer()==42 oracle reports a native JUnit failure. Only after that real failure, repair the same implementation to return literal 42. Do not skip the intentional first defect. Change only forge_release_smoke.py; never change the oracle, CI, dependencies or policy."
        payload = {
            "title": f"forge {version} [release {scenario}] {uuid.uuid4().hex[:10]}",
            "body" if self.connection.provider == "github" else "description": task,
        }
        return self.call("POST", self.root + "/issues", json=payload)

    async def command(self, issue: dict, text: str) -> dict:
        github = self.connection.provider == "github"
        number = issue["number" if github else "iid"]
        note = self.call(
            "POST",
            f"{self.root}/issues/{number}/" + ("comments" if github else "notes"),
            json={"body": text},
        )
        if github:
            payload = {
                "action": "created",
                "repository": self.repo,
                "issue": issue,
                "comment": note,
                "sender": self.user,
                "installation": {"id": self.repository.installation_id},
            }
        else:
            payload = {
                "object_kind": "note",
                "event_type": "note",
                "user": self.user,
                "project": self.repo,
                "issue": issue,
                "object_attributes": {
                    **note,
                    "note": note["body"],
                    "noteable_type": "Issue",
                    "noteable_id": issue["id"],
                    "author_id": self.user["id"],
                    "project_id": int(self.repository.native_id),
                    "action": "create",
                    "system": False,
                },
            }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        key = secret(self.connection.webhook_secret_env, self.environment).get_secret_value()
        headers = (
            {
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-Hub-Signature-256": "sha256="
                + hmac.new(key.encode(), raw, hashlib.sha256).hexdigest(),
            }
            if github
            else {
                "X-Gitlab-Event": "Note Hook",
                "X-Gitlab-Event-UUID": str(uuid.uuid4()),
                "X-Gitlab-Token": key,
            }
        )
        response = await self.server.post(
            "hooks/" + self.connection.key,
            content=raw,
            headers={**headers, "Content-Type": "application/json"},
        )
        response.raise_for_status()
        document = response.json()
        if (
            document.get("accepted") is not True
            or len(document.get("commands", [])) != 1
            or document["commands"][0]["status"] != "accepted"
        ):
            raise ValueError("native API-issued command was not durably accepted")
        return document["commands"][0]

    async def close(self):
        self.client.close()
        await self.server.aclose()
