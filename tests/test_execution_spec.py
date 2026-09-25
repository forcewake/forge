"""R38-17 (issue #318): the versioned execution/delivery specification.

ADR-0032's three contracts under test:

- the SPEC: constructed once at dispatch (fail-closed on every
  authority-bearing member), frozen, digest-stable — a field change is
  a different digest — and rendered into the SMALL pinned template
  variable set the shipped lane templates consume (never re-derived
  from ambient variables; the ambient fallbacks that remain are the
  documented, non-authority-bearing ones);
- the COMPOSITION MATRIX: the small supported list plus the preflight
  — a supported combination passes with its contract versions, every
  impossible combination refuses with a PRECISE incompatibility naming
  the axis and the supported alternatives;
- the COMPATIBILITY RULE: a persisted v1 document round-trips through
  the current composition; an unknown schema version, an unknown key
  or a departed matrix row refuses explicitly.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from forge.adaptive.execution_spec import (
    AMBIENT_FALLBACK_VARIABLES,
    CREDENTIAL_MODES,
    EXECUTION_SPEC_SCHEMA,
    EXECUTION_SPEC_VERSION,
    ExecutionSpec,
    CompositionRefusal,
    CompositionRequest,
    RESUME_CAPABLE_RECIPES,
    RESUME_MODE_WORDS,
    SupportedComposition,
    compose_execution_spec,
    preflight_composition,
    read_execution_spec,
    render_template_variables,
    supported_compositions,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ADR = REPO_ROOT / "docs" / "adr" / "0032-versioned-execution-spec.md"

#: A well-formed hex64 (the derived execution identity / checkpoint
#: content addresses / profile digests all share the shape).
_HEX64 = "a" * 64
_HEX64_B = "b" * 64


def _spec(**overrides: str) -> ExecutionSpec:
    """The canonical composed spec — the GitHub SDK-lane composition."""
    kwargs: dict[str, str] = dict(
        run_id="run-123",
        execution_attempt_id=_HEX64,
        driver="claude-sdk-lane",
        provider="github",
        runtime_recipe="github-harness-entry",
        credential_mode="github-native-secret",
        credential_ref="ENV_ANTHROPIC_AUTH_TOKEN",
        resume_mode="fresh",
        model="glm-5.3-flash",
        profile_digest=_HEX64_B,
    )
    kwargs.update(overrides)
    return compose_execution_spec(**kwargs)


class TestSpecConstruction:
    def test_the_canonical_composition_constructs_and_pins(self) -> None:
        spec = _spec()
        assert spec.run_id == "run-123"
        assert spec.driver == "claude-sdk-lane"
        assert spec.resume_mode == "fresh"
        assert spec.credential_mode == "github-native-secret"
        assert spec.credential_ref == "ENV_ANTHROPIC_AUTH_TOKEN"
        assert spec.model == "glm-5.3-flash"
        assert spec.profile_digest == _HEX64_B
        # The artifact contract rides pinned: the packaged collector
        # entry and the non-hidden output root, never ambient layout.
        assert spec.collector_entry == "python -m forge.harness_entry --collect-candidate"
        assert spec.output_root == "forge-output"
        assert spec.schema_version == EXECUTION_SPEC_SCHEMA

    def test_a_required_resume_pins_its_checkpoint_by_content_address(self) -> None:
        spec = _spec(resume_mode="required", continuation_ref=_HEX64)
        assert spec.resume_mode == "required"
        assert spec.continuation_ref == _HEX64

    def test_an_unpinned_model_is_recorded_not_guessed(self) -> None:
        spec = _spec(model="")
        assert spec.model == ""
        rendered = render_template_variables(spec)
        # The pin renders empty — the driver's DOCUMENTED vendor default
        # then applies (ADR-0032 §3), never a permissive guess here.
        assert rendered["FORGE_MODEL"] == ""


class TestSpecFailsClosed:
    """Every authority-bearing member refuses on emptiness/shape."""

    @pytest.mark.parametrize(
        "overrides",
        [
            {"run_id": ""},  # identity
            {"execution_attempt_id": ""},  # execution identity
            {"execution_attempt_id": "not-hex"},
            {"driver": ""},  # the harness axis
            {"resume_mode": ""},  # the continuation contract
            {"resume_mode": "FRESH"},  # not the decision's vocabulary
            {"profile_digest": ""},  # the envelope's profile axis
            {"profile_digest": "zz"},
            {"credential_mode": "ambient-telepathy"},  # not #303's vocabulary
            {"continuation_ref": "checkpoint-7"},  # not a content address
        ],
    )
    def test_the_member_refuses(self, overrides: dict[str, str]) -> None:
        with pytest.raises(CompositionRefusal) as excinfo:
            _spec(**overrides)
        assert str(excinfo.value)

    def test_a_required_resume_without_its_checkpoint_refuses(self) -> None:
        with pytest.raises(CompositionRefusal, match="continuation_ref"):
            _spec(resume_mode="required", continuation_ref="")

    def test_a_bound_credential_mode_without_its_ref_refuses(self) -> None:
        with pytest.raises(CompositionRefusal, match="credential_ref"):
            _spec(credential_ref="")

    def test_ambient_legacy_is_the_only_refless_mode(self) -> None:
        spec = _spec(driver="claude-code", credential_mode="ambient-legacy", credential_ref="")
        assert spec.credential_ref == ""


class TestDigestAndRendering:
    def test_a_field_change_is_a_different_digest(self) -> None:
        base = _spec()
        changed = _spec(model="glm-5.3")
        assert base.spec_digest() != changed.spec_digest()
        # The digest is deterministic by value — the reconciliation
        # axis (the rendered set names the persisted spec iff equal).
        assert _spec().spec_digest() == base.spec_digest()

    def test_the_rendered_set_is_the_small_pinned_one(self) -> None:
        rendered = render_template_variables(_spec())
        assert set(rendered) == {
            "FORGE_RUN_ID",
            "FORGE_DRIVER",
            "FORGE_MODEL",
            "FORGE_LANE_RESUME_MODE",
            "FORGE_RESUME_CHECKPOINT",
            "FORGE_CREDENTIAL_REF",
            "FORGE_CREDENTIAL_REDEEM",
            "FORGE_EXECUTION_SPEC",
            "FORGE_EXECUTION_SPEC_DIGEST",
        }
        assert rendered["FORGE_RUN_ID"] == "run-123"
        assert rendered["FORGE_DRIVER"] == "claude-sdk-lane"
        assert rendered["FORGE_MODEL"] == "glm-5.3-flash"
        assert rendered["FORGE_LANE_RESUME_MODE"] == "fresh"
        assert rendered["FORGE_CREDENTIAL_REDEEM"] == ""
        assert rendered["FORGE_EXECUTION_SPEC"] == EXECUTION_SPEC_SCHEMA
        assert rendered["FORGE_EXECUTION_SPEC_DIGEST"] == _spec().spec_digest()

    def test_the_redemption_route_renders_its_flag(self) -> None:
        rendered = render_template_variables(
            _spec(
                credential_mode="runner-redemption",
                credential_ref="env:ANTHROPIC_AUTH_TOKEN",
                grant_id="c" * 32,
            )
        )
        assert rendered["FORGE_CREDENTIAL_REDEEM"] == "1"

    def test_a_required_resume_renders_its_pinned_checkpoint(self) -> None:
        rendered = render_template_variables(_spec(resume_mode="required", continuation_ref=_HEX64))
        assert rendered["FORGE_LANE_RESUME_MODE"] == "required"
        assert rendered["FORGE_RESUME_CHECKPOINT"] == _HEX64

    def test_the_remaining_ambient_fallbacks_are_documented(self) -> None:
        """ADR-0032 §3: every ambient variable a template may still
        consult is listed with its non-authority reason — the list is
        the contract, not an aspiration."""
        names = [name for name, _reason in AMBIENT_FALLBACK_VARIABLES]
        assert "FORGE_HARNESS_MCP" in names
        assert "FORGE_STEERING_ENABLED" in names
        assert all(reason.strip() for _name, reason in AMBIENT_FALLBACK_VARIABLES)
        # Nothing the spec PINS is an ambient fallback.
        pinned = set(render_template_variables(_spec()))
        assert not pinned & set(names)


class TestCompositionMatrix:
    def test_the_matrix_is_small_and_tested(self) -> None:
        rows = supported_compositions()
        assert 0 < len(rows) <= 12  # a small list, not a combinatorial dump
        keys = {row.combination_key() for row in rows}
        assert len(keys) == len(rows)  # no duplicate combinations

    def test_every_row_carries_the_three_contract_versions(self) -> None:
        for row in supported_compositions():
            assert row.caller_contract == "forge.attempt-start/2"
            assert row.template_contract == EXECUTION_SPEC_SCHEMA
            assert row.consumer_contract == "forge.candidate-meta/2"
            assert row.provider in {"github", "gitlab", "azure"}
            assert row.credential_route in CREDENTIAL_MODES

    def test_resume_capability_is_the_recipe_not_the_spelling(self) -> None:
        for row in supported_compositions():
            assert row.resume_supported == (row.runtime_recipe in RESUME_CAPABLE_RECIPES)
        # The honest boundaries the matrix carries:
        recipes = {row.runtime_recipe: row.resume_supported for row in supported_compositions()}
        assert recipes["github-harness-entry"] is True
        assert recipes["gitlab-sdk-lane"] is True
        assert recipes["gitlab-batch-script"] is False  # no restore in the batch lane
        assert recipes["azure-harness-entry"] is False  # no resume surface on Azure

    def test_a_supported_combination_passes_with_its_row(self) -> None:
        row = preflight_composition(
            supported_compositions(),
            CompositionRequest(
                provider="gitlab",
                runtime_recipe="gitlab-sdk-lane",
                harness="claude-sdk-lane",
                credential_route="gitlab-protected-variable",
                resume_mode="required",
            ),
        )
        assert row.resume_supported is True
        assert row.template_contract == EXECUTION_SPEC_SCHEMA

    def test_a_supplied_empty_matrix_refuses(self) -> None:
        with pytest.raises(CompositionRefusal, match="empty"):
            preflight_composition((), CompositionRequest("github", "r", "h", "route", ""))


class TestPreflightRefusals:
    """Each impossible combination refuses PRECISELY (#318's negative
    test): the message names the axis and the supported alternatives."""

    def _request(self, **overrides: str) -> CompositionRequest:
        kwargs = dict(
            provider="github",
            runtime_recipe="github-harness-entry",
            harness="claude-sdk-lane",
            credential_route="github-native-secret",
            resume_mode="fresh",
        )
        kwargs.update(overrides)
        return CompositionRequest(**kwargs)

    def test_an_unknown_provider_names_the_supported_providers(self) -> None:
        with pytest.raises(CompositionRefusal, match=r"provider 'gitea'.*supported providers"):
            preflight_composition(None, self._request(provider="gitea"))

    def test_a_recipe_that_does_not_run_on_the_provider_refuses(self) -> None:
        with pytest.raises(
            CompositionRefusal,
            match=r"recipe 'gitlab-sdk-lane' does not run on provider 'github'",
        ):
            preflight_composition(None, self._request(runtime_recipe="gitlab-sdk-lane"))

    def test_a_harness_that_does_not_run_on_the_recipe_refuses(self) -> None:
        with pytest.raises(
            CompositionRefusal,
            match=r"harness 'codex-sdk-lane' does not run on provider 'gitlab'",
        ):
            preflight_composition(
                None,
                self._request(
                    provider="gitlab",
                    runtime_recipe="gitlab-batch-script",
                    harness="codex-sdk-lane",
                ),
            )

    def test_a_cross_provider_credential_route_refuses(self) -> None:
        """The impossible runtime/harness/credential combination the
        issue names: GitHub's native secret route on the GitLab lane
        (a FORGE_MODEL_<ref> ACTIONS secret is nothing a GitLab
        runner can read)."""
        with pytest.raises(
            CompositionRefusal,
            match=(
                r"credential route 'github-native-secret' is not supported for "
                r"harness 'claude-sdk-lane' on provider 'gitlab'"
            ),
        ):
            preflight_composition(
                None,
                self._request(
                    provider="gitlab",
                    runtime_recipe="gitlab-sdk-lane",
                    harness="claude-sdk-lane",
                    credential_route="github-native-secret",
                ),
            )

    def test_a_required_resume_on_a_restoreless_recipe_names_the_capable_ones(self) -> None:
        """The issue's exemplar refusal: 'driver X on provider Y lacks
        resume support; supported: ...'."""
        with pytest.raises(
            CompositionRefusal,
            match=(
                r"driver 'claude-code' on provider 'gitlab' lacks resume support"
                r".*supported resume-capable"
            ),
        ) as excinfo:
            preflight_composition(
                None,
                self._request(
                    provider="gitlab",
                    runtime_recipe="gitlab-batch-script",
                    harness="claude-code",
                    credential_route="gitlab-protected-variable",
                    resume_mode="required",
                ),
            )
        message = str(excinfo.value)
        assert "github-harness-entry" in message  # the alternatives, named
        assert "gitlab-sdk-lane" in message

    def test_a_fresh_resume_on_a_restoreless_recipe_proceeds(self) -> None:
        row = preflight_composition(
            None,
            self._request(
                provider="gitlab",
                runtime_recipe="gitlab-batch-script",
                harness="claude-code",
                credential_route="gitlab-protected-variable",
                resume_mode="fresh",
            ),
        )
        assert row.resume_supported is False  # the recipe is restoreless; fresh is safe

    def test_composition_refuses_inside_spec_construction(self) -> None:
        """The preflight is wired at the dispatch pre-check seam: an
        impossible combination refuses BEFORE a spec (or a template
        variable) renders."""
        with pytest.raises(CompositionRefusal, match="lacks resume support"):
            compose_execution_spec(
                run_id="run-123",
                execution_attempt_id=_HEX64,
                driver="claude-code",
                provider="gitlab",
                runtime_recipe="gitlab-batch-script",
                credential_mode="gitlab-protected-variable",
                credential_ref="ENV_ANTHROPIC_AUTH_TOKEN",
                resume_mode="required",
                continuation_ref=_HEX64,
                profile_digest=_HEX64_B,
            )


