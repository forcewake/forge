"""ADR-0029 / R32-24 / R36-06: the composition boundary types and the recorded-evidence matrix.

Pinned here, per the issue's gates:

- construction validation for all three types — every refusal case
  (empty/mismatched provider family, cross-family identity mixing,
  missing envelope fields as TypeError, unknown resume mode);
- the R36-06 envelope v2 identity split: the execution identity is the
  derived durable id (never the source OID, never the run id, never a
  counter), the authority epoch and the pinned continuation ref are
  INSIDE the digest, and the v1 compat shape serializes historically
  without manufacturing an execution id;
- :func:`assert_attempt_start` happy path + each violation, including
  the identity-coherence refusals;
- :func:`assert_publication_identity` — a callback is authorized by its
  EXECUTION identity, never by a source-OID match alone;
- :class:`CompositionMatrix` verified/untested/unsupported edges,
  ``blocked_by`` consistency in both directions, evidence-gated
  promotion, no silent demotion;
- :func:`matrix_drift` findings for shipped-but-unqualified combinations;
- digest determinism (equal values hash equal, whatever the construction
  order; different values hash different).
"""

import pytest

from forge.runs.composition import (
    ATTEMPT_START_V1,
    ATTEMPT_START_V2,
    EDGE_UNTESTED,
    EDGE_UNSUPPORTED,
    EDGE_VERIFIED,
    AttemptStartSpec,
    CompositionBoundaryError,
    CompositionMatrix,
    DriftFinding,
    MatrixEdge,
    MissingEnvelopeFieldError,
    RepositoryContext,
    ResumeSpec,
    assert_attempt_start,
    assert_publication_identity,
    derive_execution_attempt_id,
    matrix_drift,
)

HEX64 = "ab" * 32
OTHER_HEX64 = "cd" * 32
SOURCE_OID = "f" * 40
OTHER_SOURCE_OID = "e" * 40


def a_repository(**overrides) -> RepositoryContext:
    base = {
        "provider_family": "github",
        "connection_id": "github:acme/widgets",
        "repository_id": "acme/widgets",
    }
    base.update(overrides)
    return RepositoryContext(**base)


def the_execution_id(**overrides) -> str:
    """The derived durable execution identity (R36-06's derivation)."""
    kwargs = {"run_id": "run-1", "attempt_ordinal": 0, "source_base_oid": SOURCE_OID}
    kwargs.update(overrides)
    return derive_execution_attempt_id(**kwargs)


def an_attempt_start(repository: RepositoryContext | None = None, **overrides) -> AttemptStartSpec:
    kwargs = {
        "run_id": "run-1",
        "execution_attempt_id": the_execution_id(),
        "source_base_oid": SOURCE_OID,
        "repository": repository or a_repository(),
        "profile_digest": HEX64,
        "resume_mode": "fresh",
        "lease_id": "lease-9",
        "authority_epoch": 0,
        "continuation_ref_digest": "",
        "schema_version": ATTEMPT_START_V2,
    }
    kwargs.update(overrides)
    return AttemptStartSpec(**kwargs)


# -- RepositoryContext -------------------------------------------------------


