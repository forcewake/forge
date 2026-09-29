# Port inventory: today's provider call sites

This is the factual basis for the ports in [DESIGN.md §6.4](DESIGN.md) and
for the adapter tasks P3-16 and P3-19 in [ROADMAP.md](ROADMAP.md). It was
extracted from `main` at `7ed64ba`. Line numbers will drift; re-run the
greps before porting.

## Entry methods per provider service

| Provider | Entry methods (file:line) | Present only here |
| --- | --- | --- |
| GitLab (`runs/service.py`) | `start_run` :1075 (`/implement`, plan gate) · `handle_command_note` :5185 (`/go`, `/fix`, `/ask` routing) · `handle_cancel_note` :1832 · `handle_retry_note` :2137 · `handle_status_note` :2529 · `handle_why_blocked_note` :2607 · `handle_reconcile_note` :2670 · `handle_review_feedback_note` :2753 · `handle_issue_edited` :4856 · `handle_label_removed` :4980 · reconciler passes `evaluate_review_corrections` :3097, `evaluate_review_rounds` :3809, `evaluate_waiting_ci` :7229 / `_evaluate_one` :7320, `evaluate_revival` :8652, `evaluate_publication_intents` :8907, `evaluate_waiting_harness` :9188, `evaluate_ready_evidence` :9724, `evaluate_accepted` :9805 | Mature `/fix` and `/ask` review rounds (:2753–4821). Approved input via `resolve_approved_input` (:6089, :8004). |
| GitHub (`runs/github_service.py`) | `start_run` :541 · `handle_go` :1014 · `handle_cancel` :1208 · `handle_retry` :1338 · `handle_status` :2032 · `handle_why_blocked` :2082 · `handle_reconcile` :2144 · `handle_issue_edited` :2881 · `handle_label_removed` :3041 · revival and recovery :2015, :2220 · `resolve_publication_intents` :2675 · `evaluate_waiting_harness` :4621 · `evaluate_waiting_ci_one` :5584 · `resume_verification` :5960 · `continue_review_only` :6173 · review folded into `_review_and_ready` :6405 | Executor input via the frozen `AttemptStartSpec` envelope (`composition_adoption`, :3788–4100). Question recovery (:2449–2638). |
| Azure (`runs/azure_service.py`, frozen) | `start_run` :677 · `handle_go` :1073 · `handle_cancel` :1294 · `handle_retry` :1428 · `handle_status` :1644 · `handle_why_blocked` :1695 · `handle_reconcile` :1758 · `handle_issue_edited` :1839 · `handle_label_removed` :1967 · `evaluate_revival_recovery` :1629 · `evaluate_config_recovery` :2214 · `resolve_publication_intents` :2419 · `evaluate_waiting_harness_one` :3600 · `evaluate_waiting_ci_one` :4019 | No review rounds, no question recovery, no review-only continuation |

## Operations grouped by port