class TestAuthorityMembers:
    """Q39-17 (#336): the two AUTHORITY members ADR-0032's amendment
    adds — the approved-input digest (#321's executor-input identity)
    and the grant id (#320's credential authorization).

    Additive-with-version-note: the schema word stays
    ``forge.execution.spec/1``, the members ride the DOCUMENT (and its
    digest) but not the rendered template set, and empty values are the
    recorded pre-#321/#320 window."""

    _GRANT = "c" * 32  # the broker mints uuid4().hex

    def test_the_members_ride_the_document_and_the_digest(self) -> None:
        base = _spec()
        pinned = _spec(approved_input_digest=_HEX64, grant_id=self._GRANT)
        assert base.approved_input_digest == ""
        assert base.grant_id == ""
        assert pinned.approved_input_digest == _HEX64
        assert pinned.grant_id == self._GRANT
        assert base.to_document()["approved_input_digest"] == ""
        assert pinned.to_document()["grant_id"] == self._GRANT
        # A different authority member is a DIFFERENT spec — the two
        # members are inside the reconciliation axis.
        assert base.spec_digest() != pinned.spec_digest()
        assert _spec(approved_input_digest=_HEX64, grant_id=self._GRANT).spec_digest() == (
            pinned.spec_digest()
        )

    def test_a_malformed_approved_input_digest_refuses(self) -> None:
        with pytest.raises(CompositionRefusal, match="approved_input_digest"):
            _spec(approved_input_digest="digest-of-vibes")

    def test_a_redemption_spec_without_its_grant_id_refuses(self) -> None:
        """The redemption authorization IS the grant: a redemption-mode
        spec without its grant pins an operation the endpoint must
        refuse (grant_absent_*) — the dispatch pre-check refuses first."""
        with pytest.raises(CompositionRefusal, match="'grant_id' is empty"):
            _spec(credential_mode="runner-redemption", grant_id="")

    def test_a_grant_id_that_is_not_one_token_refuses(self) -> None:
        with pytest.raises(CompositionRefusal, match="one token"):
            _spec(credential_mode="runner-redemption", grant_id=f"{self._GRANT} padding")

    def test_a_redemption_spec_pins_its_grant(self) -> None:
        spec = _spec(credential_mode="runner-redemption", grant_id=self._GRANT)
        assert spec.grant_id == self._GRANT
        assert spec.credential_mode == "runner-redemption"

    def test_every_other_route_keeps_the_empty_legacy_window(self) -> None:
        spec = _spec(driver="claude-code", credential_mode="ambient-legacy", credential_ref="")
        assert spec.grant_id == ""

    def test_a_persisted_document_with_the_members_round_trips(self) -> None:
        spec = _spec(approved_input_digest=_HEX64, grant_id=self._GRANT)
        revived = read_execution_spec(spec.to_document())
        assert revived.spec_digest() == spec.spec_digest()
        assert revived.approved_input_digest == _HEX64
        assert revived.grant_id == self._GRANT

    def test_a_pre_amendment_document_without_the_members_round_trips(self) -> None:
        """A document persisted BEFORE the amendment (no such keys)
        loads through the empty legacy window — additive evolution, the
        declared predecessor continues."""
        document = _spec().to_document()
        del document["approved_input_digest"], document["grant_id"]
        revived = read_execution_spec(document)
        assert revived.approved_input_digest == ""
        assert revived.grant_id == ""

    def test_the_rendered_template_set_is_unchanged(self) -> None:
        """The members ride the DOCUMENT (and its digest), never the
        template variables — the shipped templates keep consuming the
        same small pinned set ADR-0032 §2 names."""
        rendered = render_template_variables(_spec(grant_id=self._GRANT))
        assert set(rendered) == {
            "FORGE_RUN_ID",
            "FORGE_DRIVER",
            "FORGE_MODEL",
            "FORGE_LANE_RESUME_MODE",
            "FORGE_RESUME_CHECKPOINT",
            "FORGE_CREDENTIAL_REF",
            "FORGE_CREDENTIAL_REDEEM",
            "FORGE_EXECUTION_SPEC",
            "FORGE_EXECUTION_SPEC_DIGEST",
        }

    def test_the_adr_amendment_names_the_members_and_the_no_bump_rule(self) -> None:
        amendment = (REPO_ROOT / "docs" / "adr" / "0032-versioned-execution-spec.md").read_text()
        assert "approved_input_digest" in amendment
        assert "grant_id" in amendment
        assert "additive-with-version-note" in amendment.lower()
        # The schema word itself did NOT bump.
        assert EXECUTION_SPEC_SCHEMA == "forge.execution.spec/1"