class TestRepositoryContext:
    @pytest.mark.parametrize("family", ["gitlab", "github", "azure_devops"])
    def test_every_provider_family_constructs(self, family):
        context = RepositoryContext(
            provider_family=family, connection_id="conn-1", repository_id="repo-1"
        )
        assert context.provider_family == family

    def test_family_qualified_identities_are_accepted(self):
        context = RepositoryContext(
            provider_family="azure_devops",
            connection_id="azure_devops:https://dev.azure.com/acme:proj",
            repository_id="azure_devops:widgets",
        )
        assert context.subject_key() == (
            "azure_devops:azure_devops:https://dev.azure.com/acme:proj:azure_devops:widgets"
        )

    def test_unknown_prefix_is_a_bare_identity_not_a_family_claim(self):
        context = RepositoryContext(
            provider_family="github", connection_id="docker:pg17", repository_id="r"
        )
        assert context.connection_id == "docker:pg17"

    def test_empty_provider_family_is_refused(self):
        with pytest.raises(MissingEnvelopeFieldError, match="provider_family"):
            RepositoryContext(provider_family="", connection_id="c", repository_id="r")

    @pytest.mark.parametrize("family", ["gitea", "Github", "github ", "1"])
    def test_mismatched_provider_family_is_refused(self, family):
        with pytest.raises(CompositionBoundaryError, match="provider_family"):
            RepositoryContext(provider_family=family, connection_id="c", repository_id="r")

    @pytest.mark.parametrize("field_name", ["connection_id", "repository_id"])
    def test_empty_identities_are_refused(self, field_name):
        kwargs = {"provider_family": "github", "connection_id": "c", "repository_id": "r"}
        kwargs[field_name] = ""
        with pytest.raises(MissingEnvelopeFieldError, match=field_name):
            RepositoryContext(**kwargs)

    @pytest.mark.parametrize("field_name", ["connection_id", "repository_id"])
    def test_cross_family_identity_mixing_is_refused(self, field_name):
        kwargs = {
            "provider_family": "github",
            "connection_id": "github:acme/widgets",
            "repository_id": "acme/widgets",
        }
        kwargs[field_name] = "gitlab:conn-1"
        with pytest.raises(CompositionBoundaryError, match=f"{field_name}.*cross-family"):
            RepositoryContext(**kwargs)

    def test_frozen(self):
        context = a_repository()
        with pytest.raises(AttributeError):
            context.connection_id = "other"  # type: ignore[misc]

    def test_subject_key_compares_family_connection_repository(self):
        assert a_repository().subject_key() == "github:github:acme/widgets:acme/widgets"
        assert (
            a_repository().subject_key() != a_repository(repository_id="acme/other").subject_key()
        )

    def test_digest_is_deterministic_and_value_sensitive(self):
        one = a_repository()
        two = a_repository()
        assert one.digest() == two.digest()
        assert one.digest() != a_repository(repository_id="acme/other").digest()
        assert len(one.digest()) == 64


# -- ResumeSpec ---------------------------------------------------------------


class TestResumeSpec:
    def test_fresh_mode_has_no_checkpoint(self):
        spec = ResumeSpec(resume_mode="fresh", authority="lane:run-1")
        assert spec.checkpoint_ref == ""
        assert spec.generation == 0

    def test_restart_mode_discards_wip(self):
        spec = ResumeSpec(resume_mode="restart", authority="cmd-abc123")
        assert spec.checkpoint_ref == ""

    def test_required_mode_binds_the_exact_checkpoint(self):
        spec = ResumeSpec(
            resume_mode="required",
            checkpoint_ref=f"run-1@{HEX64}",
            generation=2,
            source_oid="f" * 40,
            authority="cmd-abc123",
        )
        assert spec.checkpoint_ref == f"run-1@{HEX64}"
        assert spec.generation == 2

    @pytest.mark.parametrize("mode", ["", None, "resume", "REQUIRED", "latest"])
    def test_unknown_resume_mode_is_refused(self, mode):
        with pytest.raises(
            (CompositionBoundaryError, MissingEnvelopeFieldError), match="resume_mode"
        ):
            ResumeSpec(resume_mode=mode, authority="cmd-abc123")  # type: ignore[arg-type]

    def test_required_without_checkpoint_ref_is_refused(self):
        with pytest.raises(MissingEnvelopeFieldError, match="checkpoint_ref"):
            ResumeSpec(resume_mode="required", authority="cmd-abc123")

    @pytest.mark.parametrize(
        "ref", ["no-at-sign", f"run-1@{'zz' * 32}", f"@{HEX64}", "run-1@short"]
    )
    def test_malformed_checkpoint_ref_is_refused(self, ref):
        with pytest.raises(CompositionBoundaryError, match="checkpoint_ref"):
            ResumeSpec(
                resume_mode="required",
                checkpoint_ref=ref,
                generation=1,
                authority="cmd-abc123",
            )

    def test_required_with_generation_zero_is_refused(self):
        with pytest.raises(CompositionBoundaryError, match="generation"):
            ResumeSpec(
                resume_mode="required",
                checkpoint_ref=f"run-1@{HEX64}",
                generation=0,
                authority="cmd-abc123",
            )

    @pytest.mark.parametrize("mode", ["fresh", "restart"])
    def test_checkpointless_mode_with_a_ref_is_a_contradiction(self, mode):
        with pytest.raises(CompositionBoundaryError, match="checkpoint_ref"):
            ResumeSpec(
                resume_mode=mode,
                checkpoint_ref=f"run-1@{HEX64}",
                generation=1,
                authority="cmd-abc123",
            )

    def test_missing_authority_is_refused(self):
        with pytest.raises(MissingEnvelopeFieldError, match="authority"):
            ResumeSpec(resume_mode="fresh", authority="")

    def test_payload_round_trip(self):
        spec = ResumeSpec(
            resume_mode="required",
            checkpoint_ref=f"run-1@{HEX64}",
            generation=3,
            source_oid="f" * 40,
            authority="cmd-abc123",
        )
        again = ResumeSpec.from_payload(spec.to_payload())
        assert again == spec

    def test_pre_next03_payload_is_refused_never_re_read_as_fresh(self):
        with pytest.raises(CompositionBoundaryError, match="pre-NEXT-03"):
            ResumeSpec.from_payload({"checkpoint_sequence": 2})


