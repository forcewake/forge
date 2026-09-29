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