class TestCompatibilityRule:
    """Persisted specs: the declared predecessor continues; anything
    else refuses explicitly (acceptance §2 / negative test §1)."""

    def test_a_persisted_document_round_trips_with_its_digest(self) -> None:
        spec = _spec(resume_mode="required", continuation_ref=_HEX64)
        document = spec.to_document()
        revived = read_execution_spec(document)
        assert revived.spec_digest() == spec.spec_digest()
        assert revived.resume_mode == "required"
        assert revived.continuation_ref == _HEX64
        # The next safe recovery step renders through the CURRENT
        # composition: the revived spec pins the same variables.
        assert render_template_variables(revived) == render_template_variables(spec)

    def test_a_newer_schema_version_refuses_with_the_upgrade_instruction(self) -> None:
        document = _spec().to_document()
        document["schema_version"] = "forge.execution.spec/2"
        with pytest.raises(CompositionRefusal, match=r"supports 'forge.execution.spec/1'"):
            read_execution_spec(document)

    def test_an_unknown_top_level_key_refuses_never_drops(self) -> None:
        document = _spec().to_document()
        document["budget_eur"] = "12"
        with pytest.raises(CompositionRefusal, match="unknown keys \\['budget_eur'\\]"):
            read_execution_spec(document)

    def test_a_non_mapping_refuses(self) -> None:
        with pytest.raises(CompositionRefusal, match="mapping"):
            read_execution_spec("forge.execution.spec/1")  # type: ignore[arg-type]

    def test_a_departed_matrix_row_refuses_explicitly(self) -> None:
        """A persisted spec whose combination left the supported set
        (here: a custom one-off matrix) refuses at the reader — the
        compat rule is continue-the-declared-predecessor-or-refuse,
        never an ambient fallback to a lookalike row."""
        one_off = (
            SupportedComposition(
                provider="github",
                runtime_recipe="experimental-entry",
                harness="claude-sdk-lane",
                credential_route="github-native-secret",
                caller_contract="forge.attempt-start/2",
                template_contract=EXECUTION_SPEC_SCHEMA,
                consumer_contract="forge.candidate-meta/2",
                resume_supported=True,
            ),
        )
        document = compose_execution_spec(
            run_id="run-123",
            execution_attempt_id=_HEX64,
            driver="claude-sdk-lane",
            provider="github",
            runtime_recipe="experimental-entry",
            credential_mode="github-native-secret",
            credential_ref="ENV_ANTHROPIC_AUTH_TOKEN",
            profile_digest=_HEX64_B,
            matrix=one_off,
        ).to_document()
        with pytest.raises(CompositionRefusal, match="does not run on provider 'github'"):
            read_execution_spec(document)


