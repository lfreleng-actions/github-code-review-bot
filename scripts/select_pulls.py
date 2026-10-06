# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Choose the pull requests a run will review and write the trusted selection.

Runs in the trusted select job with a read token. Scans the open
pull requests of an organisation, or reads the ones named on the
command line, drops every one a skip rule catches, orders the rest
oldest-updated first and caps the list. For each survivor it records
the head SHA the review job will check out, the reviews and checks
the apply job will re-verify, and the bounded diff the agent reads.

Named pull requests bypass the exclusion list and the search alone;
every other rule still applies to them. When both pull requests and
repositories are named, the pull requests win and the repository
scope is recorded but not searched.

Outputs in ``--output-dir``: ``selection.json``, ``matrix.json``,
``excluded-repos.txt`` and ``selection-summary.md``. The prior ledger
is read, never written.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import bot_github as github
import ledger
import pull_changes as changes
import pull_reads as reads
from pull_reads import SelectionError
from selection_inputs import (
    load_exclusions,
    parse_approve_tiers,
    parse_max_pull_requests,
    parse_pull_requests,
    parse_repositories,
)
from selection_outputs import write_outputs

SCHEMA = 1
# GitHub expands at most 256 jobs from one matrix; a larger selection
# would fail at the review job rather than run.
MATRIX_LIMIT = 256
# The verifier refuses a selection.json over 16 MiB. Bodies and
# patches are bounded per pull request, but 256 of them could still
# exceed that, so the cumulative serialised size stops the selection
# first, with headroom for the header fields and indentation.
MAX_SELECTION_BYTES = 12 * 1024 * 1024
SKIP_REASONS = (
    "repository",
    "bot_author",
    "closed",
    "draft",
    "conflicting",
    "changes_requested",
    "already_approved",
    "copilot_pending",
    "copilot_feedback",
    "ci_pending",
    "ci_failing",
    "ci_none",
    "already_assessed",
    "too_large",
    "cap",
)


class Skips:
    """The pull requests a run left out: a count per reason and one row each."""

    def __init__(self) -> None:
        """Start every reason at zero so the output names them all."""
        self.counts: dict[str, int] = dict.fromkeys(SKIP_REASONS, 0)
        self.rows: list[dict[str, Any]] = []

    def add(self, candidate: dict[str, Any], reason: str) -> None:
        """Record one skipped pull request."""
        self.counts[reason] += 1
        self.rows.append(
            {
                "repository": candidate["repository"],
                "number": candidate["number"],
                "url": candidate["url"],
                "title": candidate.get("title"),
                "reason": reason,
            }
        )


def named_candidates(org: str, targets: list[tuple[str, int]]) -> list[dict[str, Any]]:
    """Candidates for the pull requests named on the command line."""
    return [
        {
            "repository": f"{org}/{name}",
            "number": number,
            "url": f"https://github.com/{org}/{name}/pull/{number}",
            "title": None,
        }
        for name, number in targets
    ]


def searched_candidates(
    org: str,
    repositories: list[str],
    *,
    exclusions: set[str],
    skips: Skips,
) -> tuple[list[dict[str, Any]], int]:
    """Search the organisation and apply the rules the payload alone answers.

    Returns the surviving candidates and how many the search returned.
    """
    pulls = reads.search_open_pulls(org, repositories)
    kept: list[dict[str, Any]] = []
    for pull in pulls:
        repo_data = reads.require_object(pull.get("repository"), "search repository")
        full = github.require_str(repo_data, "nameWithOwner", "search repository")
        owner, _, name = full.partition("/")
        candidate = {
            "repository": full,
            "number": github.require_int(pull, "number", "search pull request"),
            "url": github.require_str(pull, "url", "search pull request"),
            "title": reads.optional_str(pull.get("title"), "search pull request"),
        }
        if owner.lower() != org.lower() or name.lower() in exclusions:
            skips.add(candidate, "repository")
            continue
        author = pull.get("author")
        if reads.is_bot_author(
            reads.require_object(author, "search author")
            if author is not None
            else None
        ):
            skips.add(candidate, "bot_author")
            continue
        kept.append(candidate)
    return kept, len(pulls)


def first_skip(
    state: dict[str, Any],
    *,
    prior: dict[str, Any],
    dry_run: bool,
    reassess: bool,
) -> str | None:
    """The first skip rule the pull request's state trips, in table order."""
    if state["repository_archived"] or state["head_repository"] is None:
        return "repository"
    if reads.is_bot_author(state["author"]):
        return "bot_author"
    if state["state"] != "OPEN":
        return "closed"
    if state["is_draft"]:
        return "draft"
    if state["mergeable"] == "CONFLICTING":
        return "conflicting"
    if state["changes_requested"]:
        return "changes_requested"
    if state["approved_by"]:
        return "already_approved"
    if any(reads.is_copilot(login) for login in state["review_requests"]):
        return "copilot_pending"
    if state["unresolved_copilot_threads"]:
        return "copilot_feedback"
    if state["ci"] == "pending":
        return "ci_pending"
    if state["ci"] == "failure":
        return "ci_failing"
    if state["ci"] == "none":
        return "ci_none"
    if not reassess and ledger.already_assessed(
        prior, state["repository"], state["number"], state["head_sha"], dry_run=dry_run
    ):
        return "already_assessed"
    return None


def read_state(candidate: dict[str, Any], *, named: bool) -> dict[str, Any]:
    """Read one candidate's state, treating an unreadable named one as operator error."""
    try:
        return reads.pull_state(candidate["repository"], candidate["number"])
    except github.GitHubError as exc:
        if not named:
            raise
        raise SelectionError(
            f"named pull request {candidate['repository']}#{candidate['number']} "
            f"cannot be read: {exc}"
        ) from exc