# -- derive_execution_attempt_id (R36-06) --------------------------------------


class TestDeriveExecutionAttemptId:
    def test_two_attempts_from_the_same_source_commit_derive_distinct_ids(self):
        """AT-07 core: the source OID is shared, the EXECUTION identity is
        not — the durable attempt ordinal is a derivation member."""
        first = the_execution_id(attempt_ordinal=0)
        retry = the_execution_id(attempt_ordinal=1)
        assert first != retry
        assert len(first) == 64

    def test_the_same_durable_rows_derive_the_identical_id(self):
        """Repeat delivery: the same (run, ordinal, source) rows — what a
        restart between envelope construction and the native start
        re-reads — derive the identical execution identity, never a fresh
        one."""
        assert the_execution_id() == the_execution_id()
        assert the_execution_id() == derive_execution_attempt_id(
            run_id="run-1", attempt_ordinal=0, source_base_oid=SOURCE_OID
        )

    @pytest.mark.parametrize(
        ("knob", "value"),
        [
            ("run_id", "run-2"),
            ("attempt_ordinal", 1),
            ("source_base_oid", OTHER_SOURCE_OID),
        ],
    )
    def test_every_durable_member_is_load_bearing(self, knob, value):
        assert the_execution_id(**{knob: value}) != the_execution_id()

    @pytest.mark.parametrize("bad_ordinal", [True, "1", 1.0, -1])
    def test_a_malformed_ordinal_is_refused(self, bad_ordinal):
        with pytest.raises(CompositionBoundaryError, match="attempt_ordinal"):
            the_execution_id(attempt_ordinal=bad_ordinal)  # type: ignore[arg-type]

    def test_an_empty_run_id_is_refused(self):
        with pytest.raises(MissingEnvelopeFieldError, match="run_id"):
            the_execution_id(run_id="")

    @pytest.mark.parametrize("counter", ["7", "0", "0013", "12345678901234"])
    def test_a_checkpoint_sequence_or_command_watermark_is_refused_as_the_source(self, counter):
        """The axis guard at derivation time: a counter is never laundered
        into an identity through the source member."""
        with pytest.raises(CompositionBoundaryError, match="watermark"):
            the_execution_id(source_base_oid=counter)


# -- AttemptStartSpec ---------------------------------------------------------


