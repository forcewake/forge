"""Register GitLab webhooks and create Forge labels for projects.

Usage:
    uv run python scripts/setup_gitlab.py --project-id 42 --webhook-url http://forge:8420/webhook
    uv run python scripts/setup_gitlab.py --group-id 10 --webhook-url http://forge:8420/webhook
    uv run python scripts/setup_gitlab.py --project-id 42 --dry-run
"""

from __future__ import annotations
import argparse
import sys

import httpx

from forge.config import Settings

WEBHOOK_EVENTS = {
    "push_events": True,
    "merge_requests_events": True,
    "note_events": True,
    "pipeline_events": True,
    "job_events": True,
    "issues_events": False,
}

FORGE_LABELS = [
    {"name": "ai-reviewed", "color": "#34d399", "description": "Reviewed by Forge"},
    {"name": "ai-needs-changes", "color": "#f87171", "description": "Forge found issues"},
    {
        "name": "security-critical",
        "color": "#ef4444",
        "description": "Security issue flagged by Forge",
    },
]


def _api_client(settings: Settings) -> httpx.Client:
    return httpx.Client(
        base_url=f"{settings.GITLAB_URL.rstrip('/')}/api/v4",
        headers={"PRIVATE-TOKEN": settings.GITLAB_TOKEN.get_secret_value()},
        timeout=30.0,
    )


def _check_connectivity(client: httpx.Client) -> str:
    resp = client.get("/version")
    resp.raise_for_status()
    version = resp.json().get("version", "unknown")
    print(f"  Connected to GitLab {version}")
    return version


def _get_group_project_ids(client: httpx.Client, group_id: int) -> list[int]:
    """Fetch all project IDs in a group (CE-compatible, no group hooks)."""
    project_ids: list[int] = []
    params: dict = {"per_page": 100, "include_subgroups": True}

    while True:
        resp = client.get(f"/groups/{group_id}/projects", params=params)
        resp.raise_for_status()
        projects = resp.json()
        project_ids.extend(p["id"] for p in projects)

        next_page = resp.headers.get("x-next-page", "").strip()
        if not next_page:
            break
        params["page"] = int(next_page)

    return project_ids


def _list_existing_hooks(client: httpx.Client, project_id: int) -> list[dict]:
    resp = client.get(f"/projects/{project_id}/hooks")
    resp.raise_for_status()
    return resp.json()


def _register_webhook(
    client: httpx.Client,
    project_id: int,
    webhook_url: str,
    secret: str,
    dry_run: bool = False,
) -> bool:
    """Register webhook on a project. Returns True if registered, False if skipped."""
    existing = _list_existing_hooks(client, project_id)
    for hook in existing:
        if hook.get("url") == webhook_url:
            print(
                f"  Project {project_id}: webhook already registered (hook {hook['id']}), skipping"
            )
            return False

    if dry_run:
        print(f"  Project {project_id}: would register webhook → {webhook_url}")
        return False

    payload = {"url": webhook_url, "token": secret, **WEBHOOK_EVENTS}
    resp = client.post(f"/projects/{project_id}/hooks", json=payload)
    resp.raise_for_status()
    hook_id = resp.json().get("id")
    print(f"  Project {project_id}: webhook registered (hook {hook_id})")
    return True


def _create_labels(client: httpx.Client, project_id: int, dry_run: bool = False) -> int:
    """Create Forge labels on a project. Returns count of labels created."""
    created = 0
    for label in FORGE_LABELS:
        if dry_run:
            print(f"  Project {project_id}: would create label '{label['name']}'")
            continue
        resp = client.post(f"/projects/{project_id}/labels", json=label)
        if resp.status_code == 409:
            continue  # already exists
        resp.raise_for_status()
        created += 1
    return created


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register GitLab webhooks and labels for Forge")
    parser.add_argument(
        "--project-id",
        type=int,
        action="append",
        default=[],
        help="Project ID to configure (repeatable)",
    )
    parser.add_argument(
        "--group-id",
        type=int,
        action="append",
        default=[],
        help="Group ID — registers on all projects in the group (CE-compatible)",
    )
    parser.add_argument(
        "--webhook-url",
        type=str,
        default=None,
        help="Public URL for the Forge webhook endpoint",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without making changes",
    )

    args = parser.parse_args(argv)

    if not args.project_id and not args.group_id:
        parser.error("At least one --project-id or --group-id is required")

    settings = Settings()  # type: ignore[call-arg]

    webhook_url = args.webhook_url or "http://localhost:8420/webhook"

    if args.dry_run:
        print("DRY RUN — no changes will be made\n")

    print("Connecting to GitLab...")
    client = _api_client(settings)
    try:
        _check_connectivity(client)
    except httpx.HTTPError as exc:
        print(f"  Failed to connect: {exc}", file=sys.stderr)
        return 1

    # Resolve group IDs to project IDs
    project_ids = list(args.project_id)
    for gid in args.group_id:
        print(f"\nFetching projects in group {gid}...")
        try:
            group_projects = _get_group_project_ids(client, gid)
            print(f"  Found {len(group_projects)} projects")
            project_ids.extend(group_projects)
        except httpx.HTTPError as exc:
            print(f"  Failed to fetch group {gid}: {exc}", file=sys.stderr)
            return 1

    if not project_ids:
        print("No projects to configure.")
        return 0

    # Deduplicate
    project_ids = sorted(set(project_ids))
    secret = settings.GITLAB_WEBHOOK_SECRET.get_secret_value()

    print(f"\nConfiguring {len(project_ids)} project(s)...\n")

    registered = 0
    for pid in project_ids:
        print(f"Project {pid}:")
        try:
            if _register_webhook(client, pid, webhook_url, secret, args.dry_run):
                registered += 1
            _create_labels(client, pid, args.dry_run)
        except httpx.HTTPError as exc:
            print(f"  Error: {exc}", file=sys.stderr)

    action = "would register" if args.dry_run else "registered"
    print(f"\nDone. Webhooks {action} on {registered}/{len(project_ids)} projects.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