| Port · operation | GitLab | GitHub | Azure (for the post-1.0 adapter) |
| --- | --- | --- | --- |
| **Subject** · read issue or work item | `service.py:5416,10176,10270` `GitLabClient.get_issue` | `github_service.py:7670–7714` `get_issue` | `azure_service.py:1880` `get_work_item` |
| **Subject** · post or update comment | `service.py:5384,10576` `create_issue_note` | `github_service.py:3238,4789,7403` `create_issue_comment`, `update_issue_comment` | `azure_service.py:4976` `add_work_item_comment` |
| **Subject** · labels and state | `service.py:4980–5030` | `github_service.py:2881–3041` | `azure_service.py:1839–1967` |
| **Repo** · head, tree, blob at sha | `service.py:4691,7395,7770,8215,10493` `get_branch_head`, repository reader | `github_service.py:3589,4533,6664,7281` `get_branch_head`, `GithubReader` | `azure_service.py:558–592,2338,3224,3365` `get_tree`, `get_commit`, `get_item`, `get_branch_head` |
| **Repo** · ensure branch | `service.py:5711,6618,9550` `ChangesetWriter.ensure_branch` | `github_service.py:4533` `create_branch` | `azure_service.py:3226,3367` `create_branch_from` |
| **Repo** · commit with CAS | `service.py:5711–5800,9550–9640` `ChangesetWriter`, Commits API | `github_service.py:3589–3650,5031–5402` GraphQL with `expectedHeadOid` | `azure_service.py:3224–3330` `push_commits` with `oldObjectId` |
| **Repo** · marker lookup and settling | `service.py:4020,8989–8990,10989` `list_commits` plus settling probes | `github_service.py:3357–3360` `list_commits` | `azure_service.py:2341,3257,3349` `list_commits` |
| **Review** · find or create draft | `service.py:6742–7194` `get_merge_request`, `list_merge_requests`, `create_merge_request`, `update_merge_request` | `github_service.py:3220,6655` `get_pr_by_head`, `create_pull_request` (draft), `close_pull_request` | `azure_service.py:3385–3395` `find_draft_pr_by_head`, `create_draft_pr` |
| **Review** · head, notes, discussions | `service.py:3391,4420,4671–4713,9841` `get_merge_request`, `create_mr_note`, `list_discussions` | `github_service.py:6405–6650` (review and ready) | `azure_service.py:3369–3407,5461` |
| **CI** · dispatch lane | `service.py:382–430,5873–6250` `create_pipeline` (variables plus HMAC lane-control token) | `github_service.py:3788–4458` `dispatch_workflow` (inputs plus `AttemptStartSpec`) | `azure_service.py:3044` `run_pipeline` (string `template_parameters`) |
| **CI** · status and checks | `service.py:7395–7440,10967–11035` `list_pipelines`, `get_pipeline`, `list_pipeline_jobs` | `github_service.py:5584–5670` `list_workflow_runs_for_sha`, `get_workflow_run`, `list_workflow_dispatch_runs` | `azure_service.py:4019–4277,5059–5069` `list_builds_by_repository`, `get_run` |
| **CI** · artifacts and logs | `service.py:8611` `get_job_log`; `backends.py:540,577` `get_job_artifacts_file` | `github_service.py:5584–5960` | `azure_service.py:4019–4277` |
| **Actor** · permissions and approvers | `service.py:1075–1260` (admission) | `github_service.py:541–645`, `handle_go` :1014–1208 (installation and actor permissions) | `azure_service.py:401–410` (PAT identity) |

## Shared algorithm of the duplicated flows

These flows follow one provider-neutral algorithm; only the port calls
differ.

- **`_advance_harness`** (`service.py:5873`, `github_service.py:3788`,
  `azure_service.py:2818`):
  1. reserve capacity and enforce the budget;
  2. load and digest-verify the spec;
  3. resolve the attempt base, generation, resume mode, credentials and
     approved brief;
  4. ensure the factory branch;
  5. journal the native-start intent;
  6. dispatch;
  7. correlate the native handle;
  8. move to `waiting_harness`.
- **Plan and publish:** freeze the sha → read `.forge` config → reserve
  budget → planner → freeze and digest the RunSpec → plan comment →
  approval (`service.py:1261–1640`, `github_service.py:646–1013`,
  `azure_service.py:825–1072`).
- **`/go`:** resolve the run → approver and admission → digests and gate
  → reserve → dispatch (`service.py:5185–5348`,
  `github_service.py:1014–1208`, `azure_service.py:1073–1227`).
- **Publication:** observe the artifact → validate the metadata → CAS
  publish → find or create the draft (`service.py:6618–7194`,
  `github_service.py:5031–5402`, `azure_service.py:3220–3407`).
- **CI evaluation:** native checks → shared verdict → repair only code
  failures → review (`service.py:7229–7547`,
  `github_service.py:5584–5960`, `azure_service.py:4019–4349`).
- **Review and ready:** re-read the head → read-only review → post →
  `ready_for_human` (`service.py:8035–8505`,
  `github_service.py:6405–6650`, `azure_service.py:3407–3571`).

**Estimate.** Provider-neutral orchestration that can move to the engine:

| Service | Movable | Total |
| --- | --- | --- |
| GitLab | about 6,000–7,000 lines | 11,433 |
| GitHub | about 4,800–5,500 lines | 8,350 |
| Azure | about 3,300–3,800 lines | 6,110 |

The rest is genuinely provider-specific.

## Semantics that must stay inside the adapters

- GitLab: Commits API settling and negative probes (`service.py:4020–4101`)
  versus GitHub GraphQL `expectedHeadOid` (`github_service.py:3589–3650`)
  versus Azure `oldObjectId` (`azure_service.py:3257–3330`).
- Checks: GitLab pipelines and jobs, GitHub workflow runs identified by
  path (`github_service.py:5584–5750`) and Azure builds each need their
  own normalization and provenance.
- Azure template parameters are strings and must never carry secrets
  (`azure_service.py:2818–3044`).
- Lane authentication: today GitLab derives an HMAC lane-control token and
  Azure has no equivalent. Lane v1 replaces both with OIDC.
- GitHub App installation tokens and actor permissions remain distinct
  from GitLab tokens and Azure PATs.
