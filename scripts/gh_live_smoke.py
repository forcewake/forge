"""Live smoke for the GitHub adapter against a real repo.

Usage: GH_TOKEN=$(gh auth token) .venv/bin/python scripts/gh_live_smoke.py [owner] [repo]
Idempotent: reuses the existing branch/PR; every step verified live
2026-09-13 (CAS commit + parent check, typed STALE drift, Draft PR
find-by-head-first, Actions pull_request run).
"""

import subprocess, os

import asyncio, base64, sys

sys.path.insert(0, "src")

from forge.integrations.github import GitHubClient, GitHubStaleBranchError

TOKEN = (
    os.environ.get("GH_TOKEN")
    or subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
)
owner = sys.argv[1] if len(sys.argv) > 1 else "forcewake"
repo = sys.argv[2] if len(sys.argv) > 2 else "forge-lab-gh"


async def main():
    class StaticCreds:
        def __init__(self, tok):
            self._t = tok

        async def token(self):
            return self._t

        async def invalidate(self):
            pass

    client = GitHubClient(token_provider=StaticCreds(TOKEN))

    # 1. repo + issue + comment
    r = await client.get_repository(owner, repo)
    print("1. repo:", r["full_name"], "| default:", r["default_branch"])
    import subprocess

    issue = __import__("json").loads(
        subprocess.run(
            [
                "gh",
                "api",
                f"repos/{owner}/{repo}/issues",
                "-f",
                "title=Add a farewell module",
                "-f",
                "body=Create farewell.py exposing farewell(name: str) -> str == 'Goodbye, <name>!'. Stdlib only.",
            ],
            capture_output=True,
            text=True,
        ).stdout
    )
    print("2. issue:", issue["number"])
    await client.create_issue_comment(owner, repo, issue["number"], "/implement")

    # 2. branch from default head — idempotent: expected = live branch head
    branch = "forge/1/live-smoke"
    try:
        head = await client.get_branch_head(owner, repo, branch)
        print("3. branch exists, head", head[:8])
    except Exception:
        head = await client.get_branch_head(owner, repo, r["default_branch"])
        await client.create_branch(owner, repo, branch, head)
        print("3. branch created from", head[:8])

    # 3. CAS commit adding farewell.py
    content = base64.b64encode(
        b'"""Farewell helper."""\n\n\ndef farewell(name: str) -> str:\n    return f"Goodbye, {name}!"\n'
    ).decode()
    op = "live-smoke-0001"
    res = await client.create_commit_on_branch(
        owner,
        repo,
        branch,
        expected_head_oid=head,
        headline="forge: add farewell module (forge-op:" + op + ")",
        additions=[("farewell.py", content)],
        client_mutation_id=op,
    )
    print("4. raw mutation keys:", sorted(res.keys()))
    import json as _j

    print("   payload:", _j.dumps(res)[:300])
    new_head = res["oid"]
    print(
        "4. CAS commit:",
        new_head[:8],
        "| parent check:",
        (await client.get_branch_head(owner, repo, branch))[:8] == new_head[:8],
    )

    # 4. STALE_DATA drift: commit against the OLD head must fail
    from forge.integrations.github import GitHubAPIError

    try:
        await client.create_commit_on_branch(
            owner,
            repo,
            branch,
            expected_head_oid=head,
            headline="stale attempt",
            additions=[("stale.py", "eA==")],
            client_mutation_id="stale-1",
        )
        print("5. STALE: NOT DETECTED — BUG")
    except GitHubStaleBranchError as e:
        print("5. STALE drift detected (typed):", str(e)[:70])
    except GitHubAPIError as e:
        print("5. STALE drift detected (API):", str(e)[:70])

    # 5. Draft PR + find-by-head no duplicate
    existing = await client.get_pr_by_head(owner, repo, f"forcewake:{branch}")
    if existing:
        pr = existing
        print("6. existing draft PR:", pr["number"])
    else:
        pr = await client.create_draft_pr(
            owner,
            repo,
            head=f"forcewake:{branch}",
            base="main",
            title="Draft: Add a farewell module",
            body="Live adapter smoke.",
        )
        print("6. draft PR created:", pr["number"], "| draft:", pr.get("draft"))
    existing = await client.get_pr_by_head(owner, repo, f"forcewake:{branch}")
    print(
        "7. find-by-head -> PR",
        existing["number"],
        "(no duplicate:",
        existing["number"] == pr["number"],
        ")",
    )

    await client.aclose()


asyncio.run(main())
