"""R38-04 (#305): credential CONSUMPTION and rotation at the runner
boundary — the consumer receipt join, the JSON registry's multi-worker
contract, presence-version honesty, the strict/compat policy matrix and
SecretValue hygiene.

The recorded gap this module closes: #207/#303 proved the broker is
called and selected values reach a fake provider ledger; nothing proved
WHICH binding revision paid for WHICH attempt as the runner consumed it,
how an old worker observes a rotation, or that a presence version never
poses as a unique-secret-version proof. The runner-boundary subprocess
proofs (the REAL lane bootstrap) live in
``tests/production_entry/test_credential_dispatch.py`` (CD-7/CD-8);
this module owns the contract units beneath them:

- the consumer receipt document (schema
  ``forge.credential.consumer-receipt/1``): the broker id ↔ redemption
  id ↔ attempt ↔ consumer join, value-free by construction;
- :class:`~forge.adaptive.credential_broker.ConcurrentCredentialRegistry`
  — writes locked+atomic, reads observing another REAL worker process's
  revocation within the documented stat TTL
  (``FORGE_CREDENTIAL_REGISTRY_TTL_SECONDS``), the in-flight staged
  snapshot surviving a revocation while the NEXT resolve refuses;
- ``FORGE_CREDENTIAL_POLICY`` ∈ {compat, strict-broker} — the matrix;
- the resolved version's KIND (presence is never a secret version);
- :class:`~forge.adaptive.credential_broker.SecretValue` against an
  adversarial value (newline/quote/marker text): repr, str, exception
  serialization, receipts and logs carry none of it.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import pytest

from forge.adaptive.credential_broker import (
    CONSUMER_RECEIPT_SCHEMA,
    CREDENTIAL_POLICY_COMPAT,
    CREDENTIAL_POLICY_ENV,
    CREDENTIAL_POLICY_STRICT_BROKER,
    DEFAULT_REGISTRY_TTL_SECONDS,
    DELIVERY_MODE_GITHUB_NATIVE,
    DELIVERY_PLAN_SCHEMA,
    DELIVERY_ROUTE_ENV,
    DELIVERY_TEMPLATE_DIR_ENV,
    REGISTRY_TTL_ENV,
    VERSION_KIND_BINDING_REVISION,
    VERSION_KIND_FIXTURE,
    VERSION_KIND_PRESENCE,
    VERSION_KIND_SECRET_VERSION,
    BrokerCredentialRefusal,
    ConcurrentCredentialRegistry,
    EnvBroker,
    SecretValue,
    StagedBroker,
    credential_policy,
    delivery_plan,
    registry_ttl_seconds,
    reveal_secret,
    stage_dispatch_credential,
    version_is_unique_secret_proof,
)
from forge.adaptive.operator_snapshot import CanonicalSubject
from forge.adaptive.project_credentials import CredentialRefusal, ProjectCredentialRegistry
from forge.lane_driver import (
    CONSUMPTION_STATUS_CONSUMED,
    CONSUMPTION_STATUS_UNRESOLVED,
    credential_consumption_record,
    consumption_status_for,
)

SUBJECT = CanonicalSubject(provider_family="gitlab", connection="gitlab.example", native_id="90210")
OTHER_SUBJECT = CanonicalSubject(
    provider_family="gitlab", connection="gitlab.other.example", native_id="90210"
)

ENV_REF = "env:ANTHROPIC_AUTH_TOKEN"
VAULT_REF = "vault:kv/eng#42"

#: The credential VALUE — the string that must never appear outside the
#: env slot it was applied to. The adversarial spelling (hygiene class)
#: adds a newline, a quote and marker-like text.
CANARY_VALUE = "sk-canary-0123456789abcdef"
ADVERSARIAL_VALUE = 'sk-adv-"quoted"\nBearer ADV_MARKER=1\r\nSECRET'

TEMPLATES_DIR = Path(__file__).parents[1] / "ci" / "templates"


def _bound_registry(**kwargs) -> ProjectCredentialRegistry:
    registry = kwargs.pop("registry_class", ProjectCredentialRegistry)()
    registry.bind(SUBJECT, "anthropic-gateway", ENV_REF, bound_by="ops@a")
    return registry


def _delivery_env(*routes: str, policy: str = "") -> dict[str, str]:
    env = {
        DELIVERY_ROUTE_ENV: ",".join(routes),
        DELIVERY_TEMPLATE_DIR_ENV: str(TEMPLATES_DIR),
    }
    if policy:
        env[CREDENTIAL_POLICY_ENV] = policy
    return env


# ----------------------------------------------------------------------
# The consumer receipt join (the runner-boundary contract's unit shape)
# ----------------------------------------------------------------------


class TestConsumerReceiptJoin:
    @staticmethod
    def _redemption_record() -> dict[str, object]:
        """A redemption record as ``apply_redeemed_credential`` returns
        one against a #305-shaped redemption response."""
        return {
            "env_var": "ANTHROPIC_AUTH_TOKEN",
            "credential_ref": ENV_REF,
            "provider": "anthropic-gateway",
            "redemption_id": "redemption-1234",
            "expires_at": "2026-09-24T00:00:00+00:00",
            "binding_revision": 2,
            "resolver_identity": "staged",
            "scrubbed_env_vars": ["ANTHROPIC_API_KEY"],
            "broker_receipt_id": "broker-5678",
            "resolved_version": "v2",
            "resolved_version_kind": VERSION_KIND_FIXTURE,
            "attempt_generation": 3,
            "credential_policy": CREDENTIAL_POLICY_COMPAT,
        }

    def test_the_receipt_joins_every_identity_axis_without_a_value_slot(self):
        record = credential_consumption_record(
            redemption_record=self._redemption_record(),
            env={
                "FORGE_WORK_ID": "work-90",
                "CI_JOB_ID": "424242",
                "FORGE_LANE_DRIVER": "claude",
                "FORGE_CREDENTIAL_REDEEM": "1",
            },
        )
        assert record is not None
        assert record["schema"] == CONSUMER_RECEIPT_SCHEMA
        # The join the operator needs: broker id ↔ redemption id ↔
        # attempt ↔ consumer, over the binding revision and the route.
        assert record["redemption_id"] == "redemption-1234"
        assert record["broker_receipt_id"] == "broker-5678"
        assert record["attempt_generation"] == 3
        assert record["work_id"] == "work-90"
        assert record["consumer_identity"] == {"kind": "ci-job", "id": "424242", "via": "CI_JOB_ID"}
        assert record["binding_revision"] == 2
        assert record["binding_revision_known"] is True
        assert record["env_var"] == "ANTHROPIC_AUTH_TOKEN"  # the slot NAME, never the value
        assert record["provider_route"] == "anthropic-gateway"
        assert record["delivery_route"] == "/lane/credentials/redeem"
        assert record["resolver_identity"] == "staged"
        assert record["credential_policy"] == CREDENTIAL_POLICY_COMPAT
        assert record["resolved_version_kind"] == VERSION_KIND_FIXTURE
        assert record["consumer_receipt_id"]
        assert record["consumer_status"] == CONSUMPTION_STATUS_UNRESOLVED
        # Value-free by construction: there is no value slot at all.
        assert "value" not in record
        assert CANARY_VALUE not in json.dumps(record)
        assert ADVERSARIAL_VALUE not in json.dumps(record)

    def test_the_receipt_carries_the_operation_grant_foreign_key(self):
        """Q39-01 (#320): the grant_id is the FK the consumer receipt
        joins on — echoed from the redemption record when present (and
        honestly empty on pre-grant records, never guessed)."""
        record = credential_consumption_record(
            redemption_record={**self._redemption_record(), "grant_id": "grant-abc"},
            env={"FORGE_WORK_ID": "work-90"},
        )
        assert record is not None
        assert record["grant_id"] == "grant-abc"
        legacy = credential_consumption_record(
            redemption_record=self._redemption_record(), env={"FORGE_WORK_ID": "work-90"}
        )
        assert legacy is not None
        assert legacy["grant_id"] == ""

    def test_the_native_receipt_records_an_unknown_binding_revision_honestly(self):
        record = credential_consumption_record(
            env={
                "FORGE_WORK_ID": "work-91",
                "FORGE_CREDENTIAL_REF": "ENV_ANTHROPIC_AUTH_TOKEN",
                "FORGE_CREDENTIAL_REDEEM": "",
                "FORGE_LANE_DRIVER": "claude",
                "CI_JOB_ID": "777",
                "GITLAB_CI": "true",
            }
        )
        assert record is not None
        assert record["schema"] == CONSUMER_RECEIPT_SCHEMA
        # The native bootstrap CANNOT see the control plane's binding
        # revision — unknown is recorded as unknown, never guessed.
        assert record["binding_revision"] is None
        assert record["binding_revision_known"] is False
        assert record["delivery_route"] == "gitlab-protected-variable"
        assert record["env_var"] == "ANTHROPIC_AUTH_TOKEN"
        assert record["credential_policy"] == CREDENTIAL_POLICY_COMPAT

    def test_a_lane_with_no_delivery_gets_no_receipt(self):
        assert (
            credential_consumption_record(env={"FORGE_WORK_ID": "w", "FORGE_CREDENTIAL_REDEEM": ""})
            is None
        )

    def test_the_status_is_earned_only_by_a_completed_turn(self):
        assert consumption_status_for("completed") == CONSUMPTION_STATUS_CONSUMED
        for other in ("failed", "driver_error", "budget_exceeded", "anything-else"):
            assert consumption_status_for(other) == CONSUMPTION_STATUS_UNRESOLVED

    def test_a_pre305_redemption_record_still_joins_with_empty_ids(self):
        legacy = {
            key: value
            for key, value in self._redemption_record().items()
            if key
            not in (
                "broker_receipt_id",
                "resolved_version",
                "resolved_version_kind",
                "attempt_generation",
                "credential_policy",
            )
        }
        record = credential_consumption_record(redemption_record=legacy, env={"FORGE_WORK_ID": "w"})
        assert record is not None
        assert record["redemption_id"] == "redemption-1234"
        assert record["broker_receipt_id"] == ""
        assert record["attempt_generation"] is None
        assert record["credential_policy"] == CREDENTIAL_POLICY_COMPAT  # stamped lane-side


