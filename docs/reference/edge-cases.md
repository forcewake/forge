# Edge-case register

Hard-won gotchas, each pinned by tests or a reference. Before a module is
deleted or rewritten (ROADMAP Phase 1 and Phase 3), its entries here must
still hold in the new code; the **Code** column says where each rule lives.
`tests/test_edge_case_register.py` fails when a pin disappears.

When a pin breaks:

- port the pinning test to the new code and repoint the row in the same PR;
  the new pin must still fail when the rule is broken;
- when a rule no longer applies (for example an Azure-only rule after
  P1-11), move the row to **Retired** as a bullet with the reason;
- never reuse an ID.

## Code-level entries (each pinned by tests)

| ID | Symptom | Rule | Code | Pinning tests |
| --- | --- | --- | --- | --- |
| EC-01 | GitLab CE 18.x Commits API returns 400 when `start_branch` names an existing branch | `ensure_branch` owns branch creation; commits never pass `start_branch` | `src/forge/repository/writer.py` | `tests/test_repository_writer.py::test_no_start_branch_when_branch_pre_exists`, `tests/test_repository_writer.py::test_no_start_branch_on_fresh_branch` |
| EC-02 | npm `optionalDependencies` flake leaves a harness CLI in a silent futex hang | Install the platform binary explicitly | `src/forge/harnesses/scripts/` | `tests/test_harness_entry.py::TestRenderGrokBuild::test_script_installs_the_platform_binary_explicitly` |
| EC-03 | The platform binary drifts from the pinned wrapper version | Pin the platform binary to the wrapper's version | `src/forge/harnesses/scripts/` | `tests/test_harness_entry.py::TestDriverVersionPins::test_grok_platform_binary_follows_the_pinned_wrapper` |
| EC-04 | Two concurrent publishers create two Draft MRs | Create idempotently by intent; the database arbitrates uniqueness | `src/forge/runs/service.py` | `tests/production_entry/test_concurrent_publication.py::TestConcurrentDraftMrCreators::test_two_direct_concurrent_creators_collapse_to_one_mr` |
| EC-05 | forge's own notes contain command lines and could trigger forge; a GitHub App's login (`name[bot]`) differs from `FORGE_BOT_USERNAME` | Bot-authored events are skipped on every provider | `src/forge/gateway/validator.py`, `src/forge/gateway/github_webhook.py` | `tests/test_bot_loop.py::test_webhook_skips_bot_event`, `tests/test_github_webhook.py::TestGitHubBotLoginLoopGuard::test_bot_login_comment_does_not_create_a_run` |
| EC-06 | GitHub workflow display names are not identities | Resolve the harness workflow by path | `src/forge/runs/github_service.py` | `tests/test_github_runs.py::TestPositiveVerification::test_harness_lane_is_excluded_by_path_not_display_name` |
| EC-07 | GitLab verification pipelines (`merge_request_event`) look like lane activity | Exclude them from coding occupancy | `src/forge/runs/service.py` | `tests/test_adaptive_lease_occupancy.py::TestBranchSearchCorrelation::test_verification_pipeline_is_not_coding_occupancy` |
| EC-08 | A lost publication response tempts a second write | Resolve the unknown outcome by marker-and-parent lookup; never write twice | `src/forge/repository/writer.py`, `src/forge/durable/intents.py` | `tests/test_publication_intents.py::TestGitLabWriterIntents::test_lost_response_is_adopted_by_a_new_process`, `tests/test_publication_intents.py::TestProbeDecisionTable::test_exactly_one_marker_and_parent_match_adopts` |
| EC-09 | A human commit lands on the factory branch during a review round | A foreign tip is a typed conflict; the human commit is preserved, never reverted | `src/forge/runs/service.py` | `tests/test_review_rounds.py::TestRoundOwnEffectRecovery::test_a_human_commit_after_the_candidate_is_a_typed_conflict_preserved` |
| EC-10 | A required resume over an unusable checkpoint could start a model session on stale work | Halt before any vendor session | `src/forge/lane_driver.py` | `tests/test_application_regressions.py::TestR32ARestoredGenerationIsTheExecutionWorkspace::test_a_required_resume_that_cannot_land_halts_before_any_vendor_session` |
| EC-11 | GitHub silently drops an unknown query parameter: `head_branch=` widened the workflow-runs filter, so concurrent dispatches looked correlated (ADR-0028) | Filter runs with `branch=`, and re-check branch equality client-side | `src/forge/integrations/github.py` | `tests/test_github_client.py::TestActionsEndpoints::test_list_workflow_dispatch_runs_filters_and_windows`, `tests/test_github_actions_executor.py::TestLaunch::test_concurrent_batch_sharing_one_attempt_base_correlates_own_branch` |

