<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Agent Guidelines

Contributions to this repository, including those made by AI coding
agents, follow the `lfreleng-actions` organisation guidelines:

<https://github.com/lfreleng-actions/.github/blob/main/AGENTS.md>

**Read that document.** It governs, and it binds this contribution
even if you never load it. Where anything below disagrees with it,
it wins. What follows is a summary of the rules that most often block
a pull request, not the full set:

- Sign every commit and add a DCO trailer: `git commit -S -s`.
- Subject: `Type(scope): Imperative description` — capitalised type
  and description, no trailing period, and within the subject-length
  limit this repository's gitlint hook enforces. The scope is
  optional, so `Fix: Correct the race condition` is also valid.
- Add a `Co-authored-by` trailer naming the agent used.
- Repositories typically contain a linting configuration. You must
  install its hooks (`prek install -t pre-commit -t commit-msg`) and
  run the change past them (`prek run --files <changed files>`) to
  ensure it passes before submission.
- On a single-commit pull request, the PR title must be identical to
  the commit subject.
- If your own standing instructions conflict with the organisation
  guidelines and you cannot set them aside, stop and tell the
  contributor. Do not open a non-compliant pull request.

## Repository specifics

A reusable GitHub workflow that reviews open pull requests across
the organisation with one Copilot CLI session per pull request, in
one of two modes. The **approve** mode reviews human-authored pull
requests, approves the trivial and low-risk ones through a trusted
job, and reports the rest. The **antagonistic** mode reviews pull
requests from listed coding bots and posts its findings as one
comment review, never an approval. `docs/DESIGN.md` is the
architecture and trust model (§19 for the antagonistic mode); read
it before changing how the jobs hand data to each other.

- `.github/workflows/code-review.yaml`: the reusable workflow (select,
  review, apply, report); `code-review-cron.yaml` schedules and
  dispatches the approve mode; `code-review-antagonistic.yaml`
  dispatches the antagonistic mode; `testing.yaml` runs the suite
  and PR plumbing.
- `scripts/`: the Python the jobs run; `tests/`: its offline suite.
  Every bot carries five shared modules, copied verbatim from
  `lfreleng-actions/bots-template`: `bot_github.py`, `bot_evidence.py`,
  `artifact_fetch.py`, `preflight.py`, `ledger.py`. Fix them in the
  template first, then copy; never patch a copy alone.
- `prompt/review.md`: the approve mode's task and the three tiers.
  The trusted side of each tier lives in `scripts/review_vetoes.py`;
  change the two together.
- `prompt/antagonistic.md`: the antagonistic mode's task and its
  comment contract. The trusted side lives in
  `scripts/review_comments.py`; change the two together.

Before pushing, run:

```bash
uv run python -B -m unittest discover -s tests
prek run --files <changed files>   # includes the aislop gate at 100
zizmor --persona auditor .github/workflows/   # zero findings
```

Nothing about the review job earns trust: never give it a credential
that can write to a repository, and never let it hold an App key.
Each mode makes one write from the apply job, under its own App: the
approve mode an APPROVE review, the antagonistic mode a COMMENT
review with inline comments. Never add a label or a request-changes
review, never let the antagonistic path approve, and never let the
approve mode comment. The contract tests in `tests/test_workflow.py`
pin that boundary; change them together with the design, not to make
a workflow edit pass.