class TestAttemptStartSpec:
    def test_happy_path(self):
        spec = an_attempt_start()
        assert spec.run_id == "run-1"
        assert spec.execution_attempt_id == the_execution_id()
        assert spec.source_base_oid == SOURCE_OID
        assert spec.authority_epoch == 0
        assert spec.continuation_ref_digest == ""
        assert spec.schema_version == ATTEMPT_START_V2
        assert spec.resume_mode == "fresh"
        assert spec.lease_id == "lease-9"

    @pytest.mark.parametrize(
        ("field_name", "bad_value"),
        [
            ("run_id", ""),
            ("run_id", None),
            ("execution_attempt_id", ""),
            ("execution_attempt_id", "   "),
            ("execution_attempt_id", "not-a-digest"),
            ("source_base_oid", ""),
            ("source_base_oid", "   "),
            ("repository", None),
            ("repository", "github:acme/widgets"),
            ("profile_digest", ""),
            ("profile_digest", "not-a-digest"),
            ("resume_mode", ""),
            ("resume_mode", "whatever"),
            ("lease_id", ""),
            ("lease_id", None),
            ("authority_epoch", True),
            ("authority_epoch", "3"),
            ("authority_epoch", -1),
            ("continuation_ref_digest", "not-a-digest"),
            ("schema_version", 3),
        ],
    )
    def test_missing_or_bad_envelope_field_is_a_typeerror_naming_it(self, field_name, bad_value):
        kwargs = {
            "run_id": "run-1",
            "execution_attempt_id": the_execution_id(),
            "source_base_oid": SOURCE_OID,
            "repository": a_repository(),
            "profile_digest": HEX64,
            "resume_mode": "fresh",
            "lease_id": "lease-9",
            "authority_epoch": 0,
            "continuation_ref_digest": "",
            "schema_version": ATTEMPT_START_V2,
        }
        kwargs[field_name] = bad_value
        with pytest.raises((MissingEnvelopeFieldError, CompositionBoundaryError), match=field_name):
            AttemptStartSpec(**kwargs)

    def test_there_are_no_defaults(self):
        import inspect

        signature = inspect.signature(AttemptStartSpec)
        defaulted = [
            name
            for name, parameter in signature.parameters.items()
            if parameter.default is not inspect.Parameter.empty
        ]
        assert not defaulted

    @pytest.mark.parametrize(
        ("field_name", "bad_value"),
        [
            ("run_id", the_execution_id()),  # run id == execution id
            ("source_base_oid", "run-1"),  # run id == source oid
        ],
    )
    def test_the_run_identity_is_never_an_attempt_or_source_identity(self, field_name, bad_value):
        with pytest.raises(CompositionBoundaryError, match="never interchangeable"):
            an_attempt_start(**{field_name: bad_value})

    def test_the_execution_identity_is_never_the_source_oid(self):
        """R36-06's core refusal: an execution identity is not a code
        revision — two executions may share one source."""
        shared = "e" * 64  # hex64 so the shape checks pass; only the axes collide
        with pytest.raises(CompositionBoundaryError, match="never a code revision"):
            an_attempt_start(source_base_oid=shared, execution_attempt_id=shared)

    @pytest.mark.parametrize("counter", ["7", "42", "0013"])
    def test_a_counter_shaped_source_oid_is_refused(self, counter):
        """The checkpoint sequence / applied-command watermark shapes are
        refused on the source axis at construction too."""
        with pytest.raises(CompositionBoundaryError, match="watermark"):
            an_attempt_start(source_base_oid=counter)

    def test_required_mode_pins_the_continuation_ref(self):
        spec = an_attempt_start(resume_mode="required", continuation_ref_digest=OTHER_HEX64)
        assert spec.continuation_ref_digest == OTHER_HEX64
        assert spec.to_document()["continuation_ref_digest"] == OTHER_HEX64

    def test_required_mode_without_a_continuation_ref_is_refused(self):
        with pytest.raises(MissingEnvelopeFieldError, match="continuation_ref_digest"):
            an_attempt_start(resume_mode="required")

    @pytest.mark.parametrize("mode", ["fresh", "restart"])
    def test_checkpointless_mode_with_a_continuation_ref_is_a_contradiction(self, mode):
        with pytest.raises(CompositionBoundaryError, match="continuation_ref_digest"):
            an_attempt_start(resume_mode=mode, continuation_ref_digest=OTHER_HEX64)

    def test_non_sha256_profile_digest_is_refused(self):
        with pytest.raises(CompositionBoundaryError, match="profile_digest"):
            an_attempt_start(profile_digest=HEX64[:63])

    def test_envelope_digest_is_deterministic_and_value_sensitive(self):
        one = an_attempt_start()
        two = an_attempt_start()
        assert one.envelope_digest() == two.envelope_digest()
        assert (
            one.envelope_digest()
            != an_attempt_start(
                repository=a_repository(repository_id="acme/other")
            ).envelope_digest()
        )
        assert (
            one.envelope_digest()
            != an_attempt_start(
                execution_attempt_id=the_execution_id(attempt_ordinal=1)
            ).envelope_digest()
        )

    def test_serialization_order_never_moves_the_digest(self):
        """The inverse negative (AT-07): an unchanged intent is not
        misclassified as new merely because the construction order
        changed — the digest is over the canonical document."""
        kwargs = {
            "run_id": "run-1",
            "execution_attempt_id": the_execution_id(),
            "source_base_oid": SOURCE_OID,
            "repository": a_repository(),
            "profile_digest": HEX64,
            "resume_mode": "fresh",
            "lease_id": "lease-9",
            "authority_epoch": 0,
            "continuation_ref_digest": "",
            "schema_version": ATTEMPT_START_V2,
        }
        forward = AttemptStartSpec(**kwargs)
        backward = AttemptStartSpec(**dict(reversed(list(kwargs.items()))))
        assert forward.envelope_digest() == backward.envelope_digest()

    def test_a_changed_authority_epoch_changes_the_authority_digest_and_nothing_else(self):
        """R36-06 acceptance: the epoch is INSIDE the envelope digest —
        source, profile and lease unchanged, the digest still moves."""
        low = an_attempt_start(authority_epoch=2)
        high = an_attempt_start(authority_epoch=3)
        assert low.source_base_oid == high.source_base_oid
        assert low.profile_digest == high.profile_digest
        assert low.lease_id == high.lease_id
        assert low.execution_attempt_id == high.execution_attempt_id
        assert low.envelope_digest() != high.envelope_digest()

    def test_a_changed_execution_identity_changes_the_digest_with_the_epoch_unchanged(self):
        first = an_attempt_start()
        retry = an_attempt_start(execution_attempt_id=the_execution_id(attempt_ordinal=1))
        assert first.authority_epoch == retry.authority_epoch
        assert first.source_base_oid == retry.source_base_oid
        assert first.envelope_digest() != retry.envelope_digest()

    def test_to_document_carries_every_authority_bearing_member(self):
        document = an_attempt_start().to_document()
        assert document["schema_version"] == ATTEMPT_START_V2
        assert document["execution_attempt_id"] == the_execution_id()
        assert document["source_base_oid"] == SOURCE_OID
        assert document["authority_epoch"] == 0
        assert document["continuation_ref_digest"] == ""
        # The digest is over exactly this document (documented equivalence).
        assert an_attempt_start().envelope_digest() == an_attempt_start().envelope_digest()