| EC-12 | An unreadable or invalid project config could widen an /implement run to the default scope | Only confirmed absence earns defaults; unreadable and invalid reads block before paid calls, and post-freeze paths use the frozen scope | `src/forge/project_config.py` | `tests/test_config_scope.py::TestConfigGateStartPath::test_403_parks_config_unreadable_before_any_paid_call`, `tests/test_config_scope.py::TestPermissionsNeverWiden::test_frozen_scope_survives_an_unreadable_config` |
| EC-13 | Two repositories in one Azure project or two provider connections could share a cached config | Cache by canonical provider, connection, repository, ref and config path; never use a bare project id or private adapter attributes | `src/forge/project_config.py` | `tests/test_project_config.py::TestRepositoryIdentityContractFND01::test_real_reader_identities_key_the_authority_cache`, `tests/test_project_config.py::TestCacheIdentityD01::test_two_refs_of_one_repository_are_different_snapshots` |
| EC-14 | A malformed scope or neighbor authorization could be silently treated as unrestricted or absent | Reject non-mapping config sections, malformed implement paths and malformed neighbor entries; preserve the legacy review wrapper's separate tolerant policy | `src/forge/project_config.py` | `tests/test_project_config.py::TestStrictPolicySchemaD02::test_a_string_paths_value_is_invalid_not_unrestricted`, `tests/test_project_config_location.py::test_malformed_neighbor_authorization_is_rejected`, `tests/test_config_scope.py::TestReadProjectConfig::test_legacy_wrapper_still_degrades_to_defaults` |

