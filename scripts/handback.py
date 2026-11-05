# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""List the pull requests an antagonistic run hands back for revision.

The report job runs ``list`` and writes the pull requests to dispatch
as one ``owner/repo#N`` list on a single output line, with a record of
each confirmed hand-back: the review that requested it, the head it
reviewed, and whether it is still to dispatch or why it is skipped. The hand-back job
dispatches that list and holds the one credential that can.

Result files nominate candidates and decide nothing: an artifact is
found by name, so a compromised review runner could upload one. Each
candidate must also be in the digest-verified selection, at the head
the selection recorded, by a bot the selection lists as taking
hand-backs; and this App's own review of that head, read from GitHub,
must end with the hand-back marker the trusted applier of this run
appends. The review runner holds no App token and cannot post that
review, and a review another run posted names that run, not this one.
A candidate whose reviews cannot be read is skipped, never dispatched.

A confirmed hand-back is dropped from the dispatch list when it needs
no delivery: the pull request's head has moved past the reviewed one,
or the Code Monkey issue already carries a round footer that answers
the review. A read that fails there keeps the pull request in the list
with a warning: a duplicate dispatch costs one session that Code
Monkey refuses, while a dropped one costs a revision.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
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
# Code Monkey works an issue on this branch of its pull request.
ISSUE_BRANCH_RE = re.compile(r"^code-monkey/issue-(?P<issue>[1-9][0-9]*)\Z")
# A Code Monkey round footer, the last line of its issue comment, that
# names the antagonistic review the round answered.
ANSWERED_RE = re.compile(
    r"(?:^|\n)<!-- code-monkey \S+ [^\n]*?\bantagonistic=(?P<review>[0-9]+)"
    r" -->\s*\Z"
)


@dataclass(frozen=True)
class Handback:
    """One confirmed hand-back: the pull request, its head, and the review."""

    repository: str
    number: int
    head: str
    author: str
    review: int

    @property
    def ref(self) -> str:
        """The ``owner/repo#N`` the dispatch names."""
        return f"{self.repository}#{self.number}"


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


def selected_heads(
    selection: dict[str, Any],
) -> dict[tuple[str, int], tuple[str, str]]:
    """The head and author the selection recorded for each pull request a
    hand-back may name.
    """
    authors = [
        str(name) for name in cast("list[Any]", selection.get("handback_authors") or [])
    ]
    heads: dict[tuple[str, int], tuple[str, str]] = {}
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
            heads[(repository, number)] = (head, author)
    return heads


def marked(
    repository: str, number: int, bot_login: str, head: str, run_id: int
) -> int | None:
    """The id of this App's review of ``head`` that ends with this run's
    hand-back marker, or None when there is none.

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
            review_id = review.get("id")
            if (
                found is not None
                and found.group("head") == head
                and found.group("run") == str(run_id)
                and type(review_id) is int
                and review_id > 0
            ):
                return review_id
    return None


def handbacks(
    report: dict[str, Any], selection: dict[str, Any], org: str, run_id: int
) -> list[Handback]:
    """Each nominated hand-back that GitHub confirms, with its review.

    A candidate whose reviews cannot be read (a private repository the
    native token cannot see, a 5xx, a rate limit) is not confirmed: it
    is skipped with a warning, and the rest of the list still stands.
    """
    bot_login = selection.get("bot_login")
    if not isinstance(bot_login, str) or not bot_login:
        return []
    heads = selected_heads(selection)
    confirmed: list[Handback] = []
    for repository, number in candidates(report, org):
        selected = heads.get((repository, number))
        if selected is None:
            continue
        head, author = selected
        ref = f"{repository}#{number}"
        try:
            found = marked(repository, number, bot_login, head, run_id)
        except github.GitHubError as exc:
            print(
                f"::warning::handback: {ref} not handed back; its reviews"
                f" could not be read: {github.safe_message(exc)}",
                file=sys.stderr,
            )
            continue
        if found is not None:
            confirmed.append(Handback(repository, number, head, author, found))
    return confirmed


def answered(handback: Handback, issue: int) -> bool:
    """Whether the Code Monkey issue holds a round footer, by the pull
    request's author, that answers this hand-back's review.
    """
    own = bot_identity(handback.author)
    for comment in github.api_list(
        f"repos/{handback.repository}/issues/{issue}/comments?per_page=100"
    ):
        user = comment.get("user")
        data = cast("dict[str, Any]", user) if isinstance(user, dict) else {}
        login, body = data.get("login"), comment.get("body")
        if (
            data.get("type") == "Bot"
            and isinstance(login, str)
            and bot_identity(login) == own
            and isinstance(body, str)
        ):
            found = ANSWERED_RE.search(body)
            if found is not None and found.group("review") == str(handback.review):
                return True
    return False


def skip_reason(handback: Handback) -> str | None:
    """Why a confirmed hand-back needs no dispatch, or None to dispatch it.

    Raises ``GitHubError`` when a read fails or GitHub answers in a shape
    that cannot be judged; the caller then keeps the pull request.
    """
    context = handback.ref
    pull = github.api_object(f"repos/{handback.repository}/pulls/{handback.number}")
    head = pull.get("head")
    if not isinstance(head, dict):
        raise github.GitHubError(f"{context}: missing or invalid 'head'")
    data = cast("dict[str, Any]", head)
    sha = github.require_sha(data, "sha", context)
    if sha != handback.head:
        return f"the head moved from {handback.head[:12]} to {sha[:12]}"
    branch = data.get("ref")
    found = ISSUE_BRANCH_RE.fullmatch(branch) if isinstance(branch, str) else None
    if found is None:
        return None
    issue = int(found.group("issue"))
    if answered(handback, issue):
        return f"issue #{issue} already answers review {handback.review}"
    return None


def deliveries(confirmed: list[Handback]) -> list[dict[str, Any]]:
    """One delivery record per hand-back, with its review and reviewed head:
    ``pending`` or ``skipped``.
    """
    records: list[dict[str, Any]] = []
    for handback in confirmed:
        record: dict[str, Any] = {
            "pull": handback.ref,
            "review": handback.review,
            "head": handback.head,
        }
        try:
            reason = skip_reason(handback)
        except github.GitHubError as exc:
            print(
                f"::warning::handback: {handback.ref} kept; whether it still"
                f" needs a revision could not be read: {github.safe_message(exc)}",
                file=sys.stderr,
            )
            reason = None
        if reason is None:
            record["state"] = "pending"
        else:
            record.update(state="skipped", reason=reason)
            print(
                f"::notice::handback: {handback.ref} skipped; {reason}",
                file=sys.stderr,
            )
        records.append(record)
    return records


def main(argv: list[str] | None = None) -> None:
    """Print ``pulls=``, ``count=`` and ``records=`` lines for the outputs."""
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
        records = deliveries(handbacks(report, selection, args.org, args.run_id))
    except (OSError, ValueError, RecursionError, github.GitHubError) as exc:
        parser.exit(1, f"handback: {github.safe_message(exc)}\n")
    refs = [str(record["pull"]) for record in records if record["state"] == "pending"]
    print(f"pulls={' '.join(refs)}")
    print(f"count={len(refs)}")
    print(f"records={json.dumps(records, separators=(',', ':'))}")
    print(f"handback: {len(refs)} pull request(s) to hand back", file=sys.stderr)


if __name__ == "__main__":
    main()
