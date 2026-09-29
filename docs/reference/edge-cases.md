# Edge-case register

Hard-won gotchas, each pinned by a test or a runbook reference. Before a
module is deleted or rewritten (ROADMAP Phase 1 and Phase 3), its entries
here must still hold in the new code. `tests/test_edge_case_register.py`
fails when a pinning test disappears.

## Code-level entries (each pinned by a test)

| ID | Symptom | Rule | Pinning test |
| --- | --- | --- | --- |
| EC-01 | GitLab CE 18.x Commits API returns 400 when `start_branch` names an existing branch | `ensure_branch` owns branch creation; commits never pass `start_branch` | `tests/test_repository_writer.py::test_no_start_branch_when_branch_pre_exists` |
| EC-02 | npm `optionalDependencies` flake leaves a harness CLI in a silent futex hang | Install the platform binary explicitly | `tests/test_harness_entry.py::TestRenderGrokBuild::test_script_installs_the_platform_binary_explicitly` |
| EC-03 | The platform binary drifts from the pinned wrapper version | Pin the platform binary to the wrapper's version | `tests/test_harness_entry.py::TestDriverVersionPins::test_grok_platform_binary_follows_the_pinned_wrapper` |
| EC-04 | Two concurrent publishers create two Draft MRs | Create idempotently by intent; the database arbitrates uniqueness | `tests/production_entry/test_concurrent_publication.py::TestConcurrentDraftMrCreators::test_two_direct_concurrent_creators_collapse_to_one_mr` |
| EC-05 | forge's own notes contain command lines and could trigger forge | Bot-authored events are skipped | `tests/test_bot_loop.py::test_webhook_skips_bot_event` |
| EC-06 | GitHub workflow display names are not identities | Resolve the harness workflow by path | `tests/test_github_runs.py::TestPositiveVerification::test_harness_lane_is_excluded_by_path_not_display_name` |
| EC-07 | GitLab verification pipelines (`merge_request_event`) look like lane activity | Exclude them from coding occupancy | `tests/test_adaptive_lease_occupancy.py::TestBranchSearchCorrelation::test_verification_pipeline_is_not_coding_occupancy` |
| EC-08 | A lost publication response tempts a second write | Resolve the unknown outcome by marker-and-parent lookup | `tests/test_publication_intents.py::TestProbeDecisionTable::test_exactly_one_marker_and_parent_match_adopts` |
| EC-09 | A human commit lands on the factory branch during a review round | A foreign tip is a typed conflict and is never reverted | `tests/test_round_head_classification.py::TestForeignEffect::test_a_foreign_tip_above_the_own_effect_is_foreign` |
| EC-10 | A required resume over an unusable checkpoint could start a model session on stale work | Halt before any vendor session | `tests/test_application_regressions.py::TestR32ARestoredGenerationIsTheExecutionWorkspace::test_a_required_resume_that_cannot_land_halts_before_any_vendor_session` |

## Operational entries (no code path; follow the reference)

| ID | Symptom | Rule | Reference |
| --- | --- | --- | --- |
| OP-01 | Claude Code on z.ai treats a bare model id as a 200K context window | Use the `[1m]` suffix for the 1M window | `docs/harnesses/claude-code.md` |
| OP-02 | Redis `forge:worker:<id>` heartbeat keys outlive a dead worker by about 30 seconds | Check worker logs, not only `/metrics` | `AGENTS.md` |
| OP-03 | podman reports a 12-hex short image id; `inspect` reports the 64-hex id | Normalize ids before comparing them | `scripts/align_lab.py` |
| OP-04 | zsh does not word-split unquoted variables | Iterate with `while IFS= read -r` | `AGENTS.md` |
| OP-05 | `pytest` piped to `tail` hides the exit code | Run `set -o pipefail` first | `AGENTS.md` |