class TestADRAndVocabulary:
    def test_the_adr_exists_and_carries_the_contract_names(self) -> None:
        text = ADR.read_text()
        assert EXECUTION_SPEC_SCHEMA in text
        assert "supported composition matrix" in text
        assert "compatibility rule" in text
        # The ADR cross-references its siblings.
        assert "ADR-0029" in text
        assert "#303" in text

    def test_the_vocabulary_sets_are_the_documented_words(self) -> None:
        assert RESUME_MODE_WORDS == frozenset({"fresh", "required", "restart"})
        assert "runner-redemption" in CREDENTIAL_MODES
        assert "ambient-legacy" in CREDENTIAL_MODES
        assert EXECUTION_SPEC_VERSION == 1

    def test_compose_is_the_only_construction_surface_for_callers(self) -> None:
        """The frozen dataclass stays constructible for the READER's
        re-materialization, but the dispatch-facing surface is the
        composer: every production caller composes, never assembles."""
        signature = inspect.signature(compose_execution_spec)
        assert "provider" in signature.parameters
        assert "runtime_recipe" in signature.parameters
        assert "matrix" in signature.parameters


class TestConsoleEntryPoints:
    """ADR-0032 §5: the wheel owes the console_scripts the v0.37.0
    release lacked — resolved through the installed distribution's
    metadata (the cheap form of the ``uv run forge-doctor --help``
    smoke) and exercised in-process against the module mains."""

    def _forge_scripts(self) -> dict[str, str]:
        from importlib.metadata import entry_points

        scripts: dict[str, str] = {}
        for ep in entry_points(group="console_scripts"):
            if ep.value.startswith("forge.") or ep.name.startswith("forge"):
                scripts[ep.name] = ep.value
        return scripts

    def test_the_wheel_declares_the_doctor_and_dispatcher_scripts(self) -> None:
        scripts = self._forge_scripts()
        assert scripts.get("forge-doctor") == "forge.doctor:main"
        assert scripts.get("forge") == "forge.cli:main"

    def test_the_doctor_entry_point_loads_the_module_main(self) -> None:
        from importlib.metadata import entry_points

        matches = [ep for ep in entry_points(group="console_scripts") if ep.name == "forge-doctor"]
        assert matches, "the forge-doctor console script is not installed"
        loaded = matches[0].load()
        import forge.doctor as doctor

        assert loaded is doctor.main

    def test_the_dispatcher_maps_each_command_to_its_module_main(self) -> None:
        import forge.cli as cli

        assert set(cli.COMMANDS) == {"doctor", "gate", "migrate"}
        assert cli.COMMANDS["doctor"] == ("forge.doctor", "main")
        assert cli.COMMANDS["gate"] == ("forge.release_promotion", "main")
        assert cli.COMMANDS["migrate"] == ("forge.migrate", "main")

    def test_the_dispatcher_usage_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        import forge.cli as cli

        assert cli.main(["--help"]) == 0
        assert "doctor" in capsys.readouterr().out

    def test_an_unknown_command_exits_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        import forge.cli as cli

        assert cli.main(["teleport"]) == 2
        assert "unknown command" in capsys.readouterr().err

    def test_the_doctor_subcommand_reaches_the_module_argparse(self) -> None:
        """`forge doctor --help` ≡ `python -m forge.doctor --help` — the
        module's own argparse answers (exit 0 through SystemExit)."""
        import forge.cli as cli

        with pytest.raises(SystemExit) as excinfo:
            cli.main(["doctor", "--help"])
        assert excinfo.value.code == 0

    def test_the_gate_subcommand_forwards_the_gate_verb(self) -> None:
        """`forge gate` maps onto release_promotion's gate subcommand —
        the forwarded argv starts with the verb, then the module's own
        argparse owns everything else (here: its usage refusal)."""
        import forge.cli as cli

        forwarded: list[list[str]] = []

        def fake_main(argv: list[str] | None = None) -> int:
            forwarded.append(list(argv or []))
            return 0

        original = cli.COMMANDS["gate"]
        try:
            import forge.release_promotion as promotion

            held = promotion.main
            promotion.main = fake_main  # type: ignore[assignment]
            cli.COMMANDS["gate"] = ("forge.release_promotion", "main")
            assert cli.main(["gate", "--version", "v0.38.0"]) == 0
        finally:
            promotion.main = held  # type: ignore[assignment]
            cli.COMMANDS["gate"] = original
        assert forwarded == [["gate", "--version", "v0.38.0"]]
