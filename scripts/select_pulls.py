# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Choose the pull requests a run will review and write the trusted selection.

Runs in the trusted select job with a read token. Scans the open
pull requests of an organisation, or reads the ones named on the
command line, drops every one a skip rule catches, orders the rest
oldest-updated first and caps the list. For each survivor it records
the head SHA the review job will check out, the reviews and checks
the apply job will re-verify, and the bounded diff the agent reads.

The mode decides whose pull requests qualify. ``approve`` reviews
human authors alone and skips every bot. ``antagonistic`` inverts
that: it reviews the bots the author file lists and skips humans and
every other bot. CI state gates the approve mode alone, since only an
approval relies on it.

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
    load_bot_authors,
    load_exclusions,
    parse_approve_tiers,
    parse_max_pull_requests,
    parse_mode,
    parse_pull_requests,
    parse_repositories,
)
from selection_outputs import write_outputs
from selection_rules import Rules, Skips, author_skip, first_skip

SCHEMA = 1
# GitHub expands at most 256 jobs from one matrix; a larger selection
# would fail at the review job rather than run.
MATRIX_LIMIT = 256
# The verifier refuses a selection.json over 16 MiB. Bodies and
# patches are bounded per pull request, but 256 of them could still
# exceed that, so the cumulative serialised size stops the selection
# first, with headroom for the header fields and indentation.
MAX_SELECTION_BYTES = 12 * 1024 * 1024


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
    rules: Rules,
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
        reason = author_skip(
            reads.require_object(author, "search author")
            if author is not None
            else None,
            mode=rules.mode,
            bot_authors=rules.bot_authors,
        )
        if reason is not None:
            skips.add(candidate, reason)
            continue
        kept.append(candidate)
    return kept, len(pulls)


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
    rules: Rules,
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
        reason = first_skip(state, rules)
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
    mode = parse_mode(args.mode)
    targets = parse_pull_requests(args.pull_requests, args.org)
    explicit = parse_repositories(args.repositories)
    exclusions = set(load_exclusions(args.exclude_file, args.exclude_repos))
    max_pull_requests = parse_max_pull_requests(args.max_pull_requests)
    approve_tiers = parse_approve_tiers(args.approve_tiers)
    bot_authors: list[str] = []
    if mode == "antagonistic":
        # The antagonistic mode can never approve: a tier here is a
        # misconfigured caller, refused rather than ignored.
        if approve_tiers:
            raise SelectionError("the antagonistic mode approves nothing; pass none")
        bot_authors = load_bot_authors(args.bot_authors_file)
        if not bot_authors:
            raise SelectionError("the antagonistic mode needs a bot author list")
    histories: dict[tuple[str, int], list[dict[str, Any]]] = {}

    def history(repository: str, number: int) -> list[dict[str, Any]]:
        """The full review history, read once per pull request."""
        key = (repository, number)
        if key not in histories:
            histories[key] = changes.pull_reviews(repository, number)
        return histories[key]

    rules = Rules(
        mode=mode,
        bot_authors=bot_authors,
        prior=ledger.load_ledger(args.ledger),
        dry_run=bool(args.dry_run),
        reassess=bool(args.reassess),
        bot_login=reads.bot_login(args.bot_slug),
        history=history if mode == "antagonistic" else None,
    )
    skips = Skips()
    if targets:
        candidates = named_candidates(args.org, targets)
        seen = len(candidates)
    else:
        candidates, seen = searched_candidates(
            args.org, explicit, exclusions=exclusions, rules=rules, skips=skips
        )
    survivors = assess(candidates, named=bool(targets), rules=rules, skips=skips)
    chosen = choose(survivors, max_pull_requests=max_pull_requests, skips=skips)
    return {
        "schema": SCHEMA,
        "org": args.org,
        "mode": mode,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dry_run": bool(args.dry_run),
        "model": args.model,
        "approve_tiers": approve_tiers,
        "bot_authors": bot_authors,
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
    parser.add_argument("--mode", default="approve")
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
    parser.add_argument("--bot-authors-file", type=Path)
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
