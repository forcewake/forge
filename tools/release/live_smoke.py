"""Drive an isolated release image through real provider lanes in a sandbox.

Run inside the release image with its database and provider configuration.
Only the named sandbox is written. No merge or target branch update occurs.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ORACLE_PATH = "tests/test_forge_release_smoke.py"
ORACLE = (
    b"from forge_release_smoke import answer\n\ndef test_answer():\n    assert answer() == 42\n"
)


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"missing {name}")
    return value


class Sandbox:
    def __init__(self, provider: str, target: str) -> None:
        self.provider = provider
        self.target = target
        api = (
            os.environ.get("FORGE_GITHUB_API_URL", "https://api.github.com")
            if provider == "github"
            else required("GITLAB_URL").rstrip("/") + "/api/v4"
        )
        header = "Authorization" if provider == "github" else "PRIVATE-TOKEN"
        token = required("FORGE_RELEASE_SMOKE_ACTOR_TOKEN")
        self.client = httpx.Client(
            base_url=api + "/",
            headers={header: f"Bearer {token}" if provider == "github" else token},
            timeout=60,
            follow_redirects=False,
        )
        self.root = (
            f"repos/{target}" if provider == "github" else f"projects/{quote(target, safe='')}"
        )
        self.repo = self.call("GET", self.root)
        self.actor = self.call("GET", "user")

    def call(self, method: str, path: str, **kwargs):
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    def download(self, path: str):
        response = self.client.get(path)
        if response.is_redirect:
            location = response.headers["location"]
            if not location.startswith("https://"):
                raise ValueError("artifact redirect requires HTTPS")
            # Object storage receives no provider credential.
            response = httpx.get(location, timeout=60, follow_redirects=True)
        response.raise_for_status()
        return response

    def file(self, path: str, ref: str) -> bytes:
        if self.provider == "github":
            row = self.call("GET", f"{self.root}/contents/{path}", params={"ref": ref})
            return base64.b64decode(row["content"])
        response = self.client.get(
            f"{self.root}/repository/files/{quote(path, safe='')}/raw", params={"ref": ref}
        )
        response.raise_for_status()
        return response.content

    def variable(self, key: str, value: str | None = None, *, delete: bool = False):
        path = (
            f"{self.root}/actions/variables"
            if self.provider == "github"
            else f"{self.root}/variables"
        )
        current = self.client.get(f"{path}/{key}")
        if current.status_code != 404:
            current.raise_for_status()
        old = current.json() if current.status_code == 200 else None
        if delete:
            if old is not None:
                self.call("DELETE", f"{path}/{key}")
        elif value is not None:
            payload = (
                {"name": key, "value": value}
                if self.provider == "github"
                else {"value": value}
                if old
                else {"key": key, "value": value, "raw": True}
            )
            self.call(
                "PATCH" if old and self.provider == "github" else "PUT" if old else "POST",
                f"{path}/{key}" if old else path,
                json=payload,
            )
        return old

    def issue(self, version: str):
        task = (
            "Create forge_release_smoke.py with answer() returning the integer 42. "
            f"Keep {ORACLE_PATH} unchanged and pass its release-smoke-oracle CI job. "
            "Do not change CI, project configuration, dependencies or any other file."
        )
        payload = {"title": f"forge {version} release smoke {uuid.uuid4().hex[:10]}"}
        payload["body" if self.provider == "github" else "description"] = task
        return self.call("POST", f"{self.root}/issues", json=payload)

    def command(self, issue, command: str) -> None:
        github = self.provider == "github"
        number = issue["number" if github else "iid"]
        bot = required("FORGE_GITHUB_BOT_LOGIN" if github else "FORGE_BOT_USERNAME")
        note = self.call(
            "POST",
            f"{self.root}/issues/{number}/{'comments' if github else 'notes'}",
            json={"body": f"@{bot} {command}"},
        )
        if github:
            payload = {
                "action": "created",
                "repository": self.repo,
                "issue": issue,
                "comment": note,
                "sender": self.actor,
                "installation": {"id": int(required("FORGE_GITHUB_INSTALLATION_ID"))},
            }
            raw = json.dumps(payload).encode()
            signature = hmac.new(
                required("FORGE_GITHUB_WEBHOOK_SECRET").encode(), raw, hashlib.sha256
            ).hexdigest()
            headers = {
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": uuid.uuid4().hex,
                "X-Hub-Signature-256": f"sha256={signature}",
            }
        else:
            payload = {
                "object_kind": "note",
                "event_type": "note",
                "user": self.actor,
                "project": self.repo,
                "issue": issue,
                "object_attributes": {
                    **note,
                    "note": note["body"],
                    "noteable_type": "Issue",
                    "noteable_id": issue["id"],
                    "author_id": self.actor["id"],
                    "system": False,
                },
            }
            raw = json.dumps(payload).encode()
            headers = {
                "X-Gitlab-Event": "Note Hook",
                "X-Gitlab-Token": required("GITLAB_WEBHOOK_SECRET"),
            }
        # Deliver real API-issued comment IDs through signed production ingress.
        # Public webhook registration itself is outside this smoke's claim.
        response = httpx.post(
            required("FORGE_RELEASE_SMOKE_GATEWAY_URL").rstrip("/")
            + ("/webhook/github" if github else "/webhook"),
            content=raw,
            headers={**headers, "Content-Type": "application/json"},
            timeout=60,
        )
        response.raise_for_status()

    def candidate(self, run) -> dict:
        sha = run["candidate_shas"][-1]
        number = run["mr_iid"]
        if self.file(ORACLE_PATH, sha) != ORACLE:
            raise ValueError("candidate modified the independent oracle")
        if self.provider == "github":
            pr = self.call("GET", f"{self.root}/pulls/{number}")
            assert pr["draft"] is True and pr["head"]["sha"] == sha and not pr["merged"]
            checks = self.call(
                "GET", f"{self.root}/commits/{sha}/check-runs", params={"per_page": 100}
            )["check_runs"]
            oracle = [
                r for r in checks if r["name"] == "release-smoke-oracle" and r["head_sha"] == sha
            ]
            assert oracle and all(r["conclusion"] == "success" for r in oracle)
            artifacts = self.call(
                "GET",
                f"{self.root}/actions/artifacts",
                params={"name": f"forge-candidate-{run['id']}", "per_page": 100},
            )["artifacts"]
            assert len(artifacts) == 1 and not artifacts[0]["expired"]
            response = self.download(f"{self.root}/actions/artifacts/{artifacts[0]['id']}/zip")
        else:
            pr = self.call("GET", f"{self.root}/merge_requests/{number}")
            assert pr["draft"] is True and pr["sha"] == sha and pr["state"] == "opened"
            pipelines = self.call(
                "GET", f"{self.root}/pipelines", params={"sha": sha, "per_page": 100}
            )
            assert pipelines
            jobs = self.call(
                "GET", f"{self.root}/pipelines/{pipelines[0]['id']}/jobs", params={"per_page": 100}
            )
            oracle = [r for r in jobs if r["name"] == "release-smoke-oracle"]
            assert oracle and all(r["status"] == "success" for r in oracle)
            lane = self.gitlab_lane(run["id"])
            response = self.download(f"{self.root}/jobs/{lane}/artifacts")
        response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = [n for n in archive.namelist() if n.endswith("lane_install.json")]
            assert len(names) == 1, "lane did not export its verified wheel identity"
            identity = json.loads(archive.read(names[0]))
        return {
            "draft_url": pr.get("html_url", pr.get("web_url")),
            "candidate_sha": sha,
            "oracle_jobs": [r["id"] for r in oracle],
            "lane_install": identity,
        }

    def gitlab_lane(self, run_id: str) -> int:
        pipelines = self.call("GET", f"{self.root}/pipelines", params={"per_page": 100})
        for pipeline in pipelines:
            variables = self.call("GET", f"{self.root}/pipelines/{pipeline['id']}/variables")
            if any(v["key"] == "FORGE_RUN_ID" and v["value"] == run_id for v in variables):
                jobs = self.call(
                    "GET", f"{self.root}/pipelines/{pipeline['id']}/jobs", params={"per_page": 100}
                )
                lanes = [
                    j
                    for j in jobs
                    if j["name"] == "forge-agent-claude-sdk" and j["status"] == "success"
                ]
                assert len(lanes) == 1
                return lanes[0]["id"]
        raise ValueError("no successful lane pipeline for the smoke run")


async def wait_run(provider: str, project: int, issue: int, wanted: str, timeout: int):
    engine = create_async_engine(required("DATABASE_URL"))
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT id,status,mr_iid,candidate_shas FROM flow_runs WHERE provider=:provider AND project_id=:project AND issue_iid=:issue ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"provider": provider, "project": project, "issue": issue},
                )
                row = result.mappings().one_or_none()
            if row:
                if row["status"] == wanted:
                    return dict(row)
                if row["status"] in {"blocked", "failed", "cancelled", "ready_for_human"}:
                    raise ValueError(f"run reached {row['status']} while waiting for {wanted}")
            await asyncio.sleep(5)
        raise TimeoutError(f"run did not reach {wanted}")
    finally:
        await engine.dispose()


def verify_install(
    identity: dict, version: str, digest: str, requirements_digest: str | None = None
) -> None:
    if not (
        identity.get("pin") == "wheel"
        and identity.get("route") == "explicit-wheel"
        and identity.get("version") == version
        and identity.get("qualified") is True
        and identity.get("expected_sha256") == digest
        and identity.get("actual_sha256") == digest
        and (
            requirements_digest is None
            or identity.get("requirements_sha256") == requirements_digest
        )
    ):
        raise ValueError("live lane did not install the exact RC wheel")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=("github", "gitlab"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=2400)
    args = parser.parse_args()
    if required("FORGE_RELEASE_SANDBOX") != "1":
        raise ValueError("release smoke requires the dedicated sandbox acknowledgement")
    manifest = json.loads(args.manifest.read_text())
    import forge

    assert forge.__version__ == manifest["version"]
    sandbox = Sandbox(args.provider, required("FORGE_RELEASE_SMOKE_TARGET"))
    assert sandbox.file(ORACLE_PATH, sandbox.repo["default_branch"]) == ORACLE
    keys = {
        "FORGE_LANE_WHEEL": manifest["wheel_url"],
        "FORGE_LANE_WHEEL_SHA256": manifest["wheel_sha256"],
        "FORGE_LANE_REQUIREMENTS": manifest["requirements_url"],
        "FORGE_LANE_REQUIREMENTS_SHA256": manifest["requirements_sha256"],
    }
    previous = {key: sandbox.variable(key) for key in keys}
    try:
        for key, value in keys.items():
            sandbox.variable(key, value)
        issue = sandbox.issue(manifest["version"])
        sandbox.command(issue, "/implement")
        number = issue["number" if args.provider == "github" else "iid"]
        run = asyncio.run(
            wait_run(args.provider, sandbox.repo["id"], number, "waiting_approval", args.timeout)
        )
        sandbox.command(issue, f"/go {run['id']}")
        run = asyncio.run(
            wait_run(args.provider, sandbox.repo["id"], number, "ready_for_human", args.timeout)
        )
        proof = sandbox.candidate(run)
        verify_install(
            proof["lane_install"],
            manifest["version"],
            manifest["wheel_sha256"],
            manifest["requirements_sha256"],
        )
        args.out.write_text(
            json.dumps(
                {
                    "provider": args.provider,
                    "version": manifest["version"],
                    "image": manifest["image"],
                    "wheel_sha256": manifest["wheel_sha256"],
                    "requirements_sha256": manifest["requirements_sha256"],
                    "run_id": run["id"],
                    "issue": number,
                    "status": "passed",
                    "ingress": "signed-api-comment-delivery",
                    **proof,
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        failed_restores = []
        for key, old in previous.items():
            try:
                sandbox.variable(key, old["value"] if old else None, delete=old is None)
            except httpx.HTTPError:
                failed_restores.append(key)
        sandbox.client.close()
        if failed_restores:
            raise RuntimeError(
                "sandbox variables require restoration: " + ", ".join(failed_restores)
            )


if __name__ == "__main__":
    main()
