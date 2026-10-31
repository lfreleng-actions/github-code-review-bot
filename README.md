<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# 🤖 GitHub Code Review Bot

<!-- prettier-ignore-start -->
<!-- markdownlint-disable-next-line MD013 -->
[![Linux Foundation](https://img.shields.io/badge/Linux-Foundation-blue)](https://linuxfoundation.org/) [![Source Code](https://img.shields.io/badge/GitHub-100000?logo=github&logoColor=white&color=blue)](https://github.com/lfreleng-actions/github-code-review-bot) [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0) [![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/lfreleng-actions/github-code-review-bot/badge)](https://scorecard.dev/viewer/?uri=github.com/lfreleng-actions/github-code-review-bot)
<!-- prettier-ignore-end -->

Scheduled AI review of pull requests across the `lfreleng-actions`
organisation, in two modes:

- **Approve.** A reusable workflow selects open, human-authored,
  non-draft pull requests with green CI, runs one Copilot CLI review
  session per pull request, and approves the changes that are
  trivial or that a code review finds low risk. It posts nothing
  else: a pull request it cannot approve appears in the run report
  for a human, and the pull request itself stays untouched.
- **Antagonistic.** On dispatch, the same pipeline reads pull
  requests opened by coding bots (Code Monkey, the Copilot coding
  agent) as a hostile reviewer would, and posts its findings as one
  comment review with each finding on its line. It never approves:
  a maintainer still reviews and approves every pull request. A Code
  Monkey pull request with findings goes back to Code Monkey for a
  revision round, for three antagonistic rounds at most; after that
  a maintainer takes over.

## 📚 Documentation

<https://lfreleng-actions.github.io/github-code-review-bot/>

Credential setup, validation and operating notes live in
[`docs/setup/README.md`](docs/setup/README.md). The
[design document](docs/DESIGN.md) covers the architecture, the trust
model, the selection rules, the ledger and the data contracts.

## How it works

```text
select (trusted)       review (untrusted, matrix)      apply (trusted, matrix)
  App read token         model PAT alone                 verify evidence
  ledger + skip rules    packet + head checkout          corroborate, re-read
  selection.json ------> one JSON verdict ------------>  approve or report
```

1. **Select** fetches the prior ledger, scans the organisation (or
   the pull requests named in a dispatch), drops drafts, bot authors,
   red or pending CI, conflicts, pull requests already approved or
   awaiting Copilot, and heads a previous run assessed. It records
   a bounded packet per survivor: metadata, commits, CI, reviews and
   every file's diff.
2. **Review** runs once per pull request on its own runner with the
   model credential alone. The agent reads the packet and an unprivileged
   checkout of the head commit and emits one verdict:
   `trivial`, `low-risk` or `needs-human`, with a summary and findings.
3. **Apply** verifies the evidence, corroborates a `trivial` claim
   with a deterministic classifier, applies hard vetoes (new write
   permissions, unpinned actions, new secrets, `pull_request_target`,
   removed action inputs, `curl | sh`), re-reads the live pull request
   and, on the live path alone, mints a `pull-requests: write` token
   for that one repository and posts an APPROVE review marked as
   automated.
4. **Report** gathers every result into a run report and publishes the
   next run's ledger.

In the antagonistic mode, Select keeps the listed bots' pull
requests instead of humans', the agent emits findings anchored to
diff lines rather than a tier, and Apply checks each anchor against
the diff and posts one `COMMENT` review under a second App that
never approves. A separate job then hands Code Monkey's reviewed pull
requests back to it for revision, under a three-round cap. See
[`docs/DESIGN.md` §19](docs/DESIGN.md).

## Schedule and dispatch

`code-review-cron.yaml` runs every three hours on weekdays and twice
a day at weekends, and stays in dry-run until the rollout in the
design document clears it. A manual dispatch chooses the model, the
approvable tiers, a cap, and either repositories to scan or specific
pull requests to review:

```text
pypi-version-check-action/pull/157, pypi-version-check-action/pull/158
pypi-version-check-action#157 lfreleng-actions/pypi-version-check-action#158
```

`code-review-antagonistic.yaml` runs on dispatch alone, defaulting
to GPT-6 Astra and a dry run; Code Monkey dispatches it for its own
pull requests once their Copilot review is clean:

```bash
gh workflow run code-review-antagonistic.yaml \
  -f dry_run=false -f pull_requests='repo#12'
```

## Reusable workflow

<!-- markdownlint-disable MD013 -->

```yaml
jobs:
  code-review:
    permissions:
      pull-requests: read
      contents: read
      actions: read
    # Pin to an immutable release commit SHA; the tag rides along as a
    # comment. The trusted jobs receive your App private key.
    # yamllint disable-line rule:line-length
    uses: lfreleng-actions/github-code-review-bot/.github/workflows/code-review.yaml@<commit-sha>  # vX.Y.Z
    with:
      org: 'your-org'
      mode: 'approve'  # or 'antagonistic' with approve_tiers: 'none'
      dry_run: true
      approve_tiers: 'trivial,low-risk'
      egress_allow_config: '@<allow-list commit sha>'
      github_app_client_id: ${{ vars.YOUR_APP_CLIENT_ID }}
    secrets:
      copilot_token: ${{ secrets.COPILOT_CLI_TOKEN }}
      github_app_private_key: ${{ secrets.YOUR_APP_PRIVATE_KEY }}
```

<!-- markdownlint-enable MD013 -->

Live runs need a GitHub App installed on the organisation with the
permissions in the setup guide, a second App for the antagonistic
mode, and a personal fine-grained PAT carrying Copilot Requests and
no repository grants.

## Development

```bash
uv run python -B -m unittest discover -s tests -v
prek run --all-files
zizmor --persona auditor .github/workflows/
```
