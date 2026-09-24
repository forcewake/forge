"""Canonical repository subjects for operator reads (R37-02, AT-03).

The operator authorization material is the CANONICAL subject — provider
family + connection identity + native repository identity — never a
display name. Pinned here, through the real ASGI routes with issued v2
grants and real FlowRun rows (AT-03's boundary: no projection-only
tests):

- the subject derivations from each family's OWN persisted columns
  (GitHub full name; GitLab/Azure ``project_id`` — no GitHub-column
  population), the recorded connection, and the refuses (unknown
  provider, nameless GitHub row);
- the v2 grant material: display-spelling changes and same-name
  different-connection rows cannot widen or shift it;
- the namespace collision: two runs with the SAME display name on
  different connections, plus a legitimate GitLab run — each token
  lists/reads/exports ONLY its subject, out-of-scope is a 404
  indistinguishable from unknown, and the GitLab row is visible through
  ITS canonical subject;
- the legacy name-only grant: unique resolution among CONFIGURED
  repositories, and fail-closed refusal with the reissue instruction
  (ambiguous name, unknown name, nothing configured);
- bounded pagination: page caps, cursor continuation in the same scope,
  cursor replay under a different grant refused, and the expensive
  checkpoint-authority reads touching ONLY the page's members.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from forge.adaptive.operator_snapshot import (
    PROVIDER_FAMILIES,
    UNRECORDED_CONNECTION,
    CanonicalSubject,
    subject_from_ref,
    subject_of_run,
)
from forge.api_operator import (
    LEGACY_GRANT_REFUSED,
    OPERATOR_SUBJECTS_STATE,
    operator_scope_token,
    operator_subject_material,
    operator_subject_scope_token,
    resolve_legacy_grant,
)
from forge.config import Settings
from forge.database import reset_engine
from forge.durable.models import FlowRun
from forge.main import create_app

SECRET = "operator-secret"  # noqa: S105 — fake value for tests
NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
NAME = "owner/alpha"  # the colliding DISPLAY name every subject shares

# connection A: the public GitHub tenant; connection B: a GHE host
GITHUB_COM = CanonicalSubject(
    provider_family="github", connection="github.com", native_id=NAME, display=NAME
)
GITHUB_ENTERPRISE = CanonicalSubject(
    provider_family="github", connection="ghe.corp", native_id=NAME, display=NAME
)
GITLAB_TEST = CanonicalSubject(
    provider_family="gitlab", connection="gitlab.test", native_id="77", display=NAME
)

RUN_COM = "1" * 32
RUN_GHE = "2" * 32
RUN_GITLAB = "3" * 32


# ---------------------------------------------------------------------------
# The subject derivations (pure, from each family's own columns)
# ---------------------------------------------------------------------------


def test_the_provider_families_mirror_the_composition_contract():
    from forge.runs.composition import PROVIDER_FAMILIES as composition_families

    assert PROVIDER_FAMILIES == composition_families


def test_a_github_row_derives_its_subject_from_the_full_name():
    run = FlowRun(
        id=RUN_COM,
        project_id=10,
        provider="github",
        github_repo_full_name="Owner/Alpha ",
    )
    subject = subject_of_run(run)
    assert subject is not None
    assert subject.provider_family == "github"
    assert subject.native_id == "Owner/Alpha"  # stripped, not re-spelled
    assert subject.connection == UNRECORDED_CONNECTION  # nothing recorded
    assert subject.subject_id() == f"github/{UNRECORDED_CONNECTION}/Owner/Alpha"


def test_a_recorded_connection_joins_the_subject():
    run = FlowRun(
        id=RUN_GHE,
        project_id=11,
        provider="github",
        github_repo_full_name=NAME,
        evidence={"connection": "https://GHE.corp/org"},
    )
    subject = subject_of_run(run)
    assert subject is not None
    assert subject.connection == "ghe.corp"  # scheme/path stripped, lowered
    assert subject.subject_id() == "github/ghe.corp/owner/alpha"


def test_a_gitlab_row_derives_its_subject_without_any_github_column():
    """The R37-02 read adapter: a GitLab run carries NO
    ``github_repo_full_name`` and is still a first-class subject through
    its OWN columns (``provider`` + ``project_id``)."""
    run = FlowRun(id=RUN_GITLAB, project_id=77, provider="gitlab")
    assert run.github_repo_full_name is None

    subject = subject_of_run(run)
    assert subject is not None
    assert subject.provider_family == "gitlab"
    assert subject.native_id == "77"
    assert subject.subject_id() == f"gitlab/{UNRECORDED_CONNECTION}/77"


def test_an_azure_row_derives_its_subject_from_the_project_id():
    run = FlowRun(id="4" * 32, project_id=909, provider="azure_devops")
    subject = subject_of_run(run)
    assert subject is not None
    assert subject.subject_id() == f"azure_devops/{UNRECORDED_CONNECTION}/909"


def test_rows_without_a_nameable_subject_are_outside_every_scope():
    assert subject_of_run(FlowRun(id="5" * 32, project_id=1, provider="fake")) is None
    assert subject_of_run(FlowRun(id="6" * 32, project_id=1, provider="github")) is None


def test_subject_refs_round_trip_and_refuse_malformed_spellings():
    for subject in (GITHUB_COM, GITHUB_ENTERPRISE, GITLAB_TEST):
        ref = subject_from_ref(subject.subject_id())
        assert ref.subject_id() == subject.subject_id()  # display stays out
    with pytest.raises(ValueError, match="canonical subject"):
        subject_from_ref("github/only-two-segments")
    with pytest.raises(ValueError):
        subject_from_ref("nosuchfamily/x/y")
    with pytest.raises(ValueError):
        CanonicalSubject(provider_family="github", connection="c", native_id=" ")


def test_the_v2_material_ignores_display_and_is_order_independent():
    """Display is presentation-only: two subjects with one display name
    but different connections (or families) produce DIFFERENT materials,
    and the material is a SET — grant order and duplicates change
    nothing."""
    assert operator_subject_material([GITHUB_COM]) != operator_subject_material([GITHUB_ENTERPRISE])
    assert operator_subject_material([GITHUB_COM]) != operator_subject_material([GITLAB_TEST])
    renamed = CanonicalSubject(
        provider_family="github",
        connection="github.com",
        native_id=NAME,
        display="totally-different",
    )
    assert operator_subject_material([renamed]) == operator_subject_material([GITHUB_COM])
    assert operator_subject_material([GITHUB_COM, GITLAB_TEST]) == operator_subject_material(
        [GITLAB_TEST, GITHUB_COM, GITLAB_TEST]
    )
    assert operator_subject_material([]) == "operator-scope-v2:[]"


def test_a_v2_token_cannot_sign_a_wider_or_different_scope():
    token = operator_subject_scope_token(SECRET, [GITHUB_COM])
    from forge.api_lane_control import verify_lane_token

    assert verify_lane_token(SECRET, token, operator_subject_material([GITHUB_COM]))
    assert not verify_lane_token(SECRET, token, operator_subject_material([GITHUB_ENTERPRISE]))
    assert not verify_lane_token(
        SECRET, token, operator_subject_material([GITHUB_COM, GITLAB_TEST])
    )


# ---------------------------------------------------------------------------
# The ASGI harness (AT-03's boundary: real routes, issued grants, real rows)
# ---------------------------------------------------------------------------


def _settings(tmp_path) -> Settings:
    return Settings(
        GITLAB_URL="https://gitlab.test",
        GITLAB_TOKEN=SecretStr("glpat-test"),
        GITLAB_WEBHOOK_SECRET=SecretStr("test-secret-token"),
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/operator.db",
        LITELLM_URL="http://litellm:4000",
        REDIS_URL=None,
        FORGE_CAPTURE_DIR=None,
        FORGE_BOT_TOKEN=None,
        FORGE_BOT_USERNAME="forge-bot",
        FORGE_LANE_CONTROL_SECRET=SecretStr(SECRET),
    )


def _github_run(run_id: str, connection: str) -> FlowRun:
    stable = {"https://github.com": 101, "https://ghe.corp": 202}.get(connection, 999)
    return FlowRun(
        id=run_id,
        project_id=stable,
        provider="github",
        github_repo_full_name=NAME,  # the SAME display name on both connections
        status="validating",
        base_sha="b" * 40,
        candidate_shas=[],
        plan_digest="p" * 64,
        evidence={"connection": connection},
        created_at=NOW - timedelta(hours=3),
        updated_at=NOW - timedelta(minutes=10),
    )


def _gitlab_run(run_id: str) -> FlowRun:
    return FlowRun(
        id=run_id,
        project_id=77,
        provider="gitlab",  # NO github_repo_full_name — invisible to name-only grants
        status="planning",
        base_sha="b" * 40,
        candidate_shas=[],
        plan_digest="q" * 64,
        evidence={"connection": "gitlab.test"},
        created_at=NOW - timedelta(hours=2),
        updated_at=NOW - timedelta(minutes=5),
    )


class RecordingRepository:
    """The injected checkpoint authority — its call log pins that the
    expensive per-run reads touch ONLY the page's members."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def entry(self, work_id: str) -> dict | None:
        self.calls.append(work_id)
        return None

    async def put(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003 — recorder
        self.calls.append(f"put:{args[0] if args else '?'}")

    async def read(self, work_id: str):
        return None

    async def lookup_outcome(self, work_id: str):
        return None

    async def authority(self) -> str:
        return "recording"

    async def pin(self, *args, **kwargs) -> bool:  # noqa: ANN002, ANN003 — recorder
        self.calls.append(f"pin:{args[0] if args else '?'}")
        return False

    async def unpin(self, *args, **kwargs) -> int:  # noqa: ANN002, ANN003 — recorder
        return 0

    async def pins(self, work_id: str | None = None) -> list[dict]:
        return []


def _headers(subjects: list[CanonicalSubject]) -> dict[str, str]:
    return {"Authorization": f"Bearer {operator_subject_scope_token(SECRET, subjects)}"}


def _query(subjects: list[CanonicalSubject], **params: str) -> str:
    parts = [f"subject={entry.subject_id()}" for entry in subjects]
    parts.extend(f"{key}={value}" for key, value in params.items())
    return "&".join(parts)


@pytest.fixture()
async def app(tmp_path):
    reset_engine()
    application = create_app(settings=_settings(tmp_path))
    async with application.router.lifespan_context(application):
        yield application
    reset_engine()


@pytest.fixture()
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://forge.test") as http:
        yield http


@pytest.fixture()
def repository(app) -> RecordingRepository:
    recording = RecordingRepository()
    app.state.operator_checkpoint_repository = recording
    return recording


async def _seed(app, *rows: FlowRun) -> None:
    async with app.state.session_factory() as session:
        session.add_all(rows)
        await session.commit()


@pytest.fixture()
async def collision(app):
    """The AT-03 world: the same display name on two GitHub connections
    plus a legitimate GitLab run — all three genuinely distinct subjects."""
    await _seed(
        app,
        _github_run(RUN_COM, "https://github.com"),
        _github_run(RUN_GHE, "https://ghe.corp"),
        _gitlab_run(RUN_GITLAB),
    )


# ---------------------------------------------------------------------------
# AT-03: each canonical token sees exactly its subject
# ---------------------------------------------------------------------------


async def test_at03_each_token_lists_only_its_own_subject(app, client, repository, collision):
    listed_com = await client.get(
        f"/operator/runs?{_query([GITHUB_COM])}", headers=_headers([GITHUB_COM])
    )
    assert listed_com.status_code == 200
    assert [run["run_id"] for run in listed_com.json()["runs"]] == [RUN_COM]

    listed_ghe = await client.get(
        f"/operator/runs?{_query([GITHUB_ENTERPRISE])}", headers=_headers([GITHUB_ENTERPRISE])
    )
    assert listed_ghe.status_code == 200
    assert [run["run_id"] for run in listed_ghe.json()["runs"]] == [RUN_GHE]

    listed_gl = await client.get(
        f"/operator/runs?{_query([GITLAB_TEST])}", headers=_headers([GITLAB_TEST])
    )
    assert listed_gl.status_code == 200
    assert [run["run_id"] for run in listed_gl.json()["runs"]] == [RUN_GITLAB]


async def test_at03_the_gitlab_row_is_visible_through_its_own_identity(
    app, client, repository, collision
):
    """The R37-02 headline: a run with NO github_repo_full_name is a
    first-class subject — readable on every surface through its canonical
    GitLab identity, while the GitHub-A grant still cannot see it."""
    for path in (
        f"/operator/runs/{RUN_GITLAB}",
        f"/operator/runs/{RUN_GITLAB}/support-bundle",
    ):
        own = await client.get(f"{path}?{_query([GITLAB_TEST])}", headers=_headers([GITLAB_TEST]))
        assert own.status_code == 200, path
        foreign = await client.get(f"{path}?{_query([GITHUB_COM])}", headers=_headers([GITHUB_COM]))
        assert foreign.status_code == 404, path  # out-of-scope ≡ unknown


async def test_at03_the_same_display_name_on_two_connections_stays_isolated(
    app, client, repository, collision
):
    """The P03 probe, closed: connection A's grant cannot list, read or
    export the identically-NAMED connection-B repository — and vice
    versa."""
    for grant, own, foreign in (
        (GITHUB_COM, RUN_COM, RUN_GHE),
        (GITHUB_ENTERPRISE, RUN_GHE, RUN_COM),
    ):
        assert grant.display == NAME  # the names genuinely collide
        listing = await client.get(f"/operator/runs?{_query([grant])}", headers=_headers([grant]))
        assert [run["run_id"] for run in listing.json()["runs"]] == [own]
        detail = await client.get(
            f"/operator/runs/{foreign}?{_query([grant])}", headers=_headers([grant])
        )
        assert detail.status_code == 404
        bundle = await client.get(
            f"/operator/runs/{foreign}/support-bundle?{_query([grant])}",
            headers=_headers([grant]),
        )
        assert bundle.status_code == 404
        own_detail = await client.get(
            f"/operator/runs/{own}?{_query([grant])}", headers=_headers([grant])
        )
        assert own_detail.status_code == 200
        assert own_detail.json()["subject"] == NAME  # the display spelling
        assert own_detail.json()["subject_id"] == grant.subject_id()  # the truth


async def test_at03_a_multi_subject_grant_reads_both_named_subjects_only(
    app, client, repository, collision
):
    both = [GITHUB_COM, GITLAB_TEST]
    listing = await client.get(f"/operator/runs?{_query(both)}", headers=_headers(both))
    assert listing.status_code == 200
    assert {run["run_id"] for run in listing.json()["runs"]} == {RUN_COM, RUN_GITLAB}
    # the GHE row — same display name as RUN_COM — stays invisible
    detail = await client.get(f"/operator/runs/{RUN_GHE}?{_query(both)}", headers=_headers(both))
    assert detail.status_code == 404


# ---------------------------------------------------------------------------
# The legacy name-only grant — map uniquely or fail closed
# ---------------------------------------------------------------------------


async def test_a_unique_legacy_name_resolves_to_its_one_canonical_subject(
    app, client, repository, collision
):
    """A v1 token whose declared name matches EXACTLY ONE configured
    repository keeps working — resolved to that canonical subject."""
    distinct = CanonicalSubject(
        provider_family="github",
        connection="github.com",
        native_id="owner/omega",
        display="owner/omega",
    )
    app.state.operator_subjects = [GITHUB_COM, GITLAB_TEST, distinct]
    legacy = {"Authorization": f"Bearer {operator_scope_token(SECRET, ['owner/omega'])}"}

    response = await client.get("/operator/runs?repo=owner/omega", headers=legacy)
    assert response.status_code == 200
    document = response.json()
    assert document["scope"] == [distinct.subject_id()]
    assert document["scope_version"] == 1  # the legacy grant, resolved


async def test_at03_an_ambiguous_legacy_name_fails_closed_with_the_reissue_instruction(
    app, client, repository, collision
):
    app.state.operator_subjects = [GITHUB_COM, GITHUB_ENTERPRISE, GITLAB_TEST]
    legacy = {"Authorization": f"Bearer {operator_scope_token(SECRET, [NAME])}"}

    response = await client.get(f"/operator/runs?repo={NAME}", headers=legacy)
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert LEGACY_GRANT_REFUSED in detail
    assert "more than one" in detail
    assert "reissue" in detail  # the administrator instruction


async def test_an_unknown_legacy_name_fails_closed(app, client, repository, collision):
    app.state.operator_subjects = [GITHUB_COM]
    legacy = {"Authorization": f"Bearer {operator_scope_token(SECRET, ['owner/nobody'])}"}

    response = await client.get("/operator/runs?repo=owner/nobody", headers=legacy)
    assert response.status_code == 403
    assert LEGACY_GRANT_REFUSED in response.json()["detail"]


async def test_a_legacy_grant_with_nothing_configured_fails_closed(
    app, client, repository, collision
):
    """No configured subjects at all → uniqueness is unprovable → the
    name-only grant refuses (never a live-database guess or a fan-out)."""
    assert getattr(app.state, OPERATOR_SUBJECTS_STATE, None) is None
    legacy = {"Authorization": f"Bearer {operator_scope_token(SECRET, [NAME])}"}

    response = await client.get(f"/operator/runs?repo={NAME}", headers=legacy)
    assert response.status_code == 403
    assert LEGACY_GRANT_REFUSED in response.json()["detail"]


def test_resolve_legacy_grant_is_pure_and_refuses_collisions():
    resolved = resolve_legacy_grant(
        ["owner/omega"],
        [
            GITHUB_COM,
            GITLAB_TEST,
            CanonicalSubject(
                provider_family="github",
                connection="github.com",
                native_id="owner/omega",
                display="owner/omega",
            ),
        ],
    )
    assert len(resolved) == 1
    with pytest.raises(ValueError, match=LEGACY_GRANT_REFUSED):
        resolve_legacy_grant([NAME], [GITHUB_COM, GITHUB_ENTERPRISE])
    with pytest.raises(ValueError, match=LEGACY_GRANT_REFUSED):
        resolve_legacy_grant([NAME], [])


# ---------------------------------------------------------------------------
# Bounded pagination — scope preserved, nothing pre-materialized
# ---------------------------------------------------------------------------


async def test_pages_are_bounded_and_the_cursor_continues_the_same_scope(app, client, repository):
    """N runs in scope, page size 3: every page carries at most 3, the
    cursor continues within the SAME scope, and the union of the pages is
    exactly the scope's runs — newest first throughout."""
    runs = []
    for index in range(7):
        runs.append(
            FlowRun(
                id=f"{index:032x}",
                project_id=77,
                provider="gitlab",
                status="planning",
                base_sha="b" * 40,
                candidate_shas=[],
                plan_digest="p" * 64,
                evidence={"connection": "gitlab.test"},
                created_at=NOW - timedelta(hours=4, minutes=index),
                updated_at=NOW - timedelta(minutes=30 - index),
            )
        )
    noise = [
        FlowRun(
            id=f"n{index:031x}",
            project_id=index,
            provider="gitlab",
            status="planning",
            base_sha="b" * 40,
            candidate_shas=[],
            plan_digest="p" * 64,
            evidence={"connection": "other.gitlab"},
            created_at=NOW - timedelta(hours=5),
            updated_at=NOW - timedelta(minutes=29 - index),
        )
        for index in range(5)
    ]
    await _seed(app, *(runs + noise))

    seen: list[str] = []
    cursor = ""
    pages = 0
    while True:
        query = _query([GITLAB_TEST], **({"cursor": cursor} if cursor else {}), limit="3")
        response = await client.get(f"/operator/runs?{query}", headers=_headers([GITLAB_TEST]))
        assert response.status_code == 200
        document = response.json()
        assert document["page_size"] == 3
        assert len(document["runs"]) <= 3
        # the expensive checkpoint authority read ONLY this page's members
        assert set(repository.calls[-len(document["runs"]) :]) <= {
            run["run_id"] for run in document["runs"]
        }
        seen.extend(run["run_id"] for run in document["runs"])
        cursor = document["next_cursor"]
        pages += 1
        if not cursor:
            break
        assert pages <= 5  # 7 runs over pages of 3 — bounded, no loop

    assert pages == 3
    assert set(seen) == {run.id for run in runs}  # every in-scope run, no noise
    assert len(seen) == len(set(seen))  # exactly once each


async def test_a_cursor_replayed_under_a_different_grant_is_refused(app, client, repository):
    """The continuation token is bound to its serving scope: replaying
    connection A's cursor while authenticating as connection B (or a
    different subject on the same connection) is a 400 — never B's page,
    never A's either."""
    second = CanonicalSubject(
        provider_family="github", connection="github.com", native_id="owner/second"
    )
    await _seed(
        app,
        _github_run(RUN_COM, "https://github.com"),
        FlowRun(
            id="7" * 32,
            project_id=102,
            provider="github",
            github_repo_full_name="owner/second",
            status="validating",
            base_sha="b" * 40,
            candidate_shas=[],
            plan_digest="p" * 64,
            evidence={"connection": "https://github.com"},
            created_at=NOW - timedelta(hours=3),
            updated_at=NOW - timedelta(minutes=11),
        ),
        _github_run(RUN_GHE, "https://ghe.corp"),
        _gitlab_run(RUN_GITLAB),
    )
    wide = [GITHUB_COM, second]
    first = await client.get(f"/operator/runs?{_query(wide)}&limit=1", headers=_headers(wide))
    assert {run["run_id"] for run in first.json()["runs"]} == {RUN_COM}
    cursor = first.json()["next_cursor"]
    assert cursor

    for other in (GITHUB_ENTERPRISE, GITLAB_TEST, [GITHUB_COM]):
        subjects = other if isinstance(other, list) else [other]
        replay = await client.get(
            f"/operator/runs?{_query(subjects)}&limit=1&cursor={cursor}",
            headers=_headers(subjects),
        )
        assert replay.status_code == 400
        assert "does not belong to this subject scope" in replay.json()["detail"]


async def test_a_malformed_cursor_is_a_400(app, client, repository):
    response = await client.get(
        f"/operator/runs?{_query([GITHUB_COM])}&cursor=%%%", headers=_headers([GITHUB_COM])
    )
    assert response.status_code == 400


async def test_the_page_size_is_capped(app, client, repository):
    response = await client.get(
        f"/operator/runs?{_query([GITHUB_COM])}&limit=100000", headers=_headers([GITHUB_COM])
    )
    assert response.status_code == 422  # FastAPI's bound: le=MAX_PAGE_SIZE


async def test_reads_never_pin_mutate_or_launch(app, client, repository, collision):
    """The read-only charter at the API boundary: across list, detail and
    bundle GETs the checkpoint authority sees only ``entry`` reads, and
    the durable FlowRun rows are untouched."""
    from sqlalchemy import func, select

    for grant in (GITHUB_COM, GITHUB_ENTERPRISE, GITLAB_TEST):
        listing = await client.get(f"/operator/runs?{_query([grant])}", headers=_headers([grant]))
        assert listing.status_code == 200
        for run in listing.json()["runs"]:
            for path in (
                f"/operator/runs/{run['run_id']}",
                f"/operator/runs/{run['run_id']}/support-bundle",
            ):
                response = await client.get(f"{path}?{_query([grant])}", headers=_headers([grant]))
                assert response.status_code == 200, path

    assert set(repository.calls) == {RUN_COM, RUN_GHE, RUN_GITLAB}  # entry reads only
    async with app.state.session_factory() as session:
        count = int(await session.scalar(select(func.count()).select_from(FlowRun)) or 0)
    assert count == 3


async def test_a_path_shaped_native_id_fails_closed_not_wide(app, client, repository, collision):
    """A GitLab subject granted by full path (not the numeric project id
    the durable row carries) selects NOTHING — a refused quiet scope,
    never a crash and never a widened match."""
    path_shaped = CanonicalSubject(
        provider_family="gitlab", connection="gitlab.test", native_id="group/sub/project"
    )
    listing = await client.get(
        f"/operator/runs?{_query([path_shaped])}", headers=_headers([path_shaped])
    )
    assert listing.status_code == 200
    assert listing.json()["runs"] == []
    detail = await client.get(
        f"/operator/runs/{RUN_GITLAB}?{_query([path_shaped])}", headers=_headers([path_shaped])
    )
    assert detail.status_code == 404