# ----------------------------------------------------------------------
# The JSON registry's multi-worker contract — real processes
# ----------------------------------------------------------------------

#: The worker program: a REAL second process holding a registry over
#: the same JSON document, resolving / mutating on command.
_WORKER_SCRIPT = r"""
import json, sys, time
from pathlib import Path
from forge.adaptive.credential_broker import ConcurrentCredentialRegistry
from forge.adaptive.operator_snapshot import CanonicalSubject
from forge.adaptive.project_credentials import CredentialRefusal, resolve_dispatch_credential

PATH, MODE = Path(sys.argv[1]), sys.argv[2]
SUBJECT = CanonicalSubject(provider_family="gitlab", connection="gitlab.example", native_id="90210")
registry = ConcurrentCredentialRegistry(path=PATH)

def outcome():
    try:
        credential = resolve_dispatch_credential(
            registry, subject=SUBJECT, provider="anthropic-gateway"
        )
        return {"resolved": True, "ref": credential.credential_ref, "revision": credential.binding_revision}
    except CredentialRefusal as exc:
        return {"resolved": False, "reason": exc.reason}

if MODE == "bind":
    registry.bind(SUBJECT, "anthropic-gateway", sys.argv[3], bound_by="ops@two-proc")
    print(json.dumps(outcome()))
elif MODE == "revoke":
    result = registry.revoke(SUBJECT, "anthropic-gateway", revoked_by="ops@two-proc")
    print(json.dumps({"revoked": result is not None}))
elif MODE == "rotate":
    for index in range(int(sys.argv[3])):
        registry.bind(SUBJECT, "anthropic-gateway", f"vault:kv/eng#{index + 1}", bound_by="ops@two-proc")
    print(json.dumps(outcome()))
elif MODE == "watch":
    first = outcome()
    Path(sys.argv[3]).write_text(json.dumps(first))
    marker = Path(sys.argv[4])
    deadline = time.monotonic() + 30.0
    while not marker.is_file():
        if time.monotonic() > deadline:
            sys.exit(2)
        time.sleep(0.02)
    within_ttl = outcome()          # still inside the stat-TTL window
    time.sleep(float(sys.argv[5]))  # past the TTL
    after_ttl = outcome()
    print(json.dumps({"first": first, "within_ttl": within_ttl, "after_ttl": after_ttl}))
"""


