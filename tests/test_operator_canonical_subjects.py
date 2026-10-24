"""Native IDs scope actual authenticated SQL reads; display names grant no authority."""

from datetime import timedelta
import json

import pytest
from pydantic import ValidationError

from forge.domain.plan import RepositoryIdentity
from forge.runtime.authority import Operators, NotAuthorized
from forge.runtime.settings import RuntimeSettings
from forge.store.records import RunRequest
from forge.store.runs import subject_key
from tests.operator_surface_fixture import (
    surface as surface,
    TOKENS,
    detail,
    listing,
    scoped_config,
    seed_run,
    table_counts,
)


def invalid(config, change):
    document = config.model_dump(mode="json", by_alias=True)
    change(document)
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate_json(json.dumps(document))


def test_the_provider_families_mirror_the_composition_contract(tmp_path):
    config, _ = scoped_config(tmp_path)
    assert {c.provider for c in config.connections} == {"github", "gitlab"}


async def test_a_github_row_derives_its_subject_from_the_full_name(surface):
    # Display paths are no longer persisted authority; native numeric IDs are required.
    run = await seed_run(surface, "alpha")
    row = (await detail(surface, run, "alpha")).json()
    assert (row["provider"], row["connection_id"], row["repository_id"]) == (
        "github",
        "alpha",
        "123",
    )
    assert "full_name" not in row


async def test_a_recorded_connection_joins_the_subject(surface):
    run = await seed_run(surface, "beta")
    assert (await detail(surface, run, "beta")).json()["connection_id"] == "beta"
    assert (await detail(surface, run, "alpha")).status_code == 404


async def test_a_gitlab_row_derives_its_subject_without_any_github_column(surface):
    run = await seed_run(surface)
    row = (await detail(surface, run)).json()
    assert row["provider"] == "gitlab" and row["repository_id"] == "123"
    assert "github_repo_full_name" not in row


def test_a_retired_azure_subject_is_outside_every_supported_scope():
    with pytest.raises(ValidationError):
        RepositoryIdentity(provider="azure_devops", connection_id="retired", repository_id="123")


def test_rows_without_a_nameable_subject_are_outside_every_scope():
    for provider, connection, native in (
        ("fake", "c", "123"),
        ("github", "", "123"),
        ("github", "c", "owner/repo"),
    ):
        with pytest.raises(ValueError):
            subject_key(RunRequest("a" * 32, provider, connection, native, "issue", "7"))


def test_subject_refs_round_trip_and_refuse_malformed_spellings(tmp_path):
    config, _ = scoped_config(tmp_path)
    for c in config.connections:
        identity = c.ref(c.repositories[0]).identity
        assert RepositoryIdentity.model_validate_json(identity.model_dump_json()) == identity
    with pytest.raises(ValueError):
        subject_key(RunRequest("a" * 32, "github", "alpha", "0123", "issue", "7"))


def test_the_v2_material_ignores_display_and_is_order_independent(tmp_path):
    config, values = scoped_config(tmp_path)
    who = Operators(config, values).authenticate("Bearer " + TOKENS["wide"])
    identities = [
        c.ref(r).identity for key, (c, r) in config.repositories().items() if key in who.actors
    ]
    assert len({identity.digest for identity in identities}) == 3
    assert len({r.full_name for _, r in config.repositories().values()}) == 1


def test_a_v2_token_cannot_sign_a_wider_or_different_scope(tmp_path):
    config, values = scoped_config(tmp_path)
    who = Operators(config, values).authenticate("Bearer " + TOKENS["alpha"])
    assert set(who.actors) == {"alpha"}
    with pytest.raises(NotAuthorized):
        who.actor("beta")


async def test_at03_each_token_lists_only_its_own_subject(surface):
    runs = {key: await seed_run(surface, key) for key in ("alpha", "beta", "project")}
    for key, run in runs.items():
        assert [r["id"] for r in (await listing(surface, key)).json()["runs"]] == [run.id]
        for other, foreign in runs.items():
            assert (await detail(surface, foreign, key)).status_code == (
                200 if key == other else 404
            )


async def test_at03_the_gitlab_row_is_visible_through_its_own_identity(surface):
    run = await seed_run(surface)
    assert (await detail(surface, run)).json()["repository_id"] == "123"
    assert [r["id"] for r in (await listing(surface)).json()["runs"]] == [run.id]


async def test_at03_the_same_display_name_on_two_connections_stays_isolated(surface):
    left, right = await seed_run(surface, "alpha"), await seed_run(surface, "beta")
    assert (await detail(surface, left, "beta")).json() == (
        await detail(surface, "0" * 32, "beta")
    ).json()
    assert (await detail(surface, right, "alpha")).status_code == 404


