# Research: GitHub Actions as forge's execution adapter (2026-09-14)

Implementation reference for `src/forge/execution/github_actions.py` and
`ci/templates/forge-harness.github.yml`. Facts from official docs fetched
2026-09-14; each tagged [documented]/[observed]/[inference].

## 1. Launch: workflow_dispatch returns the run id (2026 change)

- `POST /repos/{o}/{r}/actions/workflows/{workflow_id}/dispatches` with
  `{ref, inputs}`; the workflow file must contain `on: workflow_dispatch`
  **on that ref**; `workflow_id` may be the filename. Requires
  `actions:write` (App installation permission). [documented]
- **Changelog 2026-02-19: the dispatch API now returns the run id in the
  response** — the historical "empty 204 + poll-and-guess" correlation is
  obsolete on current GitHub. [documented]
  (https://github.blog/changelog/2026-02-19-workflow-dispatch-api-now-returns-run-ids/)
- Legacy fallback (GHES/older): poll
  `GET /actions/workflows/{id}/runs?event=workflow_dispatch&head_sha=<sha>`
  + created-window; the run-name UUID injection trick was the community
  workaround. [documented]
- Inputs: strings via `inputs.*` context; dispatch inputs total ≤ 10 keys.
  [documented]

## 2. Candidate artifacts (v4)

- Runner uploads via `actions/upload-artifact@v4` → per-job artifact with
  an id; list via `GET /repos/{o}/{r}/actions/runs/{run_id}/artifacts`;
  download via `GET /repos/{o}/{r}/actions/artifacts/{artifact_id}/zip` →
  **302 to a signed URL** (follow redirects). [documented]
- Auth: any token with `actions:read` for the repo — forge's installation
  token works, including cross-run downloads (v4 removed the same-run
  limit that plagued v3). [documented]
- Downloaded artifact is a ZIP containing the uploaded paths
  (`.forge/candidate.diff`, `.forge/candidate.meta.json`). [documented]
- Artifacts are immutable after upload; retention-days controls expiry
  (forge must download before retention or reconcile the run as blocked
  `candidate_expired`). [documented + inference]

## 3. Recursion and App-triggered runs

- Pushes made with `GITHUB_TOKEN` do not trigger new workflow runs — but
  **App installation tokens DO trigger workflows and bypass first-time-
  contributor approval prompts** (research github-api.md §6). Forge's
  publisher (CAS commit) therefore DOES start the target repo's own CI on
  the factory branch — which is exactly what we want for verification —
  but the harness workflow itself must NOT re-trigger on those pushes:
  it only runs `workflow_dispatch`, so it can't. [documented + inference]

## 4. Cancel

- `POST /repos/{o}/{r}/actions/runs/{run_id}/cancel`; a cancelled run's
  completed jobs keep artifacts uploaded before cancellation (upload steps
  marked `if: always()` run on cancel). [documented]

## 5. Agent assignment UX

- The "Assign to Agent" picker is part of **GitHub's Copilot coding-agent
  partner program**: org admins enable partner agents under
  Settings → Copilot → Coding agent; Claude (Anthropic) and Codex (OpenAI)
  are listed as partners. A third-party App becomes assignable only
  through that program — plain App registration is not sufficient.
  [documented]
- Partner-program docs: https://docs.github.com/en/copilot/concepts/agents/about-third-party-coding-agents
- Fallback trigger pattern (used by several agent frameworks): a
  **label trigger** — `issues.labeled` with a reserved label (e.g.
  `forge`) normalized into a run command. [documented pattern + inference]

## 6. Latency/ops notes

- Dispatch → run start is typically seconds but is not SLA'd; poll with
  backoff. Rate limits: 5,000/h per installation (+1,000/h per repo cap
  12,500); secondary limits on concurrent requests. [documented]

## Sources

- https://docs.github.com/en/rest/actions/artifacts
- https://docs.github.com/en/rest/actions/workflows
- https://github.blog/changelog/2026-02-19-workflow-dispatch-api-now-returns-run-ids/
- https://github.blog/news-insights/product-news/get-started-with-v4-of-github-actions-artifacts/
- https://docs.github.com/en/copilot/concepts/agents/about-third-party-coding-agents
- https://github.com/orgs/community/discussions/9752