def _worker(path: Path, mode: str, *args: str, ttl: str = "1.0") -> dict[str, object]:
    """Run one REAL worker process over the shared registry document."""
    import os

    env = {**os.environ, REGISTRY_TTL_ENV: ttl, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [sys.executable, "-c", _WORKER_SCRIPT, str(path), mode, *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, f"worker {mode} failed: {result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestRegistryMultiWorkerContract:
    def test_the_ttl_is_operator_state_not_silently_redefaulted(self):
        assert registry_ttl_seconds({}) == DEFAULT_REGISTRY_TTL_SECONDS == 5.0
        assert registry_ttl_seconds({REGISTRY_TTL_ENV: "0.5"}) == 0.5
        for bad in ("nope", "-3", "0"):
            with pytest.raises(CredentialRefusal, match="credential_registry_ttl_invalid"):
                registry_ttl_seconds({REGISTRY_TTL_ENV: bad})

    def test_writes_land_atomically_no_torn_document_no_tmp_leftovers(self, tmp_path):
        path = tmp_path / "bindings.json"
        registry = ConcurrentCredentialRegistry(path=path, ttl_seconds=5.0)
        registry.bind(SUBJECT, "anthropic-gateway", ENV_REF, bound_by="ops@a")
        # The document is whole JSON, first write.
        document = json.loads(path.read_text())
        assert document["schema"] == "forge.project.credential-binding/2"
        registry.revoke(SUBJECT, "anthropic-gateway", revoked_by="ops@a")
        json.loads(path.read_text())  # still whole
        # The atomic-rename landing leaves no temp files behind.
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "bindings.json",
            "bindings.json.lock",
        ]

    def test_two_worker_processes_observe_a_revocation_per_the_ttl_contract(self, tmp_path):
        """THE propagation contract: worker A mutates; worker B — a REAL
        long-lived second process — keeps its cached view WITHIN the TTL
        window (the documented staleness bound) and observes the
        revocation on the next resolve AFTER it."""
        path = tmp_path / "bindings.json"
        assert _worker(path, "bind", ENV_REF) == {
            "resolved": True,
            "ref": ENV_REF,
            "revision": 1,
        }
        phase = tmp_path / "worker-first-view.json"
        marker = tmp_path / "revoked.marker"
        ttl = 1.0
        watcher = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _WORKER_SCRIPT,
                str(path),
                "watch",
                str(phase),
                str(marker),
                str(ttl + 0.4),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**__import__("os").environ, REGISTRY_TTL_ENV: str(ttl)},
        )
        try:
            deadline = time.monotonic() + 30.0
            while not phase.is_file():
                assert time.monotonic() < deadline, "worker never reported its first view"
                time.sleep(0.02)
            # Worker B holds the live generation; worker A revokes.
            assert _worker(path, "revoke") == {"revoked": True}
            marker.touch()
            stdout, stderr = watcher.communicate(timeout=120)
            assert watcher.returncode == 0, stderr
            observed = json.loads(stdout.strip().splitlines()[-1])
        finally:
            if watcher.poll() is None:  # pragma: no cover — failure cleanup
                watcher.kill()
        assert observed["first"] == {"resolved": True, "ref": ENV_REF, "revision": 1}
        # WITHIN the TTL: the documented cached window — still resolvable.
        assert observed["within_ttl"]["resolved"] is True
        # AFTER the TTL: the next resolve observes the revocation.
        assert observed["after_ttl"] == {"resolved": False, "reason": "revoked"}

    def test_concurrent_writers_serialize_no_lost_rotation(self, tmp_path):
        """Two REAL writer processes rotating the SAME key concurrently:
        the lock serializes every read-modify-write, so every rotation
        lands — the final revision counts them all, atomically."""
        path = tmp_path / "bindings.json"
        _worker(path, "bind", ENV_REF)
        workers = [
            subprocess.Popen(
                [sys.executable, "-c", _WORKER_SCRIPT, str(path), "rotate", "5"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**__import__("os").environ, REGISTRY_TTL_ENV: "0.2"},
            )
            for _ in range(2)
        ]
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=120)
            assert worker.returncode == 0, stderr
            assert json.loads(stdout.strip())["resolved"] is True
        final = ConcurrentCredentialRegistry(path=path, ttl_seconds=0.05)
        binding = final.binding_for(SUBJECT, "anthropic-gateway")
        assert binding is not None
        assert binding.revision == 11  # 1 initial + 10 serialized rotations
        assert len(final.history_for(SUBJECT, "anthropic-gateway")) == 10
        json.loads(path.read_text())  # and the document is whole

    async def test_a_revoked_bindings_in_flight_snapshot_stays_valid_until_terminal(self):
        """Rotation semantics at the seam: the staged SNAPSHOT a dispatch
        already took survives a revocation that lands mid-flight (the
        launched lane keeps its generation); only the NEXT resolve —
        a NEW dispatch — refuses typed."""
        registry = _bound_registry()
        broker = StagedBroker()
        broker.stage(ENV_REF, "generation-one", env_var="ANTHROPIC_AUTH_TOKEN", version="v1")
        staged = await stage_dispatch_credential(
            registry, broker, subject=SUBJECT, provider="anthropic-gateway"
        )
        assert staged is not None

        registry.revoke(SUBJECT, "anthropic-gateway", revoked_by="ops@a")

        # The in-flight snapshot is untouched — the launched env unchanged.
        assert staged.staged_env == {"ANTHROPIC_AUTH_TOKEN": "generation-one"}
        assert staged.binding_revision == 1
        # The NEW dispatch refuses — never a silent substitution.
        with pytest.raises(CredentialRefusal, match="revoked"):
            await stage_dispatch_credential(
                registry, broker, subject=SUBJECT, provider="anthropic-gateway"
            )


