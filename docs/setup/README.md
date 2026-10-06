<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Setup

The pipeline runs four jobs: trusted **Select**, untrusted **Review**
(one per pull request), trusted **Apply** (one per pull request) and
trusted **Report**. Select builds an offline packet for each eligible
pull request; Review runs Copilot CLI against the packet and a
credential-free checkout; Apply corroborates the verdict, re-reads
the live
pull request and submits the one approval; Report writes the run
summary and the ledger. [`../DESIGN.md`](../DESIGN.md) explains why
each job holds what it holds.

This guide covers what an operator configures and checks.

## GitHub App

The workflow runs as a dedicated GitHub App that does nothing but
review pull requests. In this organisation that App is **LF/RelEng
Code Review Bot** (slug `lf-releng-code-review-bot`); the slug lives
in `config/bot.json`, and the select job refuses a token minted by
any other App (see *Pre-flight gate* below). Grant it these
repository permissions and nothing else:

<!-- markdownlint-disable MD013 -->

| Permission      | Access         | Why                                                    |
| --------------- | -------------- | ------------------------------------------------------ |
| Pull requests   | Read and write | Select reads pull requests; Apply submits the approval |
| Contents        | Read           | Select reads repositories and the scan                 |
| Checks          | Read           | Status check rollup for the CI gates                   |
| Commit statuses | Read           | Status check rollup for the CI gates                   |
| Metadata        | Read           | Mandatory for every installation token                 |

<!-- markdownlint-enable MD013 -->

The App stays separate from the issues triage and code monkey Apps
on purpose: an approving identity must never be one that also pushes
code, and a permission added for one pipeline must never widen
another.

**Accept permission changes on the installation.** Adding
permissions to an App does not change its existing installations.
GitHub emails the organisation owners a request, and the
installation page under *Organization settings → GitHub Apps* shows
a pending review. Until an owner accepts it there, the select job's
token mint fails with a message naming the permission the
installation lacks. The installation must cover every repository the
scan should see; "all repositories" matches the organisation-wide
scan.

Select mints `pull-requests`, `contents`, `checks`, `statuses` and
`metadata` at read, scoped to the named repositories when any.
Apply mints `pull-requests: write` for one repository, on the live
path alone, after the offline check accepted the verdict.
Review receives neither the App key nor any App token.

## Variables and secrets

Every bot repository in this organisation uses the same names, so
the calling workflow is identical across them and the App behind a
name can change without a code change. Configure these on the
repository that runs the scheduled caller:

<!-- markdownlint-disable MD013 -->

| Name                  | Kind                | Value                                                 |
| --------------------- | ------------------- | ----------------------------------------------------- |
| `BOT_APP_CLIENT_ID`   | Repository variable | The App's client id; empty limits runs to dry-run     |
| `BOT_APP_PRIVATE_KEY` | Repository secret   | The App's private key (PEM)                           |
| `COPILOT_CLI_TOKEN`   | Organisation secret | Fine-grained PAT with Copilot Requests (next section) |

<!-- markdownlint-enable MD013 -->

The reusable workflow sees nothing but the values handed to its
named inputs, so another caller may hold them under other names.
Keep the App key out of any job that runs an agent; the bundled
callers do.

## The model credential

Copilot CLI authenticates with a personal fine-grained PAT. Create
one at
<https://github.com/settings/personal-access-tokens/new> with:

- **Resource owner**: your own account.
- **Repository access**: *Public repositories* is enough; grant no
  repository permission at all.
- **Account permissions**: *Copilot Requests* (read) and nothing
  else.
- **Expiry**: as short as your rotation cadence allows. Note
  the date; the review step cannot see it.

Store the value as `COPILOT_CLI_TOKEN`. The review step checks the
`github_pat_` prefix before launching the CLI, which rejects classic
and native tokens but cannot see extra grants on a fine-grained one.
Review the token's grants yourself. Native caller tokens are not
supported. An expired token fails every session at start; the
report then shows uniform `needs-human` rows whose reason names a
missing session.

## Validation

Start offline:

```bash
uv run python -B -m unittest discover -s tests -v
prek run --all-files
zizmor --persona auditor .github/workflows/
```

The suite includes `tests/test_workflow.py`, which pins which job
holds which credential and which step gates the write token. Expect
zero findings from zizmor.

Every pull request to this repository runs the secretless plumbing
legs: Select and Report with the pull request head as assets,
`skip_agent: true` and no secret in reach. They prove selection,
evidence upload, the ledger fetch and the report.

Run the agent itself from a manual dispatch of the testing
workflow, against a ref you have reviewed:

```bash
gh workflow run testing.yaml \
  -f pull_requests='repo/pull/157, other-repo#42'
```

