"""Read-only planning resolves real Git blob lines without provider or model calls."""

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from forge.domain.budget import Observation
from forge.domain.plan import Plan, RepositoryIdentity
from forge.planning import PlanningLimits, PlanningRefused, ResearchPlanner, freeze_snapshot
from forge.planning.tools import ReadTools, ToolCall
from forge.planning.planner import decode_reply
from forge.port_values import (
    ModelReply,
    NeighborSnapshot,
    PlanningSnapshot,
    ReadUnavailable,
    RepoRef,
    SubjectRef,
    TaskSnapshot,
    Tree,
    TreeEntry,
)
from forge_protocol import canonical_json

BASE = "a" * 40
GOLDEN = Path(__file__).parent / "fixtures/planning/plan-v1.json"
SOURCE = b"def run(x):\n    return x + 1\n\nrun(1)\n"


def repo(number="123", **changes):
    return RepoRef(
        identity=RepositoryIdentity(
            provider="github", connection_id="installation-9", repository_id=number
        ),
        full_name=changes.get("full_name", "owner/repo"),
    )


def snapshot(*, globs=("**",), neighbors=()):
    return PlanningSnapshot(
        primary=NeighborSnapshot(repo=repo(), sha=BASE, allowed_globs=globs), neighbors=neighbors
    )


class Reader:
    def __init__(self, data=None):
        self.files = data or {"123": {"src/main.py": SOURCE}}
        self.calls = []
        self.unavailable = self.wrong_tree = self.corrupt = False
        self.symlinks = set()

    async def head(self, repository, branch):
        self.calls.append(("head", repository.identity.repository_id, branch))
        return BASE if branch != "missing" else None

    async def read_tree(self, repository, sha, prefix=""):
        self.calls.append(("tree", repository.identity.repository_id, sha))
        if self.unavailable:
            raise ReadUnavailable("provider unavailable", "synthetic failure")
        entries = []
        for path, data in self.files[repository.identity.repository_id].items():
            oid = (
                subprocess.run(
                    ["git", "hash-object", "--stdin"], input=data, capture_output=True, check=True
                )
                .stdout.decode()
                .strip()
            )
            entries.append(
                TreeEntry(
                    path=path,
                    sha=oid,
                    kind="blob",
                    mode="120000" if path in self.symlinks else "100644",
                    size=len(data),
                )
            )
        return Tree(
            repo=repository, sha="b" * 40 if self.wrong_tree else sha, entries=tuple(entries)
        )

    async def read_blob(self, repository, sha, path):
        self.calls.append(("blob", repository.identity.repository_id, sha, path))
        data = self.files[repository.identity.repository_id].get(path)
        return b"different bytes" if self.corrupt else data


class Model:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def complete(self, **arguments):
        self.calls.append(arguments)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return ModelReply(
            text=json.dumps(reply),
            usage=Observation(usd=None, calls=None, tokens=None),
            evidence="synthetic model reply",
        )


def task():
    return TaskSnapshot(
        subject=SubjectRef(repo=repo(), number=1),
        title="Correct run",
        description="Keep all final steps",
        version="issue-version-1",
    )


def read_round(**changes):
    return {
        "kind": "tools",
        "calls": [{"tool": "read_file", "repo": "primary", "path": "src/main.py", **changes}],
    }


