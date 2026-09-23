"""ADR-0029 / R32-24: the composition boundary types and the recorded-evidence matrix.

Pinned here, per the issue's gates:

- construction validation for all three types — every refusal case
  (empty/mismatched provider family, cross-family identity mixing,
  missing envelope fields as TypeError, unknown resume mode);
- :func:`assert_attempt_start` happy path + each violation;
- :class:`CompositionMatrix` verified/untested/unsupported edges,
  ``blocked_by`` consistency in both directions, evidence-gated
  promotion, no silent demotion;
- :func:`matrix_drift` findings for shipped-but-unqualified combinations;
- digest determinism (equal values hash equal, whatever the construction
  order; different values hash different).
"""

import pytest

from forge.runs.composition import (
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
    matrix_drift,
)

HEX64 = "ab" * 32
OTHER_HEX64 = "cd" * 32


def a_repository(**overrides) -> RepositoryContext:
    base = {
        "provider_family": "github",
        "connection_id": "github:acme/widgets",
        "repository_id": "acme/widgets",
    }
    base.update(overrides)
    return RepositoryContext(**base)


def an_attempt_start(repository: RepositoryContext | None = None) -> AttemptStartSpec:
    return AttemptStartSpec(
        run_id="run-1",
        attempt_id="attempt-1",
        repository=repository or a_repository(),
        profile_digest=HEX64,
        resume_mode="fresh",
        lease_id="lease-9",
    )


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


# -- AttemptStartSpec ---------------------------------------------------------


class TestAttemptStartSpec:
    def test_happy_path(self):
        spec = an_attempt_start()
        assert spec.run_id == "run-1"
        assert spec.resume_mode == "fresh"
        assert spec.lease_id == "lease-9"

    @pytest.mark.parametrize(
        ("field_name", "bad_value"),
        [
            ("run_id", ""),
            ("run_id", None),
            ("attempt_id", ""),
            ("attempt_id", "   "),
            ("repository", None),
            ("repository", "github:acme/widgets"),
            ("profile_digest", ""),
            ("profile_digest", "not-a-digest"),
            ("resume_mode", ""),
            ("resume_mode", "whatever"),
            ("lease_id", ""),
            ("lease_id", None),
        ],
    )
    def test_missing_or_bad_envelope_field_is_a_typeerror_naming_it(self, field_name, bad_value):
        kwargs = {
            "run_id": "run-1",
            "attempt_id": "attempt-1",
            "repository": a_repository(),
            "profile_digest": HEX64,
            "resume_mode": "fresh",
            "lease_id": "lease-9",
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

    def test_run_id_is_not_the_attempt_id(self):
        with pytest.raises(CompositionBoundaryError, match="run_id.*attempt_id"):
            AttemptStartSpec(
                run_id="run-1",
                attempt_id="run-1",
                repository=a_repository(),
                profile_digest=HEX64,
                resume_mode="fresh",
                lease_id="lease-9",
            )

    def test_non_sha256_profile_digest_is_refused(self):
        with pytest.raises(CompositionBoundaryError, match="profile_digest"):
            AttemptStartSpec(
                run_id="run-1",
                attempt_id="attempt-1",
                repository=a_repository(),
                profile_digest=HEX64[:63],
                resume_mode="fresh",
                lease_id="lease-9",
            )

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
            != AttemptStartSpec(
                run_id="run-1",
                attempt_id="attempt-2",
                repository=a_repository(),
                profile_digest=HEX64,
                resume_mode="required",
                lease_id="lease-9",
            ).envelope_digest()
        )


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
