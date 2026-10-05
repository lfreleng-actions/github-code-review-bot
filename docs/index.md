<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# GitHub Code Review Bot

Scheduled AI review of pull requests. A reusable workflow selects the
open, human-authored pull requests of an organisation, runs one
Copilot CLI review session per pull request against an offline packet
and an unprivileged checkout, and lets a trusted job approve the changes
that are trivial or that a code review finds low risk. The bot posts
nothing else: a pull request it cannot approve appears in the run
report for a human, and the pull request itself stays untouched.

## How a run works

```text
select (trusted)
  | ledger, scan, skip rules, per-PR packet
  | commit SHA, evidence ID, digests
  v
review (untrusted, one runner per pull request)
  | offline packet + head checkout; no App key or installation token
  v
apply (trusted, one runner per pull request)
  | verify evidence, corroborate the verdict, re-read live state
  | approve with a per-repository token, or record for a human
  v
report (trusted)
  | report + the next run's ledger
```

## Tiers

<!-- markdownlint-disable MD013 -->

| Tier          | Meaning                                                                                | Outcome                  |
| ------------- | -------------------------------------------------------------------------------------- | ------------------------ |
| `trivial`     | Pins, lockfiles, documentation, formatting; corroborated by a deterministic classifier | Approved                 |
| `low-risk`    | Logic changed, full review found no security or CI/CD concern and no feedback          | Approved                 |
| `needs-human` | Anything else, including any uncertainty                                               | Reported, nothing posted |

<!-- markdownlint-enable MD013 -->

## Where to go next

- [Setup](setup/README.md): App permissions, secrets, validation,
  going live, targeted reviews and recovery.
- [Design](DESIGN.md): the architecture, trust model, selection
  rules, ledger, inputs and data contracts.