async def test_only_frozen_authorized_paths_reach_tools_or_provider():
    private = [
        ".forge/state",
        ".env.production",
        ".ssh/id_rsa",
        ".aws/credentials",
        ".kube/config",
        "private.pem",
        "tls.key",
        "credentials",
        ".mcp.json",
        ".copilot/settings.json",
    ]
    data = {
        "src/main.py": SOURCE,
        "hidden/no-match.py": b"never disclosed\n",
        **{path: b"never disclosed\n" for path in private},
    }
    reader = Reader({"123": data})
    tools = ReadTools(reader, snapshot(globs=("src/**",)), PlanningLimits())
    listed = await tools.call(ToolCall(tool="list_paths", repo="primary"))
    assert listed == {"paths": ["src/main.py"], "complete": True, "truncated": False}
    for path in ("missing.py", "hidden/no-match.py", *private):
        with pytest.raises(PlanningRefused, match="read_not_available"):
            await tools.lines("primary", path)
    with pytest.raises(PlanningRefused, match="read_not_available"):
        await tools.lines("unconfigured-neighbor", "src/main.py")
    result = await tools.call(ToolCall(tool="grep", repo="primary", query="never disclosed"))
    assert result["matches"] == [] and result["complete"]
    assert all(call[2] == BASE for call in reader.calls if call[0] in ("tree", "blob"))
    assert not any(call[0] == "head" for call in reader.calls)
    assert not any(call[-1] != "src/main.py" for call in reader.calls if call[0] == "blob")
    broad = ReadTools(reader, snapshot(), PlanningLimits())
    visible = await broad.call(ToolCall(tool="list_paths", repo="primary"))
    assert not set(private) & set(visible["paths"])


async def test_citations_resolve_real_blob_lines_and_refuse_wrong_identity_or_range():
    reader = Reader()
    tools = ReadTools(reader, snapshot(), PlanningLimits())
    await tools.call(ToolCall(tool="read_file", repo="primary", path="src/main.py"))
    plan = Plan.model_validate_json(GOLDEN.read_bytes())
    await tools.validate_citations(plan)
    assert (
        hashlib.sha1(b"blob " + str(len(SOURCE)).encode() + b"\0" + SOURCE).hexdigest()
        == (await tools.entries("primary"))["src/main.py"].sha
    )
    for change in (
        {"source_sha": "b" * 40},
        {"end_line": 999},
        {"path": "missing.py"},
        {"path": ".env.production"},
        {
            "repository": {
                "provider": "github",
                "connection_id": "other-connection",
                "repository_id": "123",
            }
        },
    ):
        changed = json.loads(GOLDEN.read_text())
        changed["steps"][0]["citations"][0].update(change)
        with pytest.raises(PlanningRefused):
            await tools.validate_citations(Plan.model_validate_json(json.dumps(changed)))
    unseen = ReadTools(Reader(), snapshot(), PlanningLimits())
    await unseen.call(ToolCall(tool="read_file", repo="primary", path="src/main.py", line_count=1))
    with pytest.raises(PlanningRefused, match="unobserved_citation"):
        await unseen.validate_citations(plan)


@pytest.mark.parametrize("fault", ["wrong_tree", "corrupt", "unavailable", "symlink"])
async def test_provider_identity_failures_never_resolve_a_citation(fault):
    reader = Reader()
    if fault == "symlink":
        reader.symlinks.add("src/main.py")
    else:
        setattr(reader, fault, True)
    with pytest.raises(PlanningRefused):
        await ReadTools(reader, snapshot(), PlanningLimits()).call(
            ToolCall(tool="read_file", repo="primary", path="src/main.py")
        )


async def test_utf8_line_paging_and_partial_search_are_honest():
    content = ("λ" * 40 + "\r\n") * 9 + "last\vstill-the-same-line"
    reader = Reader({"123": {"src/main.py": content.encode(), "src/tail.py": b"lambda\n"}})
    tools = ReadTools(reader, snapshot(), PlanningLimits(tool_output_bytes=256, scan_files=1))
    pages, start = [], 1
    while True:
        page = await tools.call(
            ToolCall(tool="read_file", repo="primary", path="src/main.py", start_line=start)
        )
        assert len(canonical_json(page)) <= 256
        pages.append(page["content"])
        if page["next_line"] is None:
            assert page["complete"]
            break
        assert not page["complete"] and page["next_line"] > start
        start = page["next_line"]
    assert "".join(pages) == content
    result = await tools.call(ToolCall(tool="grep", repo="primary", query="λ"))
    assert not result["complete"] and len(canonical_json(result)) <= 256