# ----------------------------------------------------------------------
# Presence-version honesty
# ----------------------------------------------------------------------


class TestPresenceVersionKind:
    async def test_an_env_resolution_declares_its_version_a_presence_stamp(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", CANARY_VALUE)
        resolved = await EnvBroker().resolve(ENV_REF)
        assert resolved.version_kind == VERSION_KIND_PRESENCE
        assert resolved.receipt["resolved_version_kind"] == VERSION_KIND_PRESENCE
        # A presence stamp is NEVER a unique-secret-version proof — no
        # matter how unique the string looks.
        assert version_is_unique_secret_proof(resolved.version_kind) is False
        assert version_is_unique_secret_proof(VERSION_KIND_SECRET_VERSION) is True

    async def test_a_staged_resolution_declares_a_fixture_label(self):
        broker = StagedBroker()
        broker.stage(VAULT_REF, CANARY_VALUE, env_var="ANTHROPIC_AUTH_TOKEN", version="v7")
        resolved = await broker.resolve(VAULT_REF)
        assert resolved.version_kind == VERSION_KIND_FIXTURE
        assert resolved.receipt["resolved_version_kind"] == VERSION_KIND_FIXTURE
        assert version_is_unique_secret_proof(resolved.version_kind) is False

    async def test_the_dispatch_proof_carries_the_version_kind_beside_the_version(
        self, monkeypatch
    ):
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", CANARY_VALUE)
        staged = await stage_dispatch_credential(
            _bound_registry(), EnvBroker(), subject=SUBJECT, provider="anthropic-gateway"
        )
        assert staged is not None
        assert staged.resolved_version_kind == VERSION_KIND_PRESENCE
        assert staged.proof["resolved_version"] == staged.resolved_version
        assert staged.proof["resolved_version_kind"] == VERSION_KIND_PRESENCE

    async def test_the_delivery_plan_names_the_binding_revision_as_the_attribution_axis(self):
        plan = await delivery_plan(
            _bound_registry(),
            StagedBroker(),
            subject=SUBJECT,
            provider_route="anthropic-gateway",
            profile="github",
            environ=_delivery_env(DELIVERY_MODE_GITHUB_NATIVE),
        )
        assert plan is not None
        # No CI secret facility exposes a secret-version id: the plan's
        # identity axis is the binding revision, stated as such.
        assert plan.expected_identity["version_kind"] == VERSION_KIND_BINDING_REVISION
        assert version_is_unique_secret_proof(plan.expected_identity["version_kind"]) is False
        document = plan.as_document()
        assert document["schema"] == DELIVERY_PLAN_SCHEMA
        assert document["credential_policy"] == CREDENTIAL_POLICY_COMPAT


# ----------------------------------------------------------------------
# The strict vs compatibility policy matrix
# ----------------------------------------------------------------------


class TestCredentialPolicyMatrix:
    def test_the_policy_is_operator_state(self):
        assert credential_policy({}) == CREDENTIAL_POLICY_COMPAT
        assert credential_policy({CREDENTIAL_POLICY_ENV: "strict-broker"}) == (
            CREDENTIAL_POLICY_STRICT_BROKER
        )
        with pytest.raises(CredentialRefusal, match="credential_policy_invalid"):
            credential_policy({CREDENTIAL_POLICY_ENV: "strict"})

    async def test_compat_keeps_the_unbound_opt_out_labeled_ambient_legacy(self):
        staged = await stage_dispatch_credential(
            _bound_registry(),
            StagedBroker(),
            subject=OTHER_SUBJECT,
            provider="anthropic-gateway",
            environ={CREDENTIAL_POLICY_ENV: CREDENTIAL_POLICY_COMPAT},
        )
        assert staged is None  # today's behavior; attribution stays unknown
        plan = await delivery_plan(
            _bound_registry(),
            StagedBroker(),
            subject=OTHER_SUBJECT,
            provider_route="anthropic-gateway",
            profile="github",
            environ=_delivery_env(DELIVERY_MODE_GITHUB_NATIVE, policy=CREDENTIAL_POLICY_COMPAT),
        )
        assert plan is None

    async def test_strict_refuses_an_unbound_route_on_a_registry_with_any_binding(self):
        """The matrix's strict row: the registry holds ANY binding (the
        deployment opted into bound credentials) — an unbound subject, an
        unknown route and an unknown profile each refuse TYPED."""
        for stage_kwargs in (
            {"subject": OTHER_SUBJECT, "provider": "anthropic-gateway"},  # absent binding
            {"subject": SUBJECT, "provider": ""},  # unknown route
        ):
            with pytest.raises(CredentialRefusal, match="strict_unbound_route") as caught:
                await stage_dispatch_credential(
                    _bound_registry(),
                    StagedBroker(),
                    environ={CREDENTIAL_POLICY_ENV: CREDENTIAL_POLICY_STRICT_BROKER},
                    **stage_kwargs,
                )
            assert caught.value.detail["policy"] == CREDENTIAL_POLICY_STRICT_BROKER
            assert caught.value.detail["registry_bindings"] >= 1
        with pytest.raises(CredentialRefusal, match="strict_unbound_route"):
            await delivery_plan(
                _bound_registry(),
                StagedBroker(),
                subject=OTHER_SUBJECT,
                provider_route="anthropic-gateway",
                profile="github",
                environ=_delivery_env(
                    DELIVERY_MODE_GITHUB_NATIVE, policy=CREDENTIAL_POLICY_STRICT_BROKER
                ),
            )

    async def test_strict_on_an_empty_registry_keeps_the_opt_out(self):
        """Strict guards BOUND deployments, not empty ones: a registry
        with no binding decision at all stays ambient (nothing to
        protect, no project-bound claim either)."""
        empty = ProjectCredentialRegistry()
        assert (
            await stage_dispatch_credential(
                empty,
                StagedBroker(),
                subject=SUBJECT,
                provider="anthropic-gateway",
                environ={CREDENTIAL_POLICY_ENV: CREDENTIAL_POLICY_STRICT_BROKER},
            )
            is None
        )

    async def test_a_bound_subject_stages_normally_under_strict(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", CANARY_VALUE)
        monkeypatch.setenv(CREDENTIAL_POLICY_ENV, CREDENTIAL_POLICY_STRICT_BROKER)
        staged = await stage_dispatch_credential(
            _bound_registry(), EnvBroker(), subject=SUBJECT, provider="anthropic-gateway"
        )
        assert staged is not None
        # The policy is stamped in the proof and the broker receipt.
        assert staged.credential_policy == CREDENTIAL_POLICY_STRICT_BROKER
        assert staged.proof["credential_policy"] == CREDENTIAL_POLICY_STRICT_BROKER
        assert staged.proof["receipt"]["credential_policy"] == CREDENTIAL_POLICY_STRICT_BROKER

    async def test_a_malformed_policy_never_silently_re_defaults(self):
        with pytest.raises(CredentialRefusal, match="credential_policy_invalid"):
            await stage_dispatch_credential(
                _bound_registry(),
                StagedBroker(),
                subject=SUBJECT,
                provider="anthropic-gateway",
                environ={CREDENTIAL_POLICY_ENV: "loose"},
            )

    async def test_every_receipt_stamps_the_policy_in_force(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", CANARY_VALUE)
        resolved = await EnvBroker().resolve(ENV_REF)
        assert resolved.receipt["credential_policy"] == CREDENTIAL_POLICY_COMPAT
        assert resolved.receipt["receipt_id"]  # the broker receipt's own id
        plan = await delivery_plan(
            _bound_registry(),
            EnvBroker(),
            subject=SUBJECT,
            provider_route="anthropic-gateway",
            profile="github",
            environ=_delivery_env(
                DELIVERY_MODE_GITHUB_NATIVE, policy=CREDENTIAL_POLICY_STRICT_BROKER
            ),
        )
        assert plan is not None
        assert plan.credential_policy == CREDENTIAL_POLICY_STRICT_BROKER
        assert plan.as_document()["credential_policy"] == CREDENTIAL_POLICY_STRICT_BROKER


# ----------------------------------------------------------------------
# SecretValue hygiene — the adversarial value
# ----------------------------------------------------------------------


class TestSecretValueHygiene:
    async def test_repr_and_str_carry_the_length_class_only(self):
        wrapped = SecretValue(ADVERSARIAL_VALUE)
        for rendered in (repr(wrapped), str(wrapped)):
            assert ADVERSARIAL_VALUE not in rendered
            assert "sk-adv" not in rendered
            assert rendered.startswith("<secret len=")
        assert wrapped.length_class() == "short"  # 42 chars — bucketed, not exact
        # The deliberate unwrap is the only way to the material.
        assert reveal_secret(wrapped) == ADVERSARIAL_VALUE

    async def test_the_resolved_credential_repr_cannot_leak_the_value(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", ADVERSARIAL_VALUE)
        resolved = await EnvBroker().resolve(ENV_REF)
        rendered = repr(resolved) + str(resolved) + repr(resolved.staged_env)
        assert ADVERSARIAL_VALUE not in rendered
        assert "<secret len=" in rendered

    async def test_an_exception_carrying_the_wrapper_serializes_safely(self):
        refusal = BrokerCredentialRefusal("env_absent", {"value": SecretValue(ADVERSARIAL_VALUE)})
        assert ADVERSARIAL_VALUE not in str(refusal)
        assert ADVERSARIAL_VALUE not in repr(refusal)

    async def test_receipts_proofs_and_plans_stay_value_free(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", ADVERSARIAL_VALUE)
        staged = await stage_dispatch_credential(
            _bound_registry(), EnvBroker(), subject=SUBJECT, provider="anthropic-gateway"
        )
        assert staged is not None
        plan = await delivery_plan(
            _bound_registry(),
            EnvBroker(),
            subject=SUBJECT,
            provider_route="anthropic-gateway",
            profile="github",
            environ=_delivery_env(DELIVERY_MODE_GITHUB_NATIVE),
        )
        assert plan is not None
        record = credential_consumption_record(
            redemption_record={
                "env_var": "ANTHROPIC_AUTH_TOKEN",
                "credential_ref": ENV_REF,
                "provider": "anthropic-gateway",
                "redemption_id": "r1",
                "resolver_identity": "env",
                "broker_receipt_id": staged.proof["receipt"]["receipt_id"],
                "binding_revision": 1,
                "credential_policy": CREDENTIAL_POLICY_COMPAT,
            },
            env={"FORGE_WORK_ID": "w"},
        )
        for document in (
            json.dumps(staged.proof),
            json.dumps(plan.as_document()),
            json.dumps(record),
            json.dumps(staged.proof["receipt"]),
        ):
            assert ADVERSARIAL_VALUE not in document
            assert "sk-adv" not in document

    async def test_logged_surfaces_carry_none_of_the_value(self, monkeypatch, caplog):
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", ADVERSARIAL_VALUE)
        resolved = await EnvBroker().resolve(ENV_REF)
        refusal = BrokerCredentialRefusal(
            "env_absent", {"value": list(resolved.staged_env.values())[0]}
        )
        with caplog.at_level(logging.DEBUG, logger="forge.credential_hygiene_probe"):
            logging.getLogger("forge.credential_hygiene_probe").debug(
                "resolved=%r refused=%s", resolved, refusal
            )
        assert ADVERSARIAL_VALUE not in caplog.text
        assert "<secret len=" in caplog.text

    async def test_equality_still_compares_the_material_without_exposing_it(self):
        assert SecretValue(CANARY_VALUE) == CANARY_VALUE
        assert SecretValue(CANARY_VALUE) == SecretValue(CANARY_VALUE)
        assert SecretValue(CANARY_VALUE) != CANARY_VALUE + "x"