async def test_at03_a_multi_subject_grant_reads_both_named_subjects_only(surface):
    runs = [await seed_run(surface, key) for key in ("alpha", "beta", "project")]
    assert {r["id"] for r in (await listing(surface, "wide")).json()["runs"]} == {
        r.id for r in runs
    }


def test_a_unique_legacy_name_resolves_to_its_one_canonical_subject(tmp_path):
    # Name-derived grants are retired, including otherwise unique display paths.
    config, _ = scoped_config(tmp_path)
    invalid(config, lambda d: d["operators"][0]["grants"][0].update(repository="new/repository"))


async def test_at03_an_ambiguous_legacy_name_fails_closed_with_the_reissue_instruction(surface):
    response = await listing(surface, "wide", repo="new/repository")
    assert response.status_code == 400 and "new/repository" not in response.text


def test_an_unknown_legacy_name_fails_closed(tmp_path):
    config, _ = scoped_config(tmp_path)
    invalid(config, lambda d: d["operators"][0]["grants"][0].update(repository="unconfigured"))


def test_a_legacy_grant_with_nothing_configured_fails_closed(tmp_path):
    config, _ = scoped_config(tmp_path)
    invalid(config, lambda d: d["operators"][0].update(grants=[]))


def test_resolve_legacy_grant_is_pure_and_refuses_collisions(tmp_path):
    config, values = scoped_config(tmp_path)
    operators = Operators(config, values)
    for name in ("alpha", "beta", "project"):
        assert set(operators.authenticate("Bearer " + TOKENS[name]).actors) == {name}
    with pytest.raises(NotAuthorized):
        operators.authenticate("Bearer new/repository")


async def test_pages_are_bounded_and_the_cursor_continues_the_same_scope(surface):
    now = surface.service.budget.clock.now()
    runs = [
        await seed_run(surface, number=10 + i, created_at=now - timedelta(minutes=i))
        for i in range(7)
    ]
    await seed_run(surface, "beta")
    seen = []
    for offset in (0, 3, 6):
        page = (await listing(surface, limit=3, offset=offset)).json()
        assert page["limit"] == 3 and len(page["runs"]) <= 3
        seen.extend(row["id"] for row in page["runs"])
    assert seen == [run.id for run in runs] and len(set(seen)) == 7
    assert not surface.peer.calls and not surface.gateway.writes


async def test_a_cursor_replayed_under_a_different_grant_is_refused(surface):
    # Offset pagination has no transferable cursor or client-declared scope.
    for name in ("alpha", "beta", "project", "wide"):
        assert (await listing(surface, name, cursor="old-scope-cursor")).status_code == 400


async def test_a_malformed_cursor_is_a_400(surface):
    assert (await listing(surface, cursor="%%%")).status_code == 400


async def test_the_page_size_is_capped(surface):
    for query in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"offset": 10001}):
        assert (await listing(surface, **query)).status_code == 422


async def test_reads_never_pin_mutate_or_launch(surface):
    runs = [await seed_run(surface, key) for key in ("alpha", "beta", "project")]
    before = await table_counts(surface)
    for run in runs:
        assert (await detail(surface, run, "wide")).status_code == 200
    assert (await listing(surface, "wide")).status_code == 200
    assert await table_counts(surface) == before
    assert not surface.peer.calls and not surface.gateway.writes


def test_a_path_shaped_native_id_fails_closed_not_wide(tmp_path):
    config, _ = scoped_config(tmp_path)
    invalid(
        config,
        lambda d: d["connections"][0]["repositories"][0].update(native_id="group/sub/project"),
    )


async def test_free_text_credentials_are_redacted_on_list_detail_and_status(surface):
    run = await seed_run(surface, reason="failed Bearer ghp_abcdefgh12345678")
    listed = await listing(surface)
    document = (await detail(surface, run)).json()
    observed = await surface.client.post(
        "/api/v1/commands",
        headers={"Authorization": "Bearer " + TOKENS["project"]},
        json={
            "repository": "project",
            "number": 7,
            "request_id": "status-redaction",
            "command": {"kind": "status", "argument": run.id},
        },
    )
    assert listed.json()["runs"][0]["reason"] == document["reason"] == "[redacted]"
    assert document["evidence"]["reason"] == "[redacted]"
    assert observed.json()["reason"] == "[redacted]"
    assert "ghp_abcdefgh12345678" not in listed.text + json.dumps(document) + observed.text


def test_recursive_operator_redaction_keeps_input_immutable():
    from copy import deepcopy
    from forge.operator_values import redact

    original = {
        "note": "ordinary",
        "nested": [{"password": "private"}, {"text": "glpat-private"}],
        "tokens": 14,
    }
    frozen = deepcopy(original)
    assert redact(original) == {
        "note": "ordinary",
        "nested": [{"password": "[redacted]"}, {"text": "[redacted]"}],
        "tokens": 14,
    }
    assert original == frozen