def assess(
    candidates: list[dict[str, Any]],
    *,
    named: bool,
    prior: dict[str, Any],
    dry_run: bool,
    reassess: bool,
    skips: Skips,
) -> list[dict[str, Any]]:
    """Read each candidate and keep the ones no skip rule catches.

    Each survivor carries its state plus the files and commits the
    entry needs; the diff is read last because it is the costly part.
    """
    survivors: list[dict[str, Any]] = []
    for candidate in candidates:
        state = read_state(candidate, named=named)
        current = {**candidate, **{k: state[k] for k in ("repository", "url", "title")}}
        reason = first_skip(state, prior=prior, dry_run=dry_run, reassess=reassess)
        if reason is not None:
            skips.add(current, reason)
            continue
        files, diff_bytes, too_large = changes.pull_files(
            state["repository"], state["number"]
        )
        if too_large:
            skips.add(current, "too_large")
            continue
        survivors.append(
            {
                **state,
                "files": files,
                "diff_bytes": diff_bytes,
                "commits": changes.pull_commits(state["repository"], state["number"]),
            }
        )
    return survivors


def build_entry(state: dict[str, Any]) -> dict[str, Any]:
    """The per-pull-request entry the later jobs consume."""
    return {
        "key": f"{state['repo_name']}-{state['number']}",
        "repository": state["repository"],
        "repo_name": state["repo_name"],
        "number": state["number"],
        "url": state["url"],
        "title": state["title"],
        "body": state["body"],
        "author": state["author"]["login"],
        "author_association": state["author_association"],
        "head_sha": state["head_sha"],
        "head_repository": state["head_repository"],
        "head_ref": state["head_ref"],
        "base_ref": state["base_ref"],
        "base_sha": state["base_sha"],
        "is_fork": state["is_fork"],
        "labels": state["labels"],
        "created_at": state["created_at"],
        "updated_at": state["updated_at"],
        "ci": state["ci"],
        "checks": state["checks"],
        "reviews": state["reviews"],
        "commits": state["commits"],
        "files": state["files"],
        "diff_bytes": state["diff_bytes"],
    }


def order_key(state: dict[str, Any]) -> tuple[str, str, int]:
    """Oldest update first, then a stable name for determinism."""
    return (str(state["updated_at"]), str(state["repository"]), int(state["number"]))


def choose(
    survivors: list[dict[str, Any]], *, max_pull_requests: int, skips: Skips
) -> list[dict[str, Any]]:
    """Cap the survivors by count and by serialised size, then build the entries."""
    cap = min(max_pull_requests, MATRIX_LIMIT) if max_pull_requests else MATRIX_LIMIT
    used_bytes = 0
    chosen: list[dict[str, Any]] = []
    for state in sorted(survivors, key=order_key):
        entry = build_entry(state)
        size = len(json.dumps(entry, indent=2).encode("utf-8"))
        if len(chosen) >= cap or used_bytes + size > MAX_SELECTION_BYTES:
            skips.add(state, "cap")
            continue
        used_bytes += size
        chosen.append(entry)
    return sorted(
        chosen, key=lambda entry: (str(entry["repository"]), int(entry["number"]))
    )


def build_selection(args: argparse.Namespace) -> dict[str, Any]:
    """Run the whole selection and return it."""
    targets = parse_pull_requests(args.pull_requests, args.org)
    explicit = parse_repositories(args.repositories)
    exclusions = set(load_exclusions(args.exclude_file, args.exclude_repos))
    max_pull_requests = parse_max_pull_requests(args.max_pull_requests)
    approve_tiers = parse_approve_tiers(args.approve_tiers)
    prior = ledger.load_ledger(args.ledger)
    skips = Skips()
    if targets:
        candidates = named_candidates(args.org, targets)
        seen = len(candidates)
    else:
        candidates, seen = searched_candidates(
            args.org, explicit, exclusions=exclusions, skips=skips
        )
    survivors = assess(
        candidates,
        named=bool(targets),
        prior=prior,
        dry_run=bool(args.dry_run),
        reassess=bool(args.reassess),
        skips=skips,
    )
    chosen = choose(survivors, max_pull_requests=max_pull_requests, skips=skips)
    return {
        "schema": SCHEMA,
        "org": args.org,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dry_run": bool(args.dry_run),
        "model": args.model,
        "approve_tiers": approve_tiers,
        "bot_login": reads.bot_login(args.bot_slug),
        "explicit_pull_requests": [
            f"{args.org}/{name}#{number}" for name, number in targets
        ],
        "explicit_repositories": explicit,
        "exclusions": sorted(exclusions),
        "candidates_seen": seen,
        "skip_counts": skips.counts,
        "skipped": skips.rows,
        "pull_requests": chosen,
    }


def build_parser() -> argparse.ArgumentParser:
    """Describe the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--approve-tiers", required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reassess", action="store_true")
    parser.add_argument("--pull-requests", default="")
    parser.add_argument("--repositories", default="")
    parser.add_argument("--exclude-file", type=Path)
    parser.add_argument("--exclude-repos", default="")
    parser.add_argument("--max-pull-requests", default="10")
    parser.add_argument("--bot-slug", default="")
    return parser


def main(argv: list[str] | None = None) -> None:
    """Run the selection and write its outputs."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        selection = build_selection(args)
        write_outputs(args.output_dir, selection)
    except (
        OSError,
        ValueError,
        SelectionError,
        github.GitHubError,
        ledger.LedgerError,
    ) as exc:
        parser.exit(1, f"select pulls: {github.safe_message(exc)}\n")
    print(
        f"Selected {len(selection['pull_requests'])} pull request(s) from "
        f"{selection['candidates_seen']} open; skipped {selection['skip_counts']}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