# -- the v1 compat shape -------------------------------------------------------


class TestAttemptStartV1Compat:
    def the_v1_spec(self) -> AttemptStartSpec:
        return an_attempt_start(execution_attempt_id="", schema_version=ATTEMPT_START_V1)

    def test_a_v1_read_carries_no_execution_identity_and_never_invents_one(self):
        spec = self.the_v1_spec()
        assert spec.schema_version == ATTEMPT_START_V1
        assert spec.execution_attempt_id == ""

    def test_a_v1_spec_never_carries_a_manufactured_execution_identity(self):
        with pytest.raises(CompositionBoundaryError, match="never invents one"):
            an_attempt_start(schema_version=ATTEMPT_START_V1)

    def test_a_v1_document_serializes_the_historical_shape(self):
        """v1 serializes with ``attempt_id`` carrying the source OID and
        WITHOUT the epoch — byte-compatible with a pre-R36-06 envelope,
        so the historical digest recomputes (audit, not upgrade)."""
        from forge.runs.spec import canonical_json_digest

        spec = self.the_v1_spec()
        document = spec.to_document()
        assert "attempt_id" in document
        assert document["attempt_id"] == SOURCE_OID
        assert "authority_epoch" not in document
        assert "execution_attempt_id" not in document
        assert spec.envelope_digest() == canonical_json_digest(document)