| EC-40 | An undeclared importer bypasses an authority owner | Only the exact TOML-declared importer modules may import each protected owner | `pyproject.toml` | `tests/test_boundary_contract_translation.py::test_every_surviving_authority_has_an_import_contract` |
| EC-41 | A new dispatch path reaches the retired checkpoint lookup | The legacy chain is confined to its explicit modules and the opt-in revival adapter | `src/forge/runs/revival.py` | `tests/test_architecture_boundaries.py::TestIntentionalViolationTraps::test_the_legacy_chain_inside_revival_outside_the_adapter_is_caught` |
| EC-42 | A new caller composes its own checkpoint authority | Only the enumerated composition sites may call resolve_repository | `src/forge/adaptive/checkpoint_repository.py` | `tests/test_architecture_boundaries.py::TestIntentionalViolationTraps::test_an_unregistered_resolve_repository_call_is_caught` |
| EC-43 | A service hand-builds executor input beside the approved composition seam | AttemptStartSpec construction remains confined and dispatch uses the composition adapter | `src/forge/runs/composition.py` | `tests/test_architecture_boundaries.py::TestIntentionalViolationTraps::test_a_service_hand_building_an_attempt_start_spec_is_caught` |
| EC-44 | A blob unlink races a final reference scan | CAS blobs are unlinked only inside the locked sweep entries | `src/forge/adaptive/checkpoint_repository.py` | `tests/test_architecture_boundaries.py::TestIntentionalViolationTraps::test_a_blob_unlink_outside_the_sweep_entries_is_caught` |
| EC-45 | A dispatch independently re-derives a continuation mode | Only the mode authority constructs modes; dispatch passes the selected mode through | `src/forge/adaptive/continuation.py` | `tests/test_architecture_boundaries.py::TestIntentionalViolationTraps::test_a_mode_rederivation_is_caught` |
| EC-46 | A third applicant applies a budget amendment beside its durable authority | Only the owner and declared provider continuation routes construct or apply amendments | `src/forge/durable/budgets.py` | `tests/test_architecture_boundaries.py::TestIntentionalViolationTraps::test_a_third_module_applying_a_budget_amendment_is_caught` |
| EC-47 | A credential registry lookup is mistaken for operation authorization | The redemption endpoint must load the persisted operation grant before credential lookup | `src/forge/api_lane_control.py` | `tests/test_architecture_boundaries.py::TestIntentionalViolationTraps::test_a_registry_lookup_authorization_without_the_grant_load_is_caught` |
| EC-48 | A resumed dispatch sends a superseded plan summary | Every dispatch resolves ApprovedInput and sends the approved brief bytes | `src/forge/runs/service.py` | `tests/test_architecture_boundaries.py::TestIntentionalViolationTraps::test_a_spec_plan_summary_brief_in_the_dispatch_is_caught` |
| EC-49 | Direct legacy secret-name derivation bypasses locator collision checks | Native locator allocation stays in the broker | `src/forge/adaptive/credential_broker.py` | `tests/test_architecture_boundaries.py::TestIntentionalViolationTraps::test_a_direct_legacy_carrier_derivation_is_caught` |
| EC-50 | A service invents a second factory-branch derivation | Factory branch derivation stays at its declared authority | `src/forge/runs/service.py` | `tests/test_architecture_boundaries.py::TestIntentionalViolationTraps::test_a_new_factory_branch_shortcut_in_the_service_is_caught` |
| EC-51 | A cache-only probe drops a durable command | Gateways use the durable dedup owner; legacy SET-NX stays confined to the legacy event lane | `src/forge/gateway/durable_ingress.py` | `tests/test_architecture_boundaries.py::TestIntentionalViolationTraps::test_the_setnx_probe_in_front_of_a_durable_command_is_caught` |
| EC-52 | A provider admits a review-round child outside the atomic budget transaction | The round-admission owner composes child, budget and slot operations in order | `src/forge/runs/round_admission.py` | `tests/test_architecture_boundaries.py::TestIntentionalViolationTraps::test_an_owner_that_stops_composing_the_transaction_is_caught` |
| EC-53 | An outcome projection silently classifies an unknown journal word | Authority refusal statuses and journal vocabulary must match projection classification | `src/forge/adaptive/operator_view.py` | `tests/test_architecture_boundaries.py::TestIntentionalViolationTraps::test_a_journaled_outcome_word_the_mapping_dropped_is_caught` |
| EC-54 | An unwired provider resume path is presented as feature parity | Test provider behavior and preserve explicit unsupported-provider gaps | `src/forge/runs/azure_service.py` | `tests/test_architecture_boundaries.py::TestProviderConformance::test_the_gitlab_lane_resume_seam_and_the_azure_gap_are_the_documented_state` |
| EC-55 | Production startup silently selects a reference or qualification fixture | A clean interpreter importing startup loads no fixture or reference implementation | `src/forge/main.py` | `tests/test_architecture_boundaries.py::TestSyntheticFixtureIsolation::test_default_production_startup_loads_no_fixture_or_reference_module` |
| EC-56 | A stale execution generation is accepted by a second epoch oracle | Epoch authority is unique and envelopes reject invalid epochs | `src/forge/adaptive/continuation.py` | `tests/test_architecture_boundaries.py::TestStaleEpochConformance::test_the_envelope_refuses_a_bad_epoch_and_encodes_a_good_one` |

## Operational entries (the knowledge lives in the row)

| ID | Symptom | Rule | Reference |
| --- | --- | --- | --- |
| OP-01 | Claude Code on z.ai treats a bare model id as a 200K context window | Use the `[1m]` suffix for the 1M window; the default `FORGE_HARNESS_MODEL` carries it | `src/forge/config.py` |
| OP-02 | Redis `forge:worker:<id>` heartbeat keys outlive a dead worker by about 30 seconds | Check worker logs, not only `/metrics` | `AGENTS.md` |
| OP-03 | podman reports a 12-hex short image id; `inspect` reports the 64-hex id | Drop `sha256:` and compare by prefix, never with `!=` | `scripts/run_review_loop_qualification.py` |
| OP-04 | zsh does not word-split unquoted variables | Iterate with `while IFS= read -r` | `AGENTS.md` |
| OP-05 | `pytest` piped to `tail` hides the exit code | Run `set -o pipefail` first | `AGENTS.md` |
| OP-06 | Long-lived AI streams from some runner networks crawl (226 s instead of 2 s) | Route them through `FORGE_HARNESS_HTTPS_PROXY` | `ci/templates/grok.gitlab-ci.yml` |
| OP-07 | The dead-letter queue `forge:tasks:dead` looks like stuck runs | It holds exhausted tasks; read run outcomes from Postgres `flow_runs` | `AGENTS.md` |

## Retired

None yet.