async def test_gateway_planner_keeps_full_plan_and_refuses_unavailable_or_widened_tools(
    monkeypatch,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-used")
    golden = json.loads(GOLDEN.read_text())
    model = Model([read_round(), {"kind": "plan", "plan": golden}])
    planner = ResearchPlanner(
        Reader(), model, model="planning-route", credential_alias="server-planning-alias"
    )
    plan = await planner.plan(task(), snapshot())
    assert plan.model_dump(mode="json") == golden
    assert "retain this entire final instruction" in plan.render()
    assert len(model.calls) == 2
    assert all(
        call["model"] == "planning-route" and call["credential_alias"] == "server-planning-alias"
        for call in model.calls
    )
    assert "must-not-be-used" not in model.calls[1]["prompt"]
    assert "def run(x)" in model.calls[1]["prompt"]
    for reply, reason in (
        (ReadUnavailable("gateway unavailable", "synthetic"), "planner_model_unavailable"),
        (
            {"kind": "tools", "calls": [{"tool": "shell", "repo": "primary"}]},
            "invalid_planner_output",
        ),
        (
            {"kind": "blocked", "question": "Which contract applies?"},
            "planner_requires_clarification",
        ),
    ):
        failed_model = Model([reply])
        with pytest.raises(PlanningRefused, match=reason):
            await ResearchPlanner(
                Reader(), failed_model, model="route", credential_alias="server-alias"
            ).plan(task(), snapshot())
        assert len(failed_model.calls) == 1


async def test_neighbor_authorization_is_explicit_and_freezes_heads_once():
    reader = Reader(
        {"123": {"src/main.py": SOURCE}, "124": {"lib/helper.py": b"def helper():\n    return 1\n"}}
    )
    catalog = {
        ("github", "installation-9", "124"): repo("124", full_name="owner/library"),
        ("github", "installation-9", "125"): repo("125"),
    }
    frozen = await freeze_snapshot(
        reader,
        snapshot().primary,
        [
            {
                "provider": "github",
                "connection_id": "installation-9",
                "repository_id": "124",
                "ref": "main",
                "allowed_globs": ["lib/**"],
            }
        ],
        catalog,
    )
    assert reader.calls == [("head", "124", "main")]
    tools = ReadTools(reader, frozen, PlanningLimits())
    observed = await tools.call(ToolCall(tool="read_file", repo="neighbor-1", path="lib/helper.py"))
    assert "def helper" in observed["content"]
    assert reader.calls[1:][0] == ("tree", "124", BASE)
    neighbor_plan = json.loads(GOLDEN.read_text())
    neighbor_plan["steps"] = neighbor_plan["steps"][:1]
    neighbor_plan["steps"][0]["citations"][0].update(
        repository=catalog[("github", "installation-9", "124")].identity.model_dump(),
        path="lib/helper.py",
    )
    await tools.validate_citations(Plan.model_validate_json(json.dumps(neighbor_plan)))
    assert len((await freeze_snapshot(reader, snapshot().primary, None, catalog)).neighbors) == 0
    empty = await freeze_snapshot(
        reader,
        snapshot().primary,
        [
            {
                "provider": "github",
                "connection_id": "installation-9",
                "repository_id": "124",
                "allowed_globs": [],
            }
        ],
        catalog,
    )
    assert empty.neighbors[0].allowed_globs == ()
    assert (
        await ReadTools(reader, empty, PlanningLimits()).call(
            ToolCall(tool="list_paths", repo="neighbor-1")
        )
    )["paths"] == []
    for raw in (
        [{"provider": "github", "connection_id": "installation-9", "repository_id": "999"}],
        [
            {
                "provider": "github",
                "connection_id": "installation-9",
                "repository_id": "124",
                "writable": True,
            }
        ],
        [
            {
                "provider": "github",
                "connection_id": "installation-9",
                "repository_id": "124",
                "ref": "missing",
            }
        ],
        [{"provider": "github", "connection_id": "installation-9", "repository_id": "124"}] * 2,
    ):
        with pytest.raises(PlanningRefused):
            await freeze_snapshot(reader, snapshot().primary, raw, catalog)


async def test_neighbor_connections_remain_distinct_and_mismatched_catalog_identity_refuses():
    first = RepoRef(
        identity=RepositoryIdentity(
            provider="gitlab", connection_id="tenant-a", repository_id="42"
        ),
        full_name="team/library",
    )
    second = RepoRef(
        identity=RepositoryIdentity(
            provider="gitlab", connection_id="tenant-b", repository_id="42"
        ),
        full_name="team/library",
    )
    keys = [("gitlab", "tenant-a", "42"), ("gitlab", "tenant-b", "42")]
    records = [value.identity.model_dump() for value in (first, second)]

    class Connections(Reader):
        def __init__(self):
            super().__init__()
            self.identities = []

        async def head(self, repository, branch):
            self.identities.append(repository.identity)
            return await super().head(repository, branch)

    reader = Connections()
    frozen = await freeze_snapshot(
        reader, snapshot().primary, records, dict(zip(keys, (first, second), strict=True))
    )
    assert tuple(neighbor.repo.identity for neighbor in frozen.neighbors) == (
        first.identity,
        second.identity,
    )
    assert reader.identities == [first.identity, second.identity]
    assert (
        frozen.neighbors[0].repo.identity.repository_id
        == frozen.neighbors[1].repo.identity.repository_id
    )
    for raw, catalog in (
        ([records[0]], {keys[0]: second}),
        ([records[1]], {keys[0]: first}),
        ([{**records[0], "repository_id": "43"}], {("gitlab", "tenant-a", "43"): first}),
        ([{**records[0], "provider": "github"}], {("github", "tenant-a", "42"): first}),
        ([{"provider": "gitlab", "repository_id": "42"}], {keys[0]: first}),
        ([{**records[0], "repository_id": "team/library"}], {keys[0]: first}),
    ):
        rejected = Connections()
        with pytest.raises(PlanningRefused):
            await freeze_snapshot(rejected, snapshot().primary, raw, catalog)
        assert not rejected.identities and not rejected.calls


@pytest.mark.parametrize("boundary", ["model", "tool", "input", "output", "uncited"])
async def test_work_bounds_and_uncited_plans_never_trigger_a_hidden_retry(boundary):
    reader = Reader()
    limits = PlanningLimits()
    reply = read_round()
    reason = "planning_model_limit"
    if boundary == "model":
        limits = PlanningLimits(model_calls=1)
    elif boundary == "tool":
        limits = PlanningLimits(tool_calls=1)
        reply["calls"] *= 2
        reason = "planning_tool_limit"
    elif boundary == "input":
        limits = PlanningLimits(prompt_bytes=1)
        reason = "planning_input_limit"
    elif boundary == "output":
        limits = PlanningLimits(response_bytes=1)
        reason = "invalid_planner_output"
    else:
        golden = json.loads(GOLDEN.read_text())
        golden["steps"][0]["citations"] = []
        reply = {"kind": "plan", "plan": golden}
        reason = "plan_has_no_citation"
    model = Model([reply])
    planner = ResearchPlanner(
        reader, model, model="server-route", credential_alias="scoped-alias", limits=limits
    )
    with pytest.raises(PlanningRefused, match=reason):
        await planner.plan(task(), snapshot())
    assert len(model.calls) == (0 if boundary == "input" else 1)
    if boundary != "model":
        assert not reader.calls


@pytest.mark.parametrize(
    "raw",
    [
        '{"kind":"blocked","kind":"tools","calls":[]}',
        '{"kind":"tools","calls":[{"tool":"read_file","repo":"primary","path":"src/main.py","sha":"unbound"}]}',
        '```json\n{"kind":"blocked","question":"not a pure object"}\n```',
    ],
)
def test_duplicate_or_unbound_model_authority_refuses(raw):
    with pytest.raises(PlanningRefused, match="invalid_planner_output"):
        decode_reply(raw, 65536)