# -- assert_attempt_start -----------------------------------------------------


class TestAssertAttemptStart:
    def test_happy_path_returns_none(self):
        assert assert_attempt_start(an_attempt_start(), a_repository()) is None

    def test_repository_identity_mismatch_names_the_field_and_both_keys(self):
        spec = an_attempt_start(
            repository=RepositoryContext(
                provider_family="gitlab", connection_id="conn-gl", repository_id="acme/widgets"
            )
        )
        with pytest.raises(CompositionBoundaryError, match=r"'repository'.*gitlab.*github"):
            assert_attempt_start(spec, a_repository())

    def test_same_family_other_repository_is_still_a_mismatch(self):
        spec = an_attempt_start(repository=a_repository(repository_id="acme/other"))
        with pytest.raises(CompositionBoundaryError, match="'repository'"):
            assert_attempt_start(spec, a_repository())

    def test_wrong_spec_type_names_spec(self):
        with pytest.raises(CompositionBoundaryError, match="'spec'"):
            assert_attempt_start("run-1", a_repository())  # type: ignore[arg-type]

    def test_wrong_context_type_names_context(self):
        with pytest.raises(CompositionBoundaryError, match="'context'"):
            assert_attempt_start(an_attempt_start(), {"provider": "github"})  # type: ignore[arg-type]

    @staticmethod
    def _bypassed(**overrides) -> AttemptStartSpec:
        """A spec constructed valid, then mutated behind the constructor.

        The boundary assert exists as defense in depth — it must re-check
        what construction guarantees, because construction can be
        bypassed (``object.__setattr__`` on a frozen value) exactly like
        an adapter forgetting to construct the envelope at all.
        """
        spec = an_attempt_start()
        for name, value in overrides.items():
            object.__setattr__(spec, name, value)
        return spec

    def test_missing_profile_digest_is_refused_at_the_boundary(self):
        spec = self._bypassed(profile_digest="")
        with pytest.raises(CompositionBoundaryError, match="profile_digest"):
            assert_attempt_start(spec, a_repository())

    def test_missing_lease_identity_is_refused_at_the_boundary(self):
        spec = self._bypassed(lease_id="")
        with pytest.raises(CompositionBoundaryError, match="lease_id"):
            assert_attempt_start(spec, a_repository())

    def test_a_bypassed_execution_identity_is_refused_at_the_boundary(self):
        spec = self._bypassed(execution_attempt_id="not-hex64")
        with pytest.raises(CompositionBoundaryError, match="execution_attempt_id"):
            assert_attempt_start(spec, a_repository())

    def test_a_bypassed_identity_conflation_is_refused_at_the_boundary(self):
        shared = "e" * 64
        spec = self._bypassed(execution_attempt_id=shared, source_base_oid=shared)
        with pytest.raises(CompositionBoundaryError, match="never a code revision"):
            assert_attempt_start(spec, a_repository())


# -- assert_publication_identity (R36-06) --------------------------------------