`pull_requests` and `repositories` are optional; without them the
dispatch scans the organisation with a cap of two. This path is
always dry-run and passes no App credential, so it reports and
never approves. Read the step summary of the Report job and the
session artifacts (next section) to judge whether each tier landed
where a reviewer would put it.

## Reading the artifacts

Three families, all under the run's *Artifacts* section:

- **Evidence, 7 days** (`code-review-evidence-<ns>`):
  `selection.json` with every candidate, skip reason and selected
  entry; `matrix.json`; the merged prior `ledger.json`; the resolved
  exclusions; the selection summary. Start here when a pull request
  you expected is missing: `skipped[]` names the reason.
- **Session, 7 days** (`code-review-session-<ns>-<key>`): the exact
  prompt, the CLI logs, `usage.json` and `session-summary.md`, whose
  last fenced `json` block is the verdict. Start here when a tier
  looks wrong: the summary shows what the agent read and how it
  reasoned.
- **Results and report, 90 days** (`code-review-result-<ns>-<key>-
  <attempt>`, `code-review-<ns>-<attempt>`, `code-review-ledger`):
  `check.json` with the classifier output, the vetoes and the
  effective tier; `result.json` with the verdict, the gate reasons
  and the review URL; `report.md` and `report.json` for the whole
  run; and the ledger the next run reads.

The Report job's step summary reproduces `report.md`, so a glance at
the run page answers "what happened" without a download.

## Going live

The schedule ships with `dry_run: true`. Let it run for long enough
that the reports show `would-approve` rows across a run of days, and
read each one: a `would-approve` on a change a human would have
questioned is a prompt or veto gap to close first.

To flip, open a pull request that changes the `dry_run` expression
in `.github/workflows/code-review-cron.yaml` so scheduled runs pass
`false`; the comment above it marks the line. The first live run
approves what the dry runs reported, because live runs ignore
dry-run ledger entries. `approve_tiers` defaults to
`trivial,low-risk`; a dispatch can narrow it to `trivial` or `none`
without a code change.

The organisation's branch ruleset counts an App approval towards the
required review. After the flip, a trivial pull request with a bot
approval is mergeable by its author. The approval body says in its
first line and its last that no human reviewed it.

Disabling the scheduled workflow in the Actions UI is the kill
switch.

## A targeted urgent review

To review specific pull requests ahead of the schedule, dispatch the
scheduled caller with `pull_requests` set and `dry_run` cleared:

```bash
gh workflow run code-review-cron.yaml \
  -f dry_run=false \
  -f pull_requests='repo/pull/157 owner/repo#42'
```

Accepted spellings: `repo/pull/N`, `owner/repo/pull/N`, `repo#N`,
`owner/repo#N` and full URLs, separated by commas and/or spaces.
Named pull requests bypass the exclusion list and the scan, nothing
else.
Every other rule still applies: a draft, a red build, a pending
Copilot review or a standing approval still skips the pull request,
and the report says so. Add `-f reassess=true` to review a head the
ledger already records.

## Recovery

The pipeline's single write is an APPROVE review on the head it
assessed. Nothing needs rolling back.

- **A wrong approval.** Dismiss it by hand from the pull request's
  review list, with a note. The head stays in the ledger, so the
  bot will not approve it again; a new push produces a new head and
  a fresh assessment.
- **A missed pull request.** Check `selection.json` in the evidence
  artifact for its skip reason. Fix the cause (green CI, resolve
  Copilot threads, mark ready for review) or name it in a targeted
  dispatch.
- **A `failed` row.** The reason names the step that did not
  complete: a token mint, the offline check or GitHub during the
  write. Failed heads are not recorded in the ledger, so the next
  run retries them without `reassess`.
- **A lost ledger.** The select job merges the newest five ledger
  artifacts; it notes an unreadable one and leaves it out. The cost
  is re-assessment of heads the lost entries covered, nothing more.
- **A stuck run.** Live runs serialise on one concurrency group per
  caller; cancel a stuck run from the Actions UI and the next
  schedule proceeds. Cancellation never undoes an approval already
  submitted.

## GitHub Pages

The documentation site builds with MkDocs and deploys from
`.github/workflows/documentation.yaml` on pushes to `main` that
touch `docs/`, `mkdocs.yml` or the workflow itself. Set the
repository's Pages source to **GitHub Actions** under *Settings →
Pages*; the branch-based source does not work with this workflow.
Pull requests build the site with `mkdocs build --strict` and
deploy nothing.

## Further reading

- [Design](../DESIGN.md): the architecture, the trust boundary, the
  skip rules, the classifier and vetoes, and the data contracts.
- [`prompt/review.md`](https://github.com/lfreleng-actions/github-code-review-bot/blob/main/prompt/review.md):
  the agent's task and the three tiers as it reads them.
