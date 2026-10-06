<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Design: Scheduled AI Review of Pull Requests

Status: **implemented, in dry-run rollout** (§15). The workflow,
scripts and offline tests exist. The schedule runs with
`dry_run: true` until the rollout steps clear live operation, at
which point one pull request flips the expression in
`code-review-cron.yaml`.

This document reuses the architecture, vocabulary and lessons of
[`github-issues-triage`](https://github.com/lfreleng-actions/github-issues-triage)
and
[`github-code-monkey`](https://github.com/lfreleng-actions/github-code-monkey).
Where this design departs from those, the text says why. The
operator's guide lives in [`setup/README.md`](setup/README.md).

## 1. Problem Statement

The `lfreleng-actions` organisation receives a steady flow of small
pull requests from its own members: version pins, documentation,
lockfile refreshes, one-line workflow fixes. Each needs an approving
review before the branch ruleset lets it merge, and the approval
costs a RelEng member a context switch that the change itself does
not warrant. Reviews queue, authors wait, and the backlog of pull
requests that *do* need a careful read grows harder to see among
those that do not.

Thanh Ha ran a manual procedure to thin this queue: a script on his
laptop, under his own identity, that listed open pull requests and
filed a "Review needed" issue in a personal repository for each one
that looked ready. The procedure proved the idea and exposed its
limits. It depended on one person's laptop and credentials; it
produced notification issues nobody else watched; and it had no
memory beyond those issues, so the same pull request surfaced on
every pass.

## 2. Goal

Review open, human-authored, non-draft pull requests across the
organisation with a Copilot CLI agent every three hours on weekdays
and twice a day at weekends. Approve the ones a deterministic check
confirms as trivial and the ones a full review finds low risk.
Report everything else to a human through the run report, and write
nothing to a pull request other than that one approval.

Each approval:

- comes from the **LF/RelEng Code Review Bot** App identity, not
  from a person, and says so in its body;
- counts towards the required review under the organisation's
  branch ruleset, which is the intended effect;
- names the model, the run and the summary the agent wrote, with
  every `@` encoded so the body can never mention anyone;
- lands on the head commit the agent reviewed and no other, after
  a trusted job has re-read the pull request and found it unchanged.

The measure of success is a queue in which every open pull request
either carries a bot approval a maintainer can merge on sight, or
appears in the run report with the agent's findings for a human to
weigh.

### Non-goals

- **Feedback on the pull request.** The workflow never comments,
  never requests changes and never dismisses a review. A
  `needs-human` verdict reaches people through the run report and
  nowhere else.
- **Bot-authored pull requests.** Dependabot, pre-commit.ci and Code
  Monkey pull requests stay out. `dependamerge` and human review
  handle them, and an App approving another App's work would defeat
  the ruleset's purpose.
- **Notification issues.** The manual procedure's "Review needed"
  issues have no successor. The ledger (§6) replaces them as memory; a
  supplemental channel is a later capability (§16).
- **Other engines.** Copilot CLI is the single harness, as in Code
  Monkey.
- **Private repositories.** The estate is public; the review job
  checks out the head with no credential and relies on that.

## 3. Relationship to Issues Triage and Code Monkey

Inherited without change: the trusted / untrusted / trusted job
layout; artifact retrieval by producer ID and trusted SHA-256
digest, never by name alone; a per-invocation namespace of
`run-attempt-uuid`; the `github_pat_` guard on the model token;
harden-runner with the organisation allow-list in block mode for
trusted jobs; bounded artifact extraction before any read;
scratch cleanup by plain deletion under `continue-on-error`.

Shared credentials: this workflow reuses the triage App rather than
adding a third one (§10.1). The App gains pull request, checks and
commit status permissions; its issue permissions stay for triage.

Shared code: the organisation's `bots-template` repository holds
the pattern every bot starts from, and five of this repository's
modules (`bot_github.py`, `bot_evidence.py`, `artifact_fetch.py`,
`preflight.py`, `ledger.py`) are verbatim copies of its (§14).

Three departures:

- **The untrusted job produces a verdict, not labels or commits.**
  Triage's agent emits label proposals; Code Monkey's emits a git
  bundle. This agent emits one JSON block naming a tier. The trusted
  apply job corroborates the tier against the diff itself before it
  acts, because a tier is a claim and the diff is evidence.
- **A matrix of pull requests, each with its own apply job.** Triage
  applies one packet; this workflow runs one review session and one
  apply job per pull request, so a session that fails or runs long
  costs its own entry alone.
- **Run-to-run memory in an artifact.** Neither sibling needs to
  remember what it did: triage's labels and Code Monkey's branches
  live on GitHub. An assessment that ends in `needs-human` leaves no
  trace on the pull request, so this workflow carries a ledger
  forward between runs (§6).

## 4. Architecture

```text
select (trusted)        review (untrusted, matrix)   apply (trusted, matrix)
  App read token          model PAT, no App            verify evidence
  prior ledger            checkout head, no creds      bounded session fetch
  scan + filter           Copilot CLI session          offline check + vetoes
  selection.json          one JSON verdict block       live re-read, APPROVE
  ID + digests ------->   session artifact --------->  result.json
                                                            |
                                            report (trusted) <--+
                                              report.md/json, ledger
```

The **select** job holds the App key, mints a read token,
fetches the prior ledger, enumerates and filters pull requests, and
publishes `selection.json` plus a matrix. Its artifact ID and the
SHA-256 digests of `selection.json` and `ledger.json` become job
outputs, which is how every later job knows it reads the bytes the
trusted job wrote.

The **review** job runs once per matrix entry. It verifies the
evidence, checks out the pull request's head commit without
credentials, writes the per-pull-request packet and the prompt, and
runs Copilot CLI with shell tools that read. It holds the model
credential and nothing else. Its output is a session artifact: the
share summary ending in a verdict block, the usage file, the prompt
and the CLI logs.

The **apply** job runs once per matrix entry after the review job
settles, including after a failed entry. It verifies the evidence
again, fetches the session through a bounded extractor, runs the
offline check, and on the live path mints a `pull-requests: write`
token for that one repository and submits the approval. Every path
ends in a typed `result.json`.

The **report** job gathers every result, renders the step summary,
writes `report.json` and `report.md`, and publishes the new ledger.

### 4.1 Why tool policy cannot contain the review job

The Copilot CLI invocation allows `cat`, `jq`, `grep`, `head`,
`tail`, `wc` and `ls`, and denies `write`, `gh` and `git`. That
list states intent: the CLI auto-approves what it classifies as a
read, and a prompt-injected pull request could coax a bypass out of
any shell. `awk`, `sed` and `find` stay out of the allow list for
that reason: each can write or run commands.

Containment comes from what the job lacks. It has no GitHub
credential: `COPILOT_GITHUB_TOKEN` is a fine-grained PAT with Copilot
Requests and no repository grants, and the job-native token carries
`contents: read` alone, with `persist-credentials: false` on every
checkout. Whatever the session does, it cannot approve, comment,
push or read a private repository. Its sole product is text, and
the apply job treats that text as hostile input: bounded on fetch,
parsed into a validated shape, cross-checked against the trusted
selection, corroborated against the diff, and then re-verified
against the live pull request before the one write.

### 4.2 What the design trusts the caller with

`assets_repository` and `assets_ref` choose the prompt and scripts.
The select job resolves the ref to a commit once and every later job
checks out that SHA, so a moving branch cannot swap code between
jobs. Callers run reviewed assets with secrets or none at all: the
testing workflow's pull request legs check out the pull request's
own head as assets and pass no secret for this reason.

`egress_allow_config` names the organisation allow-list coordinate
for `harden-runner-block-action`; the bundled callers pin it to a
commit. `github_app_client_id` and the two secrets reach trusted
jobs alone, by construction of the reusable workflow: no step in the
review job references them.

### 4.3 What the design does not trust the caller with

The `owner` of every token mint is `inputs.org`, and the
`repositories` of the one write mint is a single matrix repository
name recorded by the trusted select job. No mint takes its owner or
scope from a pull request, an issue, agent output or a config file.
The contract tests pin both, and the pre-flight gate re-runs those
tests before any mint.

### 4.4 The pre-flight gate

The contract tests in `tests/test_workflow.py` run when a pull
request changes the workflow. A scheduled run executes whatever is on
the default branch, and nothing in that path re-checks the boundary
before the first App token mint: a drift merged through any route
the tests do not cover, a wrong secret wired to the right name, or a
broader App's key in this workflow would all run. The gate
closes that: `scripts/preflight.py` runs from the pinned assets
checkout, before any `create-github-app-token` step, and fails the
run closed.

<!-- markdownlint-disable MD013 -->

| Stage                | Check                                                                                            | Drift it refuses                                                                                                                                                                                               |
| -------------------- | ------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Before the read mint | Re-run `tests/test_workflow.py` against the checked-out workflow files                           | An App key or mint in the review job, an unpinned action, a write mint no longer gated on `!dry_run` and the check outcome, a download by name where the design demands an ID, `persist-credentials` turned on |
| Before the read mint | `zizmor --persona auditor` on the checked-out workflows: zero findings                           | Expression injection, `pull_request_target`, cache poisoning, anything the contract tests do not name                                                                                                          |
| Before the read mint | `config/bot.json` is a small object naming one lower-case slug                                   | A config edit that points identity checks at a different App                                                                                                                                                   |
| Before the read mint | Credential shapes: client id pattern, PEM markers and plausible key length, `github_pat_` prefix | The wrong secret under the right name; values are never printed                                                                                                                                                |
| Before the read mint | Block mode has a commit-pinned allow-list coordinate and a loaded list                           | An allow-list absent without an error, or pinned to a moving branch                                                                                                                                            |
| Before the read mint | A live run's `assets_sha` equals `job.workflow_sha`                                              | A reviewed workflow executing unreviewed scripts through `assets_ref`                                                                                                                                          |
| After the read mint  | The `app-slug` the mint returns equals `config/bot.json`                                         | Another App's private key wired into this workflow                                                                                                                                                             |
| After the read mint  | `GET /repos/{this}` with the token shows no `push`, `maintain` or `admin`                        | A read mint that obtained more than it asked for                                                                                                                                                               |
| Before the write     | The write mint's `app-slug` equals `config/bot.json`                                             | The same, on the one token that can approve                                                                                                                                                                    |

<!-- markdownlint-enable MD013 -->

The gate costs about half a minute per run: a `uv` setup and a
`zizmor` install from PyPI, which the allow-list already carries.
Everything it checks is a file already on the runner or a value the
workflow already holds; it introduces nothing new to trust. A run it
fails is a run that must not proceed, and the error annotation names
the check without quoting what the check read.

The gate is not where policy lives. It verifies that the
workflow still has the shape the design and tests describe; the
design and tests remain the place to change that shape.

## 5. Selection

The select job runs `select_pulls.py` with the App read token, or
the job-native token when the caller supplied no client id. Named pull
requests (`pull_requests` input) replace the scan; otherwise
`gh search prs --owner ORG --state open --draft=false` with an
optional `--repo` restriction enumerates candidates, and a result
set at the 1,000 ceiling fails the run rather than reviewing a
subset without saying so.

Each candidate passes the skip rules in this order. The first rule
that catches a pull request names its reason, and `selection.json`
records a count for every reason, zero included:

<!-- markdownlint-disable MD013 -->

| Reason              | Meaning                                                                                           |
| ------------------- | ------------------------------------------------------------------------------------------------- |
| `repository`        | Excluded by `config/excluded-repos.txt` or `exclude_repos`, archived, or outside the organisation |
| `bot_author`        | Author is a Bot or App: `is_bot`, typename `Bot`, login ending `[bot]`, or no author at all       |
| `closed`            | Not `OPEN` at read time                                                                           |
| `draft`             | `isDraft`                                                                                         |
| `conflicting`       | `mergeable == CONFLICTING`; `UNKNOWN` does not skip                                               |
| `changes_requested` | Some reviewer's latest review is `CHANGES_REQUESTED`                                              |
| `already_approved`  | Some reviewer's latest review is `APPROVED` on the current head                                   |
| `copilot_pending`   | Copilot is a requested reviewer whose review has not arrived                                      |
| `copilot_feedback`  | Unresolved review threads opened by Copilot                                                       |
| `ci_pending`        | Status check rollup `PENDING` or `EXPECTED`                                                       |
| `ci_failing`        | Rollup `FAILURE` or `ERROR`                                                                       |
| `ci_none`           | No status check rollup at all                                                                     |
| `already_assessed`  | The ledger holds this head (§6); `reassess` bypasses                                              |
| `too_large`         | More than 200 files or more than 512 KiB of patch                                                 |
| `cap`               | Over `max_pull_requests`, oldest `updated_at` first                                               |

<!-- markdownlint-enable MD013 -->

The `repository` and `bot_author` rules run on the search result
before any further read. One GraphQL query per survivor then
supplies the state the next ten rules inspect; the diff and commit
reads, which cost the most, run for survivors alone. The default cap
is 20 pull requests; `0` lifts it to the matrix limit of 256.

Named pull requests bypass the exclusion list and the search,
nothing else. Every other rule applies to them, so an operator
cannot push a draft or a red build through by naming it. A named
pull request that the token cannot read is an operator error; the
same failure during a scan is an operational error. Both fail the
run.

For each survivor `selection.json` records the head SHA the review
job will check out, the reviews and checks the apply job will
re-verify, and the bounded diff the agent reads: every changed file
with its unified patch (64 KiB per file, marked `patch_truncated`
when cut), the body (64 KiB), and each commit message (4 KiB). The
`key` is `<repo_name>-<number>` and names every per-entry artifact.

## 6. The Ledger

The ledger is the run-to-run memory of which pull request heads a
run assessed. Without it an unchanged pull request would cost one
agent session every three hours.

Each run's report job uploads `ledger.json` as the artifact
`code-review-ledger` with 90-day retention. The next run's select
job fetches the newest five of those artifacts through the bounded
`ledger` profile of `artifact_fetch.py`, merges them keeping the
newest assessment per `(repository, number, head_sha)` and dropping
entries older than the retention, and writes the result as
`artefacts/ledger.json`. The merged file travels inside the evidence
artifact with its own digest, so the report job that extends it
starts from bytes the select job vouched for.

The report job records one entry per result whose verdict is
`approved`, `would-approve` or `needs-human`. Skips and `failed`
results are not assessments: the next run looks at those heads
again. Each entry carries the verdict, the tier, whether the run was
dry, the run id and a timestamp.

Dry runs record too, so a rollout in dry-run does not re-review the
same heads every run. A live run ignores dry-run entries: the first
live run after the flip must still approve what dry runs reported
without acting. A dry run honours both kinds.

When a prior artifact fails to download or fails the bounded
acceptance, the fetch notes it and leaves it out of the merge.
Memory degrades to re-assessment, which costs sessions and nothing
else.

## 7. The Review Job

### 7.1 Inputs to the session

- `artefacts/pull.json`: the one entry of `selection.json` whose
  `key` matches the matrix entry, projected with `jq`. The step
  checks that the entry's `head_sha` matches the matrix and that the
  checkout's `HEAD` matches both.
- `workspace/`: the head repository at the head SHA, checked out
  with `persist-credentials: false`. The job-native token has no
  grant on a fork; the checkout succeeds because the estate is
  public.
- `artefacts/prompt.md`: `prompt/review.md` followed by a runtime
  context block naming the repository, pull request number, head
  SHA, dry-run flag, approvable tiers, and the two paths above.

The Copilot CLI installs from the committed lockfile under
`tools/copilot-cli` with `npm ci --ignore-scripts`, into
`$RUNNER_TEMP`, outside the workspace the session reads.

### 7.2 Copilot CLI invocation

```text
copilot --prompt="$prompt" --model="$MODEL"
  --available-tools=bash,list_bash,read_bash,stop_bash
  --allow-tool=shell(cat),shell(cat:*),...,shell(ls),shell(ls:*)
  --deny-tool=write,shell(gh),shell(gh:*),shell(git),shell(git:*)
  --secret-env-vars=COPILOT_GITHUB_TOKEN
  --no-ask-user --no-custom-instructions --disable-builtin-mcps
  --no-auto-update --no-color
  --log-dir=artefacts/copilot-logs
  --usage-output-file artefacts/usage.json
  --share=artefacts/session-summary.md
```

The step refuses to start unless the token carries the
`github_pat_` prefix. The prefix rejects native and classic tokens;
it cannot see extra grants on a fine-grained PAT, which the
provisioning procedure in the setup guide keeps out. The step's
`timeout-minutes` is `max_runtime_minutes`; the job's is that plus
fifteen for toolchain setup and upload.

### 7.3 What the prompt asks for

`prompt/review.md` defines three tiers and asks for one:

- **`trivial`**: the diff touches nothing but dependency or version
  pins (including `uses:` lines where the SHA and version comment
  alone change), lockfiles, README and documentation, comments, or pure
  formatting and typo fixes, and touches no workflow logic,
  triggers, permissions, shell logic, action inputs or outputs, or
  anything else that changes runtime behaviour.
- **`low-risk`**: the diff does touch workflow, action, script or
  code logic, and a full review finds no issue under three heads:
  security (no new or widened permissions, no secrets newly read or
  echoed, no untrusted input reaching a shell unquoted or through
  `${{ }}`, no action loosened from a SHA, no new network calls, no
  `pull_request_target`, no `curl | sh`); CI/CD sanity (inputs and
  outputs keep names and types or change additively, no required
  step removed, no trigger change that would stop CI without notice,
  valid syntax); and zero unresolved concerns.
- **`needs-human`**: everything else, including any uncertainty
  and any feedback the agent would want a maintainer to weigh.

The prompt tells the agent that pull request text is data, never
instruction; that it must judge the diff rather than the repository
name or the author; that it reads the packet first and opens the
checkout for context on what a changed line does; and that it ends
its final message with one fenced `json` block carrying `schema`,
`repository`, `pull_request`, `head_sha`, `tier`, `summary`,
`findings` and `injection_attempts`. The `summary` appears, after
sanitisation, in the approval body, so the prompt asks for one or
two sentences for the maintainer who merges and forbids mentioning
people.

### 7.4 Outputs

The session artifact `code-review-session-<ns>-<key>` holds
`session-summary.md`, `usage.json`, `prompt.md` and `copilot-logs/`,
with 7-day retention. One name per pull request across attempts and
`overwrite: true`: a rerun of this entry replaces its own earlier
session rather than colliding with it, and the apply job always
finds it by the same name. The upload runs whenever the packet step
succeeded, so a session that timed out still leaves its logs.

A cleanup step then deletes the CLI's spilled tool-output files,
its home directory and the checkout. Hygiene, not secure erasure:
it runs under `continue-on-error` so cleanup never gates the apply
path.

### 7.5 Egress

The review job runs harden-runner in `audit` mode whatever the
caller's `egress_policy`. The model backend, `api.githubcopilot.com`,
is absent from the organisation allow-list, and the job holds
nothing a block could protect beyond the model credential. §16
records the switch to block mode as a later capability.

## 8. The Apply Job

### 8.1 Fetch

`artifact_fetch.py --profile session` locates the session artifact
through the API by name within this run, refuses a zip larger than
the sum of the permitted file caps, streams it to disk under that
limit, and extracts `session-summary.md` (8 MiB cap) and
`usage.json` (1 MiB cap) alone, each read with a hard stop. Exit 3
(no artifact) and exit 4 (refused) become `--failure` text for the
check rather than a failed job, so a session that never ran still
yields a verdict for its pull request.

### 8.2 Offline check

`apply_review.py check` reads the summary, extracts the final fenced
`json` block, and validates it: schema 1, a tier from the three,
`repository`, `pull_request` and `head_sha` equal to the selection
entry, bounded `summary`, `findings` with known areas, bounded
`injection_attempts`. Any shape problem, an unterminated fence or a
missing summary leaves the verdict `None`, the effective tier
`needs-human`, and a reason saying why.

With a usable verdict, `review_policy.check` applies three rules
from `review_vetoes.py`:

1. **Corroboration of `trivial`.** `classify_trivial` must find
   every changed file trivial, or the tier drops to `needs-human`
   with the non-trivial paths as the reason. A file is trivial
   when its name marks it as documentation (`.md`, `.markdown`, `.rst`,
   `.adoc`, `LICENSE*`, `NOTICE*`, images under `docs/`; never
   `CODEOWNERS` or a `requirements*` file), a lockfile by name
   (`uv.lock`, `poetry.lock`, `package-lock.json`, `yarn.lock`,
   `pnpm-lock.yaml`, `Cargo.lock`, `go.sum`, `Gemfile.lock` or any
   `*.lock`), or a manifest whose every changed line is a pin:
   `requirements*.txt` and `.in`, `pyproject.toml`, `package.json`,
   `go.mod`, `Cargo.toml`, `rev:` lines in
   `.pre-commit-config.yaml`, `uses:` lines in workflows and
   `action.y?ml` where the removed and added action paths pair up
   and every added ref is a 40-hex SHA, and `FROM` lines in a
   `Dockerfile*` whose added form pins `@sha256:`. Plain `.txt`
   files are not documentation: allow-lists and pins end in `.txt`.
   A truncated or missing patch is not trivial unless the file is
   documentation. A change to code comments alone is not trivial
   because the classifier cannot tell a comment from code with
   confidence.
2. **Vetoes on any approvable tier.** `find_vetoes` names changes
   no approval may carry, whatever the agent concluded. Over the
   changed lines of every non-documentation file:
   - a truncated or missing patch: "patch for `<path>` is
     unreadable";
   - a workflow adding a `pull_request_target` trigger;
   - a workflow adding `permissions: write-all` or `<key>: write`
     for any known permission key;
   - a workflow removing an empty `permissions: {}` block, which
     widens to the defaults;
   - a workflow adding a reference to a secret other than
     `secrets.GITHUB_TOKEN`, or `secrets: inherit`;
   - a workflow or action adding a `uses:` whose ref is not a
     40-hex SHA (local `./`, `$/` and `docker://` references exempt);
   - an action removing or renaming an input or output;
   - any file adding a `curl` or `wget` piped into a shell.
3. **Enabled tiers.** The effective tier is approvable when
   `approve_tiers` names it and not otherwise. A disabled tier
   keeps its name in the result with the reason "tier not enabled
   for approval".

The check writes `check.json`, a short Markdown summary for the step
summary, and `approvable=` plus `tier=` to the step outputs. The
workflow gates the token mint on `approvable == 'true'`.

### 8.3 Live re-read and the one write

The write token mints when three conditions hold: the run is live,
the offline check accepted the verdict and a client id exists. It
mints for `matrix.repo_name` alone with `pull-requests: write`. Every other
path runs `apply_review.py apply` with the job-native token, which
cannot approve anything.

`apply` re-reads the pull request with the same GraphQL query the
selection used and refuses, with a reason each, unless the pull
request is `OPEN`, not a draft, at the same head SHA, with CI
`success`, with no reviewer requesting changes, with no Copilot
review pending, with no unresolved Copilot thread, and with no
standing approval on the head. The selection skipped approved pull
requests, so an approval arriving here landed during the run, the
bot's own from an overlapping attempt included; a second one is
noise.

With `--dry-run` the verdict is `would-approve` and `apply` writes
nothing. Otherwise `apply` posts one review:

```text
POST repos/{owner}/{repo}/pulls/{number}/reviews
{"commit_id": "<head_sha>", "event": "APPROVE", "body": "<body>"}
```

`commit_id` pins the approval to the reviewed head. `apply`
composes the body; the agent never writes to GitHub:

```text
<opening line for the tier>

<sanitised summary>

---
Automated review by `lf-releng-code-review-bot[bot]` using model
`claude-opus-5.5`; see the [run](<run_url>). This is not a human review.
```

The opening lines live in `OPENINGS` in `apply_review.py`, one per
approvable tier, each starting "🤖 Auto-approved by agent:". The
`trivial` line names a dependency, documentation or formatting
change; the `low-risk` line says a review of the workflow or code
change for security and CI/CD impact found low risk. The
sanitiser folds control characters, escapes `::` and
`##[`, collapses whitespace, truncates to 600 characters, and turns
every `@` into `&#64;`, so the body can never mention a user or a
team. The reviewer name is the App login from the selection, or
"the code review bot" in a secretless run; the model name comes
from the selection, not from the session.

A GitHub failure during the write is an operational error: `apply`
exits 1 and the workflow's fallback step writes a `failed` result
whose reason names the step outcomes. A policy refusal is the code
working: a typed `needs-human` result and exit 0.

## 9. Inputs

### 9.1 Reusable workflow (`code-review.yaml`)

<!-- markdownlint-disable MD013 -->

| Input                   | Default                                   | Meaning                                                                           |
| ----------------------- | ----------------------------------------- | --------------------------------------------------------------------------------- |
| `org`                   | required                                  | Target owner; live runs require an organisation App                               |
| `dry_run`               | `true`                                    | Run the agent and the checks; approve nothing                                     |
| `model`                 | `claude-opus-5.5`                         | Copilot CLI model identifier, `^[a-z0-9.-]+$`                                     |
| `approve_tiers`         | `trivial,low-risk`                        | Tiers the run may approve: `trivial,low-risk`, `trivial` or `none`                |
| `pull_requests`         | `''`                                      | Pull requests to review instead of scanning (§9.4)                                |
| `repositories`          | `''`                                      | Repository names to scan; commas and/or spaces                                    |
| `exclude_repos`         | `''`                                      | Repository names to skip; overrides the bundled list                              |
| `max_pull_requests`     | `20`                                      | Cap after ordering; `0` lifts it to 256                                           |
| `max_concurrent_agents` | `5`                                       | Review sessions at once, 1-30                                                     |
| `max_runtime_minutes`   | `20`                                      | Wall-clock budget per session, 1-120                                              |
| `reassess`              | `false`                                   | Review heads the ledger already records                                           |
| `skip_agent`            | `false`                                   | Plumbing test: select and report without sessions                                 |
| `egress_policy`         | `block`                                   | harden-runner policy for trusted jobs: `audit` or `block`                         |
| `egress_allow_config`   | `''`                                      | `harden-runner-block-action` config coordinate                                    |
| `github_app_client_id`  | `''`                                      | App client id; empty limits runs to dry-run                                       |
| `assets_repository`     | `lfreleng-actions/github-code-review-bot` | Trusted source of prompt and scripts                                              |
| `assets_ref`            | `''`                                      | Commit, tag or branch of the assets; empty resolves the calling workflow's commit |

<!-- markdownlint-enable MD013 -->

<!-- markdownlint-disable MD013 -->

| Secret                   | Required      | Meaning                                                         |
| ------------------------ | ------------- | --------------------------------------------------------------- |
| `copilot_token`          | for sessions  | Fine-grained PAT with Copilot Requests and no repository grants |
| `github_app_private_key` | for live runs | App private key; trusted jobs alone                             |

<!-- markdownlint-enable MD013 -->

The select job validates every numeric and enumerated input before
any token mints, and fails a live run that lacks a client id. It
also derives the repository scope of the read token: the named
repositories, or the repositories of the named pull requests, or
empty for an organisation-wide token when scanning.

### 9.2 Scheduled caller (`code-review-cron.yaml`)

Two cron entries, both UTC: `0 */3 * * 1-5` and `0 8,20 * * 0,6`.
The `workflow_dispatch` form offers `dry_run` (default `true`), a
model choice, `approve_tiers`, `pull_requests`, `repositories`,
`max_pull_requests`, `max_concurrent_agents` and `reassess`.

An `options` job maps the display name to the identifier the CLI
accepts: Claude Opus 5.5 to `claude-opus-5.5`, Claude Fable 5.1 to
`claude-fable-5.1`, Claude Sonnet 5.5 to `claude-sonnet-5.5`, GPT-6
Astra to `gpt-6-astra`. The caller passes
`org: github.repository_owner`, so a fork's scheduled runs stay
inside the fork; pins the assets to `github.sha`; runs trusted jobs
in block mode with the organisation allow-list; and hands
`vars.BOT_APP_CLIENT_ID`, `secrets.BOT_APP_PRIVATE_KEY`
and `secrets.COPILOT_CLI_TOKEN` to the reusable workflow.

Scheduled runs take `dry_run` from the expression
`github.event_name != 'workflow_dispatch' || inputs.dry_run`: dry
until the rollout flips it, while a manual dispatch takes the
operator's choice. Disabling the workflow in the Actions UI is the
kill switch.

### 9.3 Testing caller (`testing.yaml`)

Pull request runs are secretless by design: a same-repository pull
request can change the reusable workflow and the scripts, so a check
holding the model credential would hand it to the code under
review. Two `plumbing` legs run with `dry_run: true`,
`skip_agent: true`, the pull request head as assets, block-mode
egress and a cap of three; two legs prove invocation namespaces
never collide. A `regression` job runs the offline suite with
`uv run --locked`, and a `docs` job builds the site with
`mkdocs build --strict` without any Pages permission.

The full agent session runs from `workflow_dispatch` alone: a
maintainer dispatches `testing.yaml` against a chosen ref with
optional `pull_requests`, `repositories` and `max_pull_requests`
(default 2), two concurrent agents, `dry_run: true` and the
`COPILOT_CLI_TOKEN` secret. No App credential reaches this path, so
it can report but never approve.

### 9.4 Targeted runs

`pull_requests` accepts `repo/pull/N`, `owner/repo/pull/N`,
`repo#N`, `owner/repo#N` and full `https://github.com/...` URLs,
separated by commas and/or spaces, deduplicated in order. An owner
other than `org` is an error. Named pull requests bypass the
exclusion list and the scan alone; every skip rule in §5 still
applies, and the read token scopes to their repositories.

### 9.5 Mode and dry-run matrix

<!-- markdownlint-disable MD013 -->

| Path                             | `dry_run`         | App | Agent             | Writes                                   |
| -------------------------------- | ----------------- | --- | ----------------- | ---------------------------------------- |
| Schedule, before the flip        | `true`            | yes | yes               | none; `would-approve` recorded           |
| Schedule, after the flip         | `false`           | yes | yes               | one approval per approvable pull request |
| Cron dispatch                    | operator's choice | yes | yes               | as above                                 |
| `testing.yaml` on a pull request | `true`            | no  | no (`skip_agent`) | none                                     |
| `testing.yaml` dispatch          | `true`            | no  | yes               | none                                     |

<!-- markdownlint-enable MD013 -->

## 10. Credentials

### 10.1 A dedicated App, under template names

The workflow runs as its own GitHub App. The first live approval
used the issues triage App with its permissions widened, and that
was a mistake worth recording: an identity that approves pull
requests must never be one that also labels issues or pushes code,
because a permission added for one pipeline widens the other, and
because the ruleset exception an approver needs must stay as narrow
as the approver. In this organisation the App is **LF/RelEng Code
Review Bot**; its slug lives in `config/bot.json`, and the
pre-flight gate (section 4.4) refuses a token minted by any other
App.

The calling workflow names the credentials by role, never by App or
repository: `vars.BOT_APP_CLIENT_ID` and `secrets.BOT_APP_PRIVATE_KEY`.
Every bot repository in the organisation uses the same two names,
so the callers are identical and the App behind a name can change
without a code change.

Repository permissions the App needs, and no others:

<!-- markdownlint-disable MD013 -->

| Permission      | Access         | Used by                      |
| --------------- | -------------- | ---------------------------- |
| Pull requests   | read and write | select reads; apply approves |
| Contents        | read           | select reads repositories    |
| Checks          | read           | status check rollup          |
| Commit statuses | read           | status check rollup          |
| Metadata        | read           | required by every token      |

<!-- markdownlint-enable MD013 -->

Two mints, each the least its step needs. Select mints
`pull-requests`, `contents`, `checks`, `statuses` and `metadata` at
`read`, scoped to the named repositories when any and
organisation-wide for a scan. Apply mints `pull-requests: write`
plus the three reads for `matrix.repo_name` alone, on the live
path and after the offline check accepted the verdict. The App key
appears in the select and apply jobs as an input to the token
action and nowhere else; the review job's steps reference neither
the key nor an App token.

The ledger lives in this repository's own artifacts, so the select
job reads it with the job-native token under `actions: read`; the
App has no grant here and needs none.

### 10.2 The model credential

The organisation secret `COPILOT_CLI_TOKEN` holds a personal
fine-grained PAT with Copilot Requests and no repository grants. The
review step checks the `github_pat_` prefix before launching the
CLI, which rejects native and classic tokens and nothing more; the
setup guide's provisioning procedure keeps extra grants out. The
token's owner is the rotation owner; an expired token fails every
session at start, visible in the report as `needs-human` rows
whose reason names a missing session.

### 10.3 Models

The default model is `claude-opus-5.5`. The dispatch form offers
Claude Opus 5.5, Claude Fable 5.1, Claude Sonnet 5.5 and GPT-6
Astra (§9.2). The selection records the model, the approval body
names it from the selection, and `usage.json` supplies premium
request cost and API seconds per session, which the report sums.

## 11. Scheduling and Concurrency

- Weekdays every three hours and weekends at 08:00 and 20:00 UTC.
  Nothing upstream gates this schedule: a pull request becomes
  eligible when its CI is green and no review blocks it, whenever
  that happens.
- Reusable workflow group: live runs lock on
  `code-review-<caller repository>-<org>` across the whole
  pipeline; dry runs lock on `code-review-dry-run-<run id>-<org>`,
  a group of their own, so they neither wait on nor cancel a live
  run. `cancel-in-progress: false` throughout.
- Scheduled caller group: `code-review` for schedules and live
  dispatches, a per-run group for dry dispatches. A group holds one
  pending run and a newcomer replaces it, so this is not a FIFO
  queue.
- Testing caller group: `testing-<ref>` on pull requests with
  `cancel-in-progress: true`, so a new push supersedes the older
  run; a per-run group for dispatches, so a second dispatch never
  cancels an agent session in flight.
- Within a run, the matrix makes entries independent;
  `max_concurrent_agents` bounds the review job's `max-parallel`.
- Across runs, the ledger (§6) and the `already_approved` skip stop
  a head from costing a second session or a second approval.

## 12. Artefacts and Retention

<!-- markdownlint-disable MD013 -->

| Artifact                                  | Producer | Content                                                                                      | Retention |
| ----------------------------------------- | -------- | -------------------------------------------------------------------------------------------- | --------- |
| `code-review-evidence-<ns>`               | select   | `selection.json`, `matrix.json`, `ledger.json`, `excluded-repos.txt`, `selection-summary.md` | 7 days    |
| `code-review-session-<ns>-<key>`          | review   | `session-summary.md`, `usage.json`, `prompt.md`, `copilot-logs/`                             | 7 days    |
| `code-review-result-<ns>-<key>-<attempt>` | apply    | `check.json`, `check-summary.md`, `result.json`                                              | 90 days   |
| `code-review-<ns>-<attempt>`              | report   | `report.md`, `report.json`                                                                   | 90 days   |
| `code-review-ledger`                      | report   | `ledger.json`                                                                                | 90 days   |

<!-- markdownlint-enable MD013 -->

`<ns>` is the select job's namespace, `run-attempt-uuid`, and
`<key>` the pull request key. Trusted evidence downloads use the
producer's artifact ID and verify the digests; the session download
uses the name within the run and the bounded extractor (§8.1); the
report job gathers results by name pattern within the namespace.
Result and report names append the run attempt so a rerun avoids
immutable-name conflicts while producer artifacts survive. The
ledger's fixed name is unique within a run, which is all the upload
requires; the next select job fetches the newest five by that name.

## 13. Failure Modes

<!-- markdownlint-disable MD013 -->

| Failure                                                  | Effect                                     | Mitigation                                                                                        |
| -------------------------------------------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------------------- |
| Session times out or the CLI fails                       | Summary missing or without a verdict block | Apply writes a typed `needs-human` result with the reason; `reassess` retries the head            |
| Session artifact missing or refused by the bounded fetch | Exit 3 or 4 from the fetch                 | `--failure` text becomes the reason; typed `needs-human`, no job failure                          |
| Verdict names another repository, number or head         | Target mismatch                            | Offline check refuses; `needs-human` with the reason                                              |
| Agent claims `trivial` on a code change                  | Classifier disagrees                       | Tier drops to `needs-human`; non-trivial paths listed                                             |
| Head moves between select and apply                      | Live re-read sees a new SHA                | `needs-human` with "head moved"; the new head is a new key next run                               |
| Human approves or requests changes during the run        | Live re-read sees it                       | `needs-human`; no duplicate or conflicting review                                                 |
| App token mint fails                                     | Apply step skipped                         | Fallback writes a `failed` result, a report row, no ledger entry; next run retries                |
| GitHub unreachable during the write                      | `apply` exits 1                            | Same fallback; the run shows the failed entry                                                     |
| Ledger artifact unreadable                               | Prior memory incomplete                    | Reported and left out of the merge; heads re-assessed                                             |
| Model PAT expired                                        | Every session fails at start               | Prefix guard cannot detect; rotation owner (§10.2); report shows uniform failures                 |
| Search hits the 1,000 ceiling                            | Selection fails before filtering           | Restrict `repositories` rather than review a subset                                               |
| Two live runs overlap                                    | Possible second approval                   | Caller and reusable concurrency groups; `already_approved` gate on re-read                        |
| Runaway spend                                            | Long sessions in parallel                  | `max_runtime_minutes` times `max_concurrent_agents` bounds wall-clock; usage in the report        |
| Prompt injection in a pull request                       | Agent misled                               | No credential to misuse; corroboration, vetoes and live gates; `injection_attempts` in the report |

<!-- markdownlint-enable MD013 -->

## 14. Repository Layout

```text
.github/workflows/code-review.yaml       reusable workflow
.github/workflows/code-review-cron.yaml  schedule and dispatch caller
.github/workflows/testing.yaml           PR plumbing, manual dry-run,
                                         regression, docs build
.github/workflows/documentation.yaml     mkdocs to GitHub Pages
prompt/review.md                         agent task (§7.3)
config/excluded-repos.txt                repositories the scan skips
tools/copilot-cli/                       pinned CLI lockfile
scripts/bot_github.py                    gh wrapper, REST and GraphQL
scripts/bot_evidence.py                  evidence digests, file caps
scripts/artifact_fetch.py                bounded artifact extraction
scripts/ledger.py                        run-to-run memory (§6)
scripts/pull_reads.py                    GitHub reads for selection
scripts/pull_changes.py                  bounded files and commits
scripts/select_pulls.py                  selection policy (§5)
scripts/selection_inputs.py              input parsing contract
scripts/selection_outputs.py             selection files and summary
scripts/review_policy.py                 verdict extraction, check
scripts/review_vetoes.py                 classifier and vetoes (§8.2)
scripts/apply_review.py                  check and apply (§8)
scripts/review_report.py                 report and new ledger
tests/test_<module>.py                   offline unittest suite
tests/test_workflow.py                   workflow contract tests
pyproject.toml, uv.lock                  Python tooling
docs/DESIGN.md                           this document
docs/setup/README.md                     operator's guide
```

Every bot carries five shared modules, copied verbatim from
`lfreleng-actions/bots-template`: `bot_github.py`, `bot_evidence.py`,
`artifact_fetch.py`, `preflight.py`, `ledger.py`. Fix them in the
template first, then copy; never patch a copy alone. The same holds
for their test files.

The suite holds one test file per script module that owns a
contract (`apply_review`, `artifact_fetch`, `bot_evidence`,
`bot_github`, `ledger`, `preflight`, `pull_reads`, `review_policy`,
`review_report`, `select_pulls`) plus `test_workflow.py`, which
pins the workflow facts the scripts rely on: which job holds which
credential, which step gates the write token, and the artifact
names. The helpers split from `select_pulls` and `review_policy`
take their coverage through their parents. Linting runs through `prek`
hooks including gitleaks, gitlint, ruff, mypy, basedpyright,
actionlint, reuse, markdownlint, write-good, codespell and `aislop`
at threshold 100.

## 15. Rollout

1. **Secretless plumbing.** Every pull request to this repository
   runs the two `plumbing` legs and the regression suite. They
   prove selection, evidence upload, ledger fetch and the report
   from the pull request head with no secret in reach.
2. **Manual agent dry-run.** A maintainer dispatches `testing.yaml`
   against a ref, with `pull_requests` naming representative pull
   requests. The report shows what each tier would have
   approved and why; the session artifacts show what the agent
   read. Repeat until the tiers land where a reviewer would put
   them.
3. **Scheduled dry-run.** The schedule runs as committed, with
   `dry_run: true` and the App credentials, so the ledger fills and
   `would-approve` rows accumulate. Read the reports for false
   positives; a `would-approve` on a change a human would have
   questioned is a prompt or veto gap to close before the flip.
4. **The flip.** One pull request changes the `dry_run` expression
   in `code-review-cron.yaml` so scheduled runs pass `false`. The
   first live run approves what the dry runs reported, because
   live runs ignore dry-run ledger entries (§6). `approve_tiers`
   stays at `trivial,low-risk` from the start, the maintainer's
   choice; narrowing to `trivial` or `none` is a dispatch input away
   if the first live reports warrant it.
5. **Ruleset effect.** The organisation's branch ruleset counts an
   App approval towards required reviews. A trivial pull request
   with a bot approval is mergeable by its author; that is the
   intended effect, and the approval body says in its first line
   and its last that no human reviewed it.

## 16. Later Capabilities

- **Block-mode egress for the review job**, once the organisation
  allow-list carries the Copilot backend. The job would then share
  the trusted jobs' `egress_policy` rather than forcing `audit`.
- **A supplemental notification channel** for `needs-human`
  verdicts, if humans want one beyond the run report: a digest
  issue, a chat message, or a label on the pull request. Each is a
  write the current design avoids, and each needs its own
  provenance and rate thinking before it lands.
- **A per-repository opt-out label**, so a maintainer can keep the
  bot off one repository's pull requests without editing
  `config/excluded-repos.txt`.
- **Re-approval after a new push** needs nothing new: a ruleset
  with "dismiss stale reviews" drops the approval when the head
  changes, the new head is a new ledger key, and the next run
  assesses it afresh.

## 17. Decisions and Remaining Questions

Decided:

- **Approve, never comment.** A bot comment on a pull request is
  noise a human has to dismiss; a bot approval is a decision a
  human can rely on or revert. Everything short of an approval
  goes to the report.
- **Deterministic corroboration of `trivial`.** The agent's tier is
  a claim. The classifier reads the same patches the agent read
  and must agree before an approval goes out under that tier.
- **Vetoes on every approvable tier.** Some changes no approval may
  carry whatever a reviewer concludes: a new `pull_request_target`
  trigger, a write permission, an unpinned action, a secret
  reference, a removed action input, a download piped into a
  shell. The list is short on purpose; it names the changes whose
  cost of a wrong approval is highest.
- **Low-risk enabled from the start.** The maintainer chose
  `trivial,low-risk` as the default rather than a staged widening,
  on the ground that the dry-run period exercises both tiers and
  the knob stays available.
- **One App for triage and review.** A second App would add
  inventory for no isolation gain: both workflows run in trusted
  jobs under the same organisation.
- **Dry-run entries in the ledger.** Without them the dry-run
  period would re-review every open pull request every three
  hours; with them, the first live run must still act, which the
  `dry_run` flag on each entry arranges.

Remaining, for rollout to settle:

- Whether the default cap of 20 and the three-hour cadence match
  the organisation's flow, or whether the schedule should slow once
  the initial backlog clears.
- Whether `max_runtime_minutes: 20` suffices for the largest pull
  requests under the size cap, or whether those should fall to
  `too_large` sooner.

## 18. Data Contracts

The files the jobs exchange. Every reader treats a file from a less
trusted producer as hostile input: typed, bounded and cross-checked
against the trusted `selection.json`.

### 18.1 `selection.json` (select to review, apply, report; trusted)

```json
{
  "schema": 1,
  "org": "lfreleng-actions",
  "generated_at": "2026-10-05T10:00:00Z",
  "dry_run": true,
  "model": "claude-opus-5.5",
  "approve_tiers": ["low-risk", "trivial"],
  "bot_login": "lf-releng-code-review-bot[bot]",
  "explicit_pull_requests": ["lfreleng-actions/repo#157"],
  "explicit_repositories": ["repo"],
  "exclusions": ["project-reporting-artifacts"],
  "candidates_seen": 13,
  "skip_counts": {"draft": 1, "bot_author": 2, "cap": 0},
  "skipped": [
    {"repository": "lfreleng-actions/repo", "number": 5,
     "url": "https://github.com/lfreleng-actions/repo/pull/5",
     "title": "...", "reason": "draft"}
  ],
  "pull_requests": [
    {
      "key": "repo-157",
      "repository": "lfreleng-actions/repo",
      "repo_name": "repo",
      "number": 157,
      "url": "https://github.com/lfreleng-actions/repo/pull/157",
      "title": "Fix: Correct the thing",
      "body": "bounded to 64 KiB",
      "author": "login",
      "author_association": "MEMBER",
      "head_sha": "<40 hex>",
      "head_repository": "fork-owner/repo",
      "head_ref": "fix/thing",
      "base_ref": "main",
      "base_sha": "<40 hex>",
      "is_fork": true,
      "labels": ["bug"],
      "created_at": "...", "updated_at": "...",
      "ci": "success",
      "checks": [{"name": "Testing", "status": "COMPLETED",
                  "conclusion": "SUCCESS"}],
      "reviews": [{"author": "login", "is_bot": false,
                   "state": "COMMENTED", "commit": "<40 hex>",
                   "submitted_at": "..."}],
      "commits": [{"sha": "<40 hex>", "message": "bounded 4 KiB",
                   "verified": true, "author": "login"}],
      "files": [{"path": "README.md", "status": "modified",
                 "additions": 1, "deletions": 1,
                 "previous_path": null, "patch": "@@ ... @@",
                 "patch_truncated": false}],
      "diff_bytes": 1234
    }
  ]
}
```

`bot_login` is `null` without an App slug. `approve_tiers` holds a
sorted list. `skip_counts` carries every reason from §5. `skipped` has
one row per skipped pull request; `title` may be `null` when the
skip happened before a read. Entries sort by repository and number.
`files` comes from REST `pulls/{n}/files`; a file GitHub returns
without a patch has `patch: null`.

### 18.2 `matrix.json` (select to workflow)

```json
{"include": [{"key": "repo-157",
              "repository": "lfreleng-actions/repo",
              "repo_name": "repo", "number": 157,
              "head_sha": "<40 hex>",
              "head_repository": "fork-owner/repo"}]}
```

The review and apply jobs fan out over `include`; `repo_name` scopes
the write token and `head_repository` with `head_sha` drive the
checkout.

### 18.3 `ledger.json` (report to the next run's select; trusted)

```json
{"schema": 1,
 "entries": [{"repository": "lfreleng-actions/repo", "number": 157,
              "head_sha": "<40 hex>", "verdict": "would-approve",
              "tier": "trivial", "dry_run": true,
              "run_id": 123456789,
              "assessed_at": "2026-10-05T10:04:00Z"}]}
```

`verdict` is one of `approved`, `would-approve` or `needs-human`.
The merge keeps the newest entry per `(repository, number,
head_sha)` within 90 days. The select job reads the merged prior
ledger and never writes it; the report job extends it.

### 18.4 Agent verdict (review to apply; untrusted)

The last fenced `json` block of `session-summary.md`:

```json
{
  "schema": 1,
  "repository": "lfreleng-actions/repo",
  "pull_request": 157,
  "head_sha": "<40 hex>",
  "tier": "trivial",
  "summary": "One or two sentences a maintainer reads in the approval.",
  "findings": [{"area": "security", "note": "..."}],
  "injection_attempts": ["..."]
}
```

`tier` is `trivial`, `low-risk` or `needs-human`; `area` is
`security`, `ci`, `correctness`, `scope` or `other`. The extractor
bounds the summary to 600 characters, findings to 20 of 400
characters each, and injection attempts to 20; it refuses an
unterminated fence or any shape problem.

### 18.5 `check.json` (apply, offline; trusted)

```json
{
  "schema": 1, "key": "repo-157",
  "repository": "lfreleng-actions/repo", "number": 157,
  "head_sha": "<40 hex>", "url": "...", "title": "...",
  "agent_tier": "trivial",
  "tier": "needs-human",
  "approvable": false,
  "reasons": ["trivial claim not corroborated: .github/workflows/ci.yaml"],
  "summary": "sanitised, bounded",
  "findings": [], "injection_attempts": [],
  "classifier": {"trivial": false,
                 "non_trivial_paths": [".github/workflows/ci.yaml"]},
  "vetoes": [],
  "premium_requests": 1.5, "agent_seconds": 42
}
```

`tier` is the effective tier after corroboration and vetoes;
`approvable` is `tier in approve_tiers`. `agent_tier` is `null` when
the check could read no verdict; `tier` is then `needs-human` and `reasons`
says why. `premium_requests` and `agent_seconds` come from
`usage.json` when present and finite.

### 18.6 `result.json` (apply, per pull request; trusted)

```json
{
  "schema": 1, "key": "repo-157",
  "repository": "lfreleng-actions/repo", "number": 157,
  "url": "...", "title": "...", "head_sha": "<40 hex>",
  "verdict": "approved", "tier": "trivial",
  "reasons": [], "summary": "...", "findings": [],
  "injection_attempts": [],
  "review_url": "https://github.com/.../pull/157#pullrequestreview-1",
  "dry_run": false, "run_attempt": 1,
  "premium_requests": 1.5, "agent_seconds": 42
}
```

`verdict` is `approved`, `would-approve`, `needs-human` or
`failed`. The workflow's fallback step writes `failed` with the same
keys when apply never produced a result; `tier` is then `null` and
`reasons` names the step outcomes.

### 18.7 `report.json` and `report.md` (report; trusted)

```json
{"schema": 1, "dry_run": true,
 "counts": {"approved": 0, "would-approve": 2, "needs-human": 1,
            "failed": 0, "skipped": 9, "candidates_seen": 13},
 "results": ["...result.json objects sorted by key..."],
 "skipped": ["...from selection.skipped..."],
 "premium_requests": 4.5, "agent_seconds": 300,
 "ledger_entries": 42}
```

`report.md` carries a heading, a one-line count summary, a table of
results (pull request link, title, author, tier, verdict, review
link or first reason), a table of skipped pull requests grouped by
reason, and the spend line. Summaries and reasons pass through the
same one-line sanitiser as the approval body. A result file that is
unreadable or malformed stays out, with a note, so one broken
matrix leg cannot hide every other verdict or stop the ledger.