class TestAssertPublicationIdentity:
    def the_persisted_document(self, **overrides) -> dict:
        document = {
            "version": 2,
            "envelope_digest": HEX64,
            "execution_attempt_id": the_execution_id(),
            "source_base_oid": SOURCE_OID,
            "authority_epoch": 2,
        }
        document.update(overrides)
        return document

    def test_the_current_execution_identity_authorizes(self):
        assert (
            assert_publication_identity(
                persisted=self.the_persisted_document(),
                presented_execution_attempt_id=the_execution_id(),
                presented_source_base_oid=SOURCE_OID,
                presented_authority_epoch=2,
            )
            is None
        )

    def test_a_source_oid_match_alone_never_authorizes_a_stale_callback(self):
        """AT-07: the callback from the PRECEDING attempt presents the same
        source OID — refused, and the refusal says exactly why."""
        with pytest.raises(CompositionBoundaryError, match="source-OID match alone"):
            assert_publication_identity(
                persisted=self.the_persisted_document(),
                presented_execution_attempt_id=the_execution_id(attempt_ordinal=1),
                presented_source_base_oid=SOURCE_OID,
            )

    def test_a_callback_without_an_execution_identity_is_refused(self):
        with pytest.raises(CompositionBoundaryError, match="no execution_attempt_id"):
            assert_publication_identity(
                persisted=self.the_persisted_document(), presented_execution_attempt_id=""
            )

    def test_a_run_without_a_persisted_envelope_fails_closed(self):
        with pytest.raises(CompositionBoundaryError, match="no persisted attempt_start"):
            assert_publication_identity(
                persisted=None, presented_execution_attempt_id=the_execution_id()
            )

    def test_a_v1_envelope_cannot_authorize(self):
        """The compat policy: the weaker identity is readable for audit but
        refused for authority-bearing comparisons — no new guarantee is
        claimed over an old envelope."""
        with pytest.raises(CompositionBoundaryError, match="version 1"):
            assert_publication_identity(
                persisted={"version": 1, "attempt_base": SOURCE_OID, "authority_epoch": 0},
                presented_execution_attempt_id=the_execution_id(),
                presented_source_base_oid=SOURCE_OID,
            )

    def test_an_epoch_mismatch_is_refused_naming_both(self):
        with pytest.raises(CompositionBoundaryError, match="authority.epoch_mismatch"):
            assert_publication_identity(
                persisted=self.the_persisted_document(),
                presented_execution_attempt_id=the_execution_id(),
                presented_authority_epoch=3,
            )


# -- CompositionMatrix --------------------------------------------------------


