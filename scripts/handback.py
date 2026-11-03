# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""List the pull requests an antagonistic run hands back for revision.

The report job runs ``list`` and writes the pull requests to dispatch
as one ``owner/repo#N`` list on a single output line. The hand-back
job dispatches that list and holds the one credential that can.

Result files nominate candidates and decide nothing: an artifact is
found by name, so a compromised review runner could upload one. Each
candidate must also be in the digest-verified selection, at the head
the selection recorded, by a bot the selection lists as taking
hand-backs; and this App's own review of that head, read from GitHub,
must end with the hand-back marker the trusted applier of this run
appends. The review runner holds no App token and cannot post that
review, and a review another run posted names that run, not this one.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, cast

import bot_github as github
from bot_evidence import MAX_EVIDENCE_BYTES, read_regular
from selection_inputs import bot_identity

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MARKER_RE = re.compile(
    r"<!-- antagonistic-handback round=[1-9][0-9]* head=(?P<head>[0-9a-f]{40})"
    r" run=(?P<run>[1-9][0-9]*) -->\Z"
)


def load_object(path: Path, context: str) -> dict[str, Any]:
    """Read a bounded JSON file that must hold an object."""
    parsed: Any = json.loads(read_regular(path, MAX_EVIDENCE_BYTES))
    if not isinstance(parsed, dict):
        raise ValueError(f"{context} is not an object")
    return cast("dict[str, Any]", parsed)


def candidates(report: dict[str, Any], org: str) -> list[tuple[str, int]]:
    """``(repository, number)`` of each result that claims a posted hand-back."""
    found: list[tuple[str, int]] = []
    for item in cast("list[Any]", report.get("results") or []):
        if not isinstance(item, dict):
            continue
        result = cast("dict[str, Any]", item)
        if result.get("verdict") != "commented" or result.get("handback") is not True:
            continue
        repository, number = result.get("repository"), result.get("number")
        if not isinstance(repository, str) or type(number) is not int or number <= 0:
            continue
        owner, _, name = repository.partition("/")
        if owner.lower() != org.lower() or not NAME_RE.fullmatch(name):
            continue
        if (repository, number) not in found:
            found.append((repository, number))
    return found


def selected_heads(selection: dict[str, Any]) -> dict[tuple[str, int], str]:
    """The head the selection recorded for each pull request a hand-back may name."""
    authors = [
        str(name) for name in cast("list[Any]", selection.get("handback_authors") or [])
    ]
    heads: dict[tuple[str, int], str] = {}
    for item in cast("list[Any]", selection.get("pull_requests") or []):
        if not isinstance(item, dict):
            continue
        entry = cast("dict[str, Any]", item)
        author, head = entry.get("author"), entry.get("head_sha")
        repository, number = entry.get("repository"), entry.get("number")
        if (
            isinstance(author, str)
            and bot_identity(author) in authors
            and isinstance(head, str)
            and github.SHA_RE.fullmatch(head)
            and isinstance(repository, str)
            and type(number) is int
        ):
            heads[(repository, number)] = head
    return heads


def marked(
    repository: str, number: int, bot_login: str, head: str, run_id: int
) -> bool:
    """Whether this App's review of ``head`` ends with this run's hand-back marker.

    The run in the marker must be this one: two runs can hold the same
    head, and the one whose applier refused the head must not dispatch
    on the strength of the other's review.
    """
    own = bot_identity(bot_login)
    for review in github.api_list(
        f"repos/{repository}/pulls/{number}/reviews?per_page=100"
    ):
        user = review.get("user")
        data = cast("dict[str, Any]", user) if isinstance(user, dict) else {}
        login, body = data.get("login"), review.get("body")
        if (
            data.get("type") == "Bot"
            and isinstance(login, str)
            and bot_identity(login) == own
            and review.get("commit_id") == head
            and isinstance(body, str)
        ):
            found = MARKER_RE.search(body.rstrip())
            if (
                found is not None
                and found.group("head") == head
                and found.group("run") == str(run_id)
            ):
                return True
    return False


def handbacks(
    report: dict[str, Any], selection: dict[str, Any], org: str, run_id: int
) -> list[str]:
    """The ``owner/repo#N`` of each nominated hand-back that GitHub confirms."""
    bot_login = selection.get("bot_login")
    if not isinstance(bot_login, str) or not bot_login:
        return []
    heads = selected_heads(selection)
    confirmed: list[str] = []
    for repository, number in candidates(report, org):
        head = heads.get((repository, number))
        if head is not None and marked(repository, number, bot_login, head, run_id):
            confirmed.append(f"{repository}#{number}")
    return confirmed


def main(argv: list[str] | None = None) -> None:
    """Print ``pulls=`` and ``count=`` lines for the workflow outputs."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="the hand-backs GitHub confirms")
    listing.add_argument("--report", type=Path, required=True)
    listing.add_argument("--selection", type=Path, required=True)
    listing.add_argument("--org", required=True)
    listing.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args(argv)
    if not NAME_RE.fullmatch(args.org):
        parser.error("--org must be an owner name")
    if args.run_id <= 0:
        parser.error("--run-id must be a positive integer")
    try:
        report = load_object(args.report, "the report")
        selection = load_object(args.selection, "the selection")
        refs = handbacks(report, selection, args.org, args.run_id)
    except (OSError, ValueError, RecursionError, github.GitHubError) as exc:
        parser.exit(1, f"handback: {github.safe_message(exc)}\n")
    print(f"pulls={' '.join(refs)}")
    print(f"count={len(refs)}")
    print(f"handback: {len(refs)} pull request(s) to hand back", file=sys.stderr)


if __name__ == "__main__":
    main()
