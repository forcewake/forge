"""Retained publication certainty lessons use current journals and native Git CAS."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from forge_protocol import canonical_json

from forge.domain.plan import RepositoryIdentity
from forge.domain.verdicts import Committed, Conflict, Unknown
from forge.engine.effects import Effects
from forge.port_values import Found, Inconclusive, RepoRef
from forge.providers.fake import Blob, Fault
from forge.providers.fake.state import commit_value
from forge.providers.git_commit import GitTarget, publish
from forge.providers.git_process import GitFailure, GitProcess
from forge.providers.gitlab.commit import GitLabCommitter
from forge.store.effects import EffectStore
from forge.store.models_execution import EngineStep
from forge.store.records import RunRequest, StaleLease, StoreConflict
from forge.store.runs import RunStore
from tests import test_github_reads as reads
from tests import test_native_git_publication as git_fixture
from tests.native_store_fixture import native_store as native_store
from tests.publication_journal_fixture import journal as journal
from tests.test_engine_commands import commands as commands, envelope
from tests.test_engine_delivery import delivery as delivery, completed
from tests.test_engine_lane_handlers import case as case, env as env, settings as settings, started
from tests.test_engine_operator import operator as operator
from tests.test_github_native_commit import (
    connected as connected,
    app_key as app_key,
    native as native,
)


async def native_commit(connected, marker="current-operation"):
    writer, repo, wire, server, seed, base, calls = connected
    candidate = git_fixture.candidate(base)
    result = await writer.commit(reads.REPO, candidate, base, marker)
    assert isinstance(result, Committed)
    return repo, wire, base, candidate, result


class Proxy:
    def __init__(self, native):
        self.native = native

    def __getattr__(self, name):
        return getattr(self.native, name)


async def native_race(native, monkeypatch, *, first_lands_first=False):
    server, _, base, _ = native
    original, entered, release = GitProcess.run, asyncio.Event(), asyncio.Event()
    first = True

    async def delayed(self, *args, **kwargs):
        nonlocal first
        if args[0] == "push" and first:
            first = False
            entered.set()
            await release.wait()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(GitProcess, "run", delayed)
    target = GitTarget(git_fixture.URL, "Basic synthetic-credential")
    candidate = git_fixture.candidate(base)
    pending = asyncio.create_task(
        publish(target, candidate, base, "first-effect", stamp=git_fixture.STAMP)
    )
    await entered.wait()
    if first_lands_first:
        release.set()
        first_result = await pending
        with pytest.raises(GitFailure) as conflict:
            await publish(target, candidate, base, "second-effect", stamp=git_fixture.STAMP + 1)
        assert conflict.value.stale
        winner = first_result
    else:
        winner = await publish(
            target, candidate, base, "second-effect", stamp=git_fixture.STAMP + 1
        )
        release.set()
        with pytest.raises(GitFailure) as conflict:
            await pending
        assert conflict.value.stale
    assert (
        git_fixture.git(server, "rev-parse", "refs/heads/" + git_fixture.BRANCH).decode() == winner
    )
    assert git_fixture.git(server, "rev-list", "--count", base + ".." + winner) == b"1"
    return winner


async def ambiguous_native_probe(connected, monkeypatch):
    repo, _, base, candidate, _ = await native_commit(connected)
    original = repo.api._transport.request

    async def ambiguous(method, path, *args, **kwargs):
        document, request_id = await original(method, path, *args, **kwargs)
        if path.endswith("/commits"):
            twin = {**document[0], "sha": "e" * 40}
            document = [*document, twin]
        return document, request_id

    monkeypatch.setattr(repo.api._transport, "request", ambiguous)
    proof = await repo.probe_commit(
        reads.REPO, git_fixture.BRANCH, "current-operation", base, candidate
    )
    return proof


class TestProbeDecisionTable:
    async def test_exactly_one_marker_and_parent_match_adopts(self, connected):
        repo, _, base, candidate, result = await native_commit(connected)
        proof = await repo.probe_commit(
            reads.REPO, git_fixture.BRANCH, "current-operation", base, candidate
        )
        assert isinstance(proof, Found) and proof.value.sha == result.sha

    async def test_previous_repair_key_is_never_mistaken_for_this_intent(self, connected):
        repo, _, base, candidate, _ = await native_commit(connected, "previous-operation")
        proof = await repo.probe_commit(
            reads.REPO, git_fixture.BRANCH, "new-operation", base, candidate
        )
        assert isinstance(proof, Inconclusive)

    async def test_right_marker_wrong_parent_is_not_a_match(self, connected):
        repo, _, _, candidate, _ = await native_commit(connected)
        proof = await repo.probe_commit(
            reads.REPO, git_fixture.BRANCH, "current-operation", "f" * 40, candidate
        )
        assert not (isinstance(proof, Found) and isinstance(proof.value, Committed))

    async def test_two_matches_are_unknown(self, connected, monkeypatch):
        assert isinstance(await ambiguous_native_probe(connected, monkeypatch), Inconclusive)

    async def test_zero_matches_head_intact_is_not_permission_before_call_window_closes(
        self, journal
    ):
        row = await journal.crash("after_t2")
        recovered = await journal.engine().recover(row)
        assert recovered.state == "unknown" and journal.writes() == 0

    async def test_zero_matches_head_moved_is_duplicated(self, journal):
        await journal.crash("after_t2")
        human = commit_value({"a.py": Blob(b"human\n")}, journal.base, "Human commit")
        repo = journal.provider.repo(journal.repo)
        repo.commits[human.sha], repo.branches[journal.candidate.changeset.branch] = (
            human,
            human.sha,
        )
        recovered = await journal.engine().recover(await journal.row())
        assert (
            recovered.state == "adopted"
            and json.loads(recovered.outcome_json)["kind"] == "conflict"
        )
        assert journal.writes() == 0

    async def test_missing_exact_parent_cannot_authorize_root_commit(self, native):
        _, _, base, calls = native
        with pytest.raises(ValueError):
            await publish(
                GitTarget(git_fixture.URL, "Basic synthetic-credential"),
                git_fixture.candidate(base),
                "",
                "root",
                stamp=git_fixture.STAMP,
            )
        assert not calls

    async def test_marker_format_is_the_frozen_contract(self, journal):
        result = await journal.publish()
        row = await journal.row()
        assert json.loads(row.payload_json)["marker"] == journal.marker
        assert (
            journal.provider.repo(journal.repo)
            .commits[result.sha]
            .message.endswith("\n" + journal.marker)
        )
        with pytest.raises(StoreConflict):
            await journal.publish(marker="another-operation")
        assert journal.writes() == 1


class TestRedispatchGuaranteeTable:
    async def test_only_native_cas_adapters_are_cas_protected(self, connected):
        writer, _, _, server, _, base, _ = connected
        result = await writer.commit(reads.REPO, git_fixture.candidate(base), base, "first")
        assert isinstance(result, Committed)
        second = await writer.commit(reads.REPO, git_fixture.candidate(base), base, "different")
        assert isinstance(second, Conflict)
        assert (
            git_fixture.git(server, "rev-parse", "refs/heads/" + git_fixture.BRANCH).decode()
            == result.sha
        )
        with pytest.raises(ValueError):
            RepositoryIdentity(
                provider="retired-provider", connection_id="test", repository_id="42"
            )

    async def test_gitlab_native_push_has_exact_old_ref_cas(self, native):
        server, _, base, _ = native
        repo = RepoRef(
            identity=RepositoryIdentity(
                provider="gitlab", connection_id="test", repository_id="42"
            ),
            full_name="owner/repository",
        )

        class API:
            transport = SimpleNamespace(endpoint=SimpleNamespace(url="https://fixture.test"))

            async def metadata(self, supplied):
                assert supplied == repo
                return SimpleNamespace(
                    document={
                        "http_url_to_repo": git_fixture.URL,
                        "path_with_namespace": repo.full_name,
                    }
                )

            async def write_token(self, supplied):
                assert supplied == repo
                return "synthetic-credential"

        class Repository:
            async def head(self, supplied, branch):
                assert supplied == repo
                return git_fixture.git(server, "rev-parse", "refs/heads/" + branch).decode()

        writer = GitLabCommitter(API(), Repository(), reads.Clock())
        first = await writer.commit(repo, git_fixture.candidate(base), base, "first")
        second = await writer.commit(repo, git_fixture.candidate(base), base, "different")
        assert isinstance(first, Committed) and isinstance(second, Conflict)
        assert await Repository().head(repo, git_fixture.BRANCH) == first.sha

    def test_dispatch_call_window_is_bounded(self, journal):
        for limit in (0, 3601):
            with pytest.raises(ValueError):
                Effects(
                    EffectStore(journal.factory),
                    *(journal.provider,) * 6,
                    journal.clock,
                    call_timeout=limit,
                )


class TestA12SettleMachine:
    async def test_first_negative_probe_opens_the_window(self, journal):
        row = await journal.crash("after_t2")
        assert (await journal.engine().recover(row)).state == "unknown"
        assert isinstance(await journal.publish(), Unknown) and journal.writes() == 0

    async def test_open_window_waits_without_touching_the_frozen_intent(self, journal):
        row = await journal.crash("after_t2")
        await journal.engine().recover(row)
        again = await journal.engine().recover(await journal.row())
        assert (again.key, again.payload_digest, again.payload_json, again.step_id) == (
            row.key,
            row.payload_digest,
            row.payload_json,
            row.step_id,
        )
        assert journal.writes() == 0

    async def test_gitlab_window_exhaustion_parks_unknown(self, journal):
        journal.provider.fail_next("commit", Fault(http_5xx=True))
        assert isinstance(await journal.publish(), Unknown)
        await journal.expire()
        for _ in range(4):
            journal.provider.fail_next("probe_commit", Fault(http_5xx=True))
            assert (await journal.engine().recover(await journal.row())).state == "unknown"
        assert journal.writes() == 1

    async def test_native_absence_after_call_window_releases_only_original_owner(self, journal):
        await journal.crash("after_t2")
        await journal.expire()
        row = await journal.engine().recover(await journal.row())
        assert row.state == "requested"
        with pytest.raises(StaleLease):
            await journal.publish()
        await journal.reclaim()
        assert isinstance(await journal.publish(), Committed) and journal.writes() == 1


class TestGitLabWriterIntents:
    async def test_intent_and_action_written_before_http(self, journal):
        class Observing(Proxy):
            async def commit(self, *args):
                row = await journal.row()
                assert row.state == "dispatched" and row.payload_digest
                await RunStore(journal.factory).admit(
                    RunRequest("e" * 32, journal.repo.identity.provider, "test", "42", "issue", "2")
                )
                return await self.native.commit(*args)

        assert isinstance(
            await journal.publish(engine=journal.engine(provider=Observing(journal.provider))),
            Committed,
        )

    async def test_intent_key_is_stable_across_retries(self, journal):
        await journal.publish()
        original = await journal.row()
        assert await journal.publish() == await journal.publish()
        assert (await journal.row()).payload_json == original.payload_json and journal.writes() == 1

    async def test_lost_response_is_adopted_by_a_new_process(self, journal):
        journal.provider.fail_next("commit", Fault(unknown_after_success=True))
        assert isinstance(await journal.publish(), Unknown)
        assert (await journal.engine().recover(await journal.row())).state == "adopted"
        assert isinstance(await journal.publish(engine=journal.engine()), Committed)
        assert journal.writes() == 1

    async def test_response_lost_and_nothing_landed_settles_then_parks_unknown(self, journal):
        journal.provider.fail_next("commit", Fault(http_5xx=True))
        assert isinstance(await journal.publish(), Unknown)
        assert (await journal.engine().recover(await journal.row())).state == "unknown"
        assert isinstance(await journal.publish(), Unknown) and journal.writes() == 1

    async def test_previous_repair_commit_not_mistaken_for_new_attempt(self, journal):
        first = await journal.publish()
        second = await journal.publish(key=journal.key + ":new", marker="new-attempt")
        assert isinstance(first, Committed) and isinstance(second, Conflict)
        assert (
            journal.provider.repo(journal.repo).branches[journal.candidate.changeset.branch]
            == first.sha
        )

    async def test_unresolvable_two_matches_block_unknown(self, journal):
        class Ambiguous(Proxy):
            async def probe_commit(self, *args):
                return Inconclusive(
                    reason="two native marker matches", evidence='{"hits":["one","two"]}'
                )

        journal.provider.fail_next("commit", Fault(http_5xx=True))
        await journal.publish()
        row = await journal.engine(provider=Ambiguous(journal.provider)).recover(
            await journal.row()
        )
        assert row.state == "unknown" and journal.writes() == 1

    async def test_head_moved_by_human_resolves_duplicated_and_drifts(self, journal):
        journal.provider.fail_next("commit", Fault(conflict=True))
        result = await journal.publish()
        assert isinstance(result, Conflict) and (await journal.row()).state == "conflict"
        assert await journal.publish() == result and journal.writes() == 1


class TestA12DelayedApplyGitLab:
    async def test_negative_probe_never_redispatches_and_late_commit_is_adopted(self, journal):
        journal.provider.fail_next("commit", Fault(unknown_after_success=True, delay_visibility=2))
        assert isinstance(await journal.publish(), Unknown)
        for _ in range(2):
            assert (await journal.engine().recover(await journal.row())).state == "unknown"
            assert isinstance(await journal.publish(), Unknown)
        assert (await journal.engine().recover(await journal.row())).state == "adopted"
        assert journal.writes() == 1


class TestPublisherIntentMetadata:
    async def test_publish_candidate_journals_intent_before_effect(self, journal):
        await journal.crash("after_t1")
        row = await journal.row()
        payload = json.loads(row.payload_json)
        assert row.state == "requested" and row.step_id == journal.lease.step_id
        assert payload["repo"]["identity"] == journal.repo.identity.model_dump()
        assert payload["expected_head"] == journal.base and payload["marker"] == journal.marker
        assert payload["candidate"]["changeset"]["branch"] == journal.candidate.changeset.branch
        assert journal.writes() == 0


class TestGitHubFlowAdoption:
    async def test_lost_response_adopted_on_stale_cas(self, journal):
        journal.provider.fail_next("commit", Fault(unknown_after_success=True))
        await journal.publish()
        assert (await journal.engine().recover(await journal.row())).state == "adopted"
        assert isinstance(await journal.publish(), Committed) and journal.writes() == 1

    async def test_different_key_is_not_adopted_and_stays_drift(self, journal):
        await journal.publish()
        result = await journal.publish(key="different-operation", marker="different-marker")
        assert isinstance(result, Conflict)

    async def test_ambiguous_marker_matches_stay_drift(self, connected, monkeypatch):
        assert isinstance(await ambiguous_native_probe(connected, monkeypatch), Inconclusive)


class TestA12DelayedApplyGitHubCAS:
    async def test_slow_first_write_cannot_double_the_redispatch(self, native, monkeypatch):
        await native_race(native, monkeypatch)

    async def test_late_landing_first_write_is_adopted_not_duplicated(self, native, monkeypatch):
        await native_race(native, monkeypatch, first_lands_first=True)


class TestGitHubIntentScanner:
    async def test_scanner_adopts_landed_commit_without_granting_run_authority(self, journal):
        journal.provider.fail_next("commit", Fault(unknown_after_success=True))
        await journal.publish()
        result = await journal.reconciler().pass_once()
        assert result.recovered == 1 and (await journal.row()).state == "adopted"
        assert (await RunStore(journal.factory).load(journal.run.id)).state == journal.run.state
        assert journal.writes() == 1

    async def test_scanner_preserves_superseded_run_and_records_source_fact(self, journal):
        journal.provider.fail_next("commit", Fault(unknown_after_success=True))
        await journal.publish()
        await RunStore(journal.factory).request_cancel(
            journal.run.id, expected_version=journal.run.version
        )
        assert (await journal.reconciler().pass_once()).recovered == 1
        assert (await journal.row()).state == "adopted"
        assert (await RunStore(journal.factory).load(journal.run.id)).cancel_requested
        assert journal.writes() == 1

    async def test_scanner_unknown_keeps_publication_blocked(self, delivery, operator):
        env = delivery["env"]
        run, _, pending = await started(delivery)
        await completed(delivery, pending)
        env.fake.fail_next("commit", Fault(http_5xx=True))
        assert (await env.runner.tick()).parked == 1
        result = await operator["commands"].submit(envelope(operator, "reconcile"))
        assert result.reason == "effects_observed"
        current = await env.runs.load(run.id)
        assert current.state.value == "blocked" and current.reason == "effect_unknown:commit"
        assert sum(name == "commit" for name, _ in env.fake.calls) == 1

    async def test_scanner_negative_probe_waits_without_posting(self, journal):
        await journal.crash("after_t2")
        result = await journal.reconciler().pass_once()
        assert result.requeued == 0 and (await journal.row()).state == "unknown"
        assert journal.writes() == 0

    async def test_scanner_authoritative_absence_requeues_same_original_step(self, journal):
        await journal.crash("after_t2")
        await journal.expire()
        result = await journal.reconciler().pass_once()
        assert result.requeued == 1 and (await journal.row()).state == "requested"
        async with journal.factory() as session:
            step = await session.get(EngineStep, journal.lease.step_id)
            assert step.state == "scheduled" and step.lease_epoch == journal.lease.epoch
        assert journal.writes() == 0


class TestRetainedNativeCASAdoption:
    async def test_publication_embeds_the_frozen_operation_marker(self, native):
        server, _, base, _ = native
        marker = "<!-- exact-operation-key -->"
        sha = await publish(
            GitTarget(git_fixture.URL, "Basic synthetic-credential"),
            git_fixture.candidate(base),
            base,
            marker,
            stamp=git_fixture.STAMP,
        )
        assert (
            git_fixture.git(server, "show", "-s", "--format=%B", sha).decode().splitlines()[-1]
            == marker
        )

    async def test_stale_cas_adopts_previous_push(self, connected):
        repo, _, base, candidate, result = await native_commit(connected, "lost-response")
        adopted = await repo.probe_commit(
            reads.REPO, git_fixture.BRANCH, "lost-response", base, candidate
        )
        assert isinstance(adopted, Found) and adopted.value.sha == result.sha


class TestRetainedDelayedApplyNativeCAS:
    async def test_delayed_first_push_has_exactly_one_surviving_effect(self, native, monkeypatch):
        await native_race(native, monkeypatch)


class TestIdempotentCompletion:
    async def test_completing_the_same_effect_again_is_a_no_op(self, journal):
        journal.provider.fail_next("commit", Fault(unknown_after_success=True))
        await journal.publish()
        adopted = await journal.engine().recover(await journal.row())
        saved = await EffectStore(journal.factory).record_observation(
            adopted.id, state="committed", outcome_json=adopted.outcome_json
        )
        assert saved.state == "adopted" and saved.outcome_json == adopted.outcome_json
        assert journal.writes() == 1

    async def test_completing_a_different_effect_still_raises(self, journal):
        await journal.publish()
        with pytest.raises(StoreConflict):
            await journal.publish(marker="another-effect")
        assert journal.writes() == 1

    async def test_late_conflicting_observation_is_retained_without_replacing_authority(
        self, journal
    ):
        await journal.publish()
        original = await journal.row()
        competing = canonical_json(Committed(sha="f" * 40, evidence="late_source_fact")).decode()
        observed = await EffectStore(journal.factory).record_observation(
            original.id, state="committed", outcome_json=competing
        )
        assert observed.outcome_json == original.outcome_json
        assert any(
            json.loads(value)["outcome"]["sha"] == "f" * 40 for value in observed.observations
        )
        assert journal.writes() == 1