class TestCompositionMatrix:
    def test_unregistered_pair_reads_untested(self):
        matrix = CompositionMatrix()
        assert matrix.edge("lane:resume/required", "checkpoint_channel:NEXT-03") == EDGE_UNTESTED

    def test_registered_verdicts_round_trip(self):
        matrix = CompositionMatrix()
        matrix.register_untested("a", "b")
        matrix.register_unsupported("c", "d")
        matrix.register_verified("e", "f", evidence="ci:11827")
        assert matrix.edge("a", "b") == EDGE_UNTESTED
        assert matrix.edge("c", "d") == EDGE_UNSUPPORTED
        assert matrix.edge("e", "f") == EDGE_VERIFIED

    @pytest.mark.parametrize("evidence", ["", "   ", None])
    def test_verified_without_evidence_cannot_be_recorded(self, evidence):
        matrix = CompositionMatrix()
        with pytest.raises(CompositionBoundaryError, match="evidence"):
            matrix.register_verified("a", "b", evidence=evidence)  # type: ignore[arg-type]

    def test_verified_edge_dataclass_requires_evidence_too(self):
        with pytest.raises(CompositionBoundaryError, match="evidence"):
            MatrixEdge("a", "b", EDGE_VERIFIED)

    def test_bad_verdict_is_refused(self):
        with pytest.raises(CompositionBoundaryError, match="verdict"):
            MatrixEdge("a", "b", "maybe")

    def test_edges_are_directional(self):
        matrix = CompositionMatrix()
        matrix.register_verified("consumer:v8", "provider:v3", evidence="ci:11827")
        matrix.register_unsupported("provider:v3", "consumer:v8")
        assert matrix.edge("consumer:v8", "provider:v3") == EDGE_VERIFIED
        assert matrix.edge("provider:v3", "consumer:v8") == EDGE_UNSUPPORTED

    def test_blocked_by_finds_the_same_edge_from_both_directions(self):
        matrix = CompositionMatrix()
        matrix.register_untested("consumer:v8", "provider:v3")
        from_consumer = matrix.blocked_by("consumer:v8")
        from_provider = matrix.blocked_by("provider:v3")
        assert from_consumer == from_provider
        assert len(from_consumer) == 1
        assert from_consumer[0].from_key == "consumer:v8"
        assert from_consumer[0].to_key == "provider:v3"

    def test_blocked_by_ignores_verified_edges(self):
        matrix = CompositionMatrix()
        matrix.register_verified("consumer:v8", "provider:v3", evidence="ci:11827")
        matrix.register_unsupported("provider:v3", "consumer:v9")
        assert matrix.blocked_by("consumer:v8") == ()
        blocked = matrix.blocked_by("provider:v3")
        assert [edge.to_key for edge in blocked] == ["consumer:v9"]

    def test_blocked_by_an_unknown_key_is_empty_not_a_guess(self):
        # The query never invents edges: an unregistered combination is
        # drift (matrix_drift's finding), not a fabricated blocker.
        assert CompositionMatrix().blocked_by("never-registered") == ()

    @pytest.mark.parametrize("demote", ["untested", "unsupported"])
    def test_verified_edges_are_never_silently_demoted(self, demote):
        matrix = CompositionMatrix()
        matrix.register_verified("a", "b", evidence="ci:11827")
        with pytest.raises(CompositionBoundaryError, match="never silently demoted"):
            if demote == "untested":
                matrix.register_untested("a", "b")
            else:
                matrix.register_unsupported("a", "b")
        assert matrix.edge("a", "b") == EDGE_VERIFIED

    def test_re_verification_with_new_evidence_is_a_re_promotion(self):
        matrix = CompositionMatrix()
        matrix.register_verified("a", "b", evidence="ci:11827")
        matrix.register_verified("a", "b", evidence="ci:12051")
        assert matrix.edge("a", "b") == EDGE_VERIFIED
        assert matrix.edges()[0].evidence == "ci:12051"

    def test_untested_may_be_promoted_to_unsupported_and_back(self):
        matrix = CompositionMatrix()
        matrix.register_untested("a", "b")
        matrix.register_unsupported("a", "b")
        assert matrix.edge("a", "b") == EDGE_UNSUPPORTED
        matrix.register_untested("a", "b")
        assert matrix.edge("a", "b") == EDGE_UNTESTED


# -- matrix_drift -------------------------------------------------------------


class TestMatrixDrift:
    def test_shipped_combination_absent_from_the_matrix_is_a_finding(self):
        matrix = CompositionMatrix()
        findings = matrix_drift(["lane:claude-sdk:2"], matrix)
        assert len(findings) == 1
        assert findings[0].combination == "lane:claude-sdk:2"
        assert "no qualification evidence in scope" in findings[0].detail

    def test_shipped_combination_with_untested_edge_is_a_finding(self):
        matrix = CompositionMatrix()
        matrix.register_untested("consumer:v8", "provider:v3")
        findings = matrix_drift(["consumer:v8"], matrix)
        assert [f.combination for f in findings] == ["consumer:v8"]
        assert "untested" in findings[0].detail
        assert findings[0].edges == matrix.blocked_by("consumer:v8")

    def test_shipped_combination_with_unsupported_edge_names_it_explicitly(self):
        matrix = CompositionMatrix()
        matrix.register_unsupported("consumer:v8", "provider:v3")
        findings = matrix_drift(["consumer:v8"], matrix)
        assert "explicitly unsupported" in findings[0].detail

    def test_fully_verified_combination_is_no_finding(self):
        matrix = CompositionMatrix()
        matrix.register_verified("consumer:v8", "provider:v3", evidence="ci:11827")
        matrix.register_verified("provider:v3", "checkpoint_channel:NEXT-03", evidence="ci:11830")
        assert matrix_drift(["consumer:v8", "provider:v3"], matrix) == []

    def test_no_shipped_combinations_no_findings(self):
        assert matrix_drift([], CompositionMatrix()) == []

    def test_finding_is_a_dataclass_pairing_combination_detail_edges(self):
        finding = DriftFinding(combination="x", detail="d")
        assert (finding.combination, finding.detail, finding.edges) == ("x", "d", ())
