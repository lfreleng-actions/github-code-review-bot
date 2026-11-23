# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tell a pull request that the antagonistic review it was owed did not land.

``list`` runs in a trusted job whose token reads public repositories
and writes nothing. It names the pull requests a live run was asked to
review and left without a review of their head, and prints ``count=``,
``pages=`` and ``matrix=`` lines for the outputs; everything else goes
to stderr. One matrix runs at most 256 jobs, so the candidates the
run's input names are sorted and split into pages of 256 before any
live read, and ``--page`` picks one: a page holds the same candidates
however often it is listed, so the bound loses no notice owed.
``post`` runs once per matrix entry with a token that can write that
one repository's pull requests. It re-reads the pull request and posts
one issue comment, never a review: a review would count as one of the
pull request's rounds and mark the head reviewed, so the head could
never get the review it is still owed.

The comment holds fixed text and values this script validated: the
head, the run URL, the two repository names, the bot login and the
reason. Nothing from the pull request, a session or a result reaches
it. It ends with a hidden marker naming the reason, the head and the
run, which keeps a second notice for the same head and reason from
being posted, and recovers a write whose reply was lost.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, cast

import bot_evidence as evidence
import bot_github as github
import pull_changes
import pull_reads
import review_files as files
import review_recovery as recovery
from notice_text import MARKER_RE, NOTIFYING, SENTENCES, notice_body
from pull_reads import SelectionError
from select_pulls import MATRIX_LIMIT
from selection_inputs import (
    BOT_LOGIN_RE,
    bot_identity,
    load_bot_authors,
    parse_pull_requests,
)
from selection_rules import author_skip, reviewed_by

MODE = "antagonistic"
SOURCE = "review_notice"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# The one endpoint this module writes to.
COMMENTS_RE = re.compile(
    r"repos/[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+/issues/[1-9][0-9]*/comments"
)


def warn(message: str) -> None:
    """A workflow warning on stderr, so stdout stays a clean list of outputs."""
    print(f"::warning::{SOURCE}: {message}", file=sys.stderr)


def in_org(repository: object, org: str) -> bool:
    """Whether ``repository`` is a well-formed ``owner/name`` in ``org``."""
    if not isinstance(repository, str) or not github.REPO_RE.fullmatch(repository):
        return False
    owner, _, name = repository.partition("/")
    return owner.lower() == org.lower() and name not in (".", "..")


def target(row: object, org: str) -> tuple[str, int, str] | None:
    """The repository, number and head of a selection row, or None."""
    if not isinstance(row, dict):
        return None
    data = cast("dict[str, Any]", row)
    repository, number, head = (
        data.get("repository"),
        data.get("number"),
        data.get("head_sha"),
    )
    if (
        not in_org(repository, org)
        or type(number) is not int
        or number <= 0
        or not isinstance(head, str)
        or not github.SHA_RE.fullmatch(head)
    ):
        return None
    return str(repository), number, head


def entry(repository: str, number: int, head: str, reason: str) -> dict[str, Any]:
    """One matrix entry, keyed as the selection keys its entries."""
    name = repository.partition("/")[2]
    return {
        "key": f"{name}-{number}",
        "repository": repository,
        "repo_name": name,
        "number": number,
        "head_sha": head,
        "reason": reason,
    }


def rows(value: object) -> list[Any]:
    """A selection list field, or empty when it is not a list."""
    return cast("list[Any]", value) if isinstance(value, list) else []


def page_count(total: int) -> int:
    """How many pages of ``MATRIX_LIMIT`` candidates ``total`` fills."""
    return -(-total // MATRIX_LIMIT)


def page_of(items: list[Any], page: int) -> list[Any]:
    """Page ``page`` of ``items``, empty past the last."""
    return items[page * MATRIX_LIMIT : (page + 1) * MATRIX_LIMIT]


def reviewed(item: dict[str, Any], bot_login: str) -> bool:
    """Whether this App reviewed the entry's head; an unreadable history says no."""
    repository, number = item["repository"], item["number"]
    try:
        history = pull_changes.pull_reviews(repository, number)
    except github.GitHubError as exc:
        warn(
            f"{repository}#{number} kept; its reviews could not be read:"
            f" {github.safe_message(exc)}"
        )
        return False
    return reviewed_by(history, bot_login, item["head_sha"])


def from_selection(
    selection: dict[str, Any], org: str, page: int
) -> tuple[list[dict[str, Any]], int]:
    """The notices a succeeded selection owes on ``page``, and the page count.

    The candidates come from the selection alone, so a page holds the
    same ones each time it is listed. Each selected entry without this
    App's review of its head is owed ``no_review``; a history that
    cannot be read keeps the entry, since the selection vouches for it
    and ``post`` reads again. A named run's skipped rows with a
    notifying reason are owed that reason; a scan's are owed nothing,
    since nobody asked for them. Selection never both picks and skips
    one pull request.
    """
    files.require_mode(selection, MODE, "selection")
    if selection.get("dry_run") is not False:
        print(f"{SOURCE}: a dry run owes no notice", file=sys.stderr)
        return [], 0
    candidates: list[dict[str, Any]] = []
    for row in rows(selection.get("pull_requests")):
        picked = target(row, org)
        if picked is not None:
            candidates.append(entry(*picked, "no_review"))
    if rows(selection.get("explicit_pull_requests")):
        for row in rows(selection.get("skipped")):
            picked = target(row, org)
            reason = cast("dict[str, Any]", row).get("reason") if picked else None
            if picked is not None and reason in NOTIFYING:
                candidates.append(entry(*picked, str(reason)))
    candidates = unique(candidates)
    bot_login = selection.get("bot_login")
    own = bot_login if isinstance(bot_login, str) and bot_login else None
    found = [
        item
        for item in page_of(candidates, page)
        if item["reason"] != "no_review" or own is None or not reviewed(item, own)
    ]
    return found, page_count(len(candidates))


def from_names(
    text: str, org: str, authors: list[str], page: int
) -> tuple[list[dict[str, Any]], int]:
    """The notices a failed selection owes the names on ``page``, and the page count.

    The names are sorted and paged before any read, so a page holds
    the same ones each time it is listed. Each on the page is read
    once. Only an open pull request by a listed bot is owed
    ``selection_failed``, at the head just read; nothing vouches for
    any other, so it is left out with a warning.
    """
    names = sorted(parse_pull_requests(text, org))
    found: list[dict[str, Any]] = []
    for name, number in page_of(names, page):
        ref = f"{org}/{name}#{number}"
        try:
            state = pull_reads.pull_state(f"{org}/{name}", number)
        except github.GitHubError as exc:
            warn(f"{ref} left out; it could not be read: {github.safe_message(exc)}")
            continue
        if state["state"] != "OPEN" or author_skip(
            state["author"], mode=MODE, bot_authors=authors
        ):
            warn(f"{ref} left out; it is not an open pull request by a listed bot")
            continue
        found.append(
            entry(
                str(state["repository"]), number, state["head_sha"], "selection_failed"
            )
        )
    return found, page_count(len(names))


def unique(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per pull request, the first kept, sorted by repository and number."""
    kept: dict[tuple[str, int], dict[str, Any]] = {}
    for item in found:
        kept.setdefault((str(item["repository"]).lower(), int(item["number"])), item)
    return sorted(kept.values(), key=lambda item: (item["repository"], item["number"]))


def own_notice(
    repository: str,
    number: int,
    bot_login: str,
    head: str,
    reason: str,
    run_id: int | None = None,
) -> dict[str, Any] | None:
    """This App's notice for ``head`` and ``reason``, or None.

    Only a comment GitHub types as a bot's, whose login folds to this
    App's and whose body ends with the marker, counts: a human who
    copies the marker suppresses nothing. With ``run_id`` the marker
    must name that run too. A failed read raises.
    """
    own = bot_identity(bot_login)
    for comment in github.api_list(
        f"repos/{repository}/issues/{number}/comments?per_page=100"
    ):
        user = comment.get("user")
        data = cast("dict[str, Any]", user) if isinstance(user, dict) else {}
        login, body = data.get("login"), comment.get("body")
        if not (
            data.get("type") == "Bot"
            and isinstance(login, str)
            and bot_identity(login) == own
            and isinstance(body, str)
        ):
            continue
        found = MARKER_RE.search(body.rstrip())
        if (
            found is not None
            and found.group("reason") == reason
            and found.group("head") == head
            and (run_id is None or found.group("run") == str(run_id))
        ):
            return comment
    return None


def why_not(args: argparse.Namespace, bot_login: str, authors: list[str]) -> str | None:
    """The first condition the live pull request fails, or None to post."""
    repository, number, head = args.repository, args.number, args.head
    state = pull_reads.pull_state(repository, number)
    if state["state"] != "OPEN":
        return f"the pull request is {str(state['state']).lower()}"
    if author_skip(state["author"], mode=MODE, bot_authors=authors) is not None:
        return "its author is not a listed bot"
    if state["head_sha"] != head:
        return f"its head moved from {head[:12]} to {str(state['head_sha'])[:12]}"
    if reviewed_by(pull_changes.pull_reviews(repository, number), bot_login, head):
        return f"this App has reviewed head {head[:12]}"
    if own_notice(repository, number, bot_login, head, args.reason) is not None:
        return f"a {args.reason} notice for head {head[:12]} is already posted"
    return None


def post(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Send one issue comment, refusing any other endpoint before it leaves.

    The one place this module writes, so a call built some other way
    still cannot review, approve or label through it.
    """
    if not COMMENTS_RE.fullmatch(endpoint):
        raise ValueError("a review notice posts an issue comment alone")
    return github.api_write("POST", endpoint, payload)


def run_post(args: argparse.Namespace) -> str:
    """Post the notice, or say why not; return the line that records it."""
    authors = load_bot_authors(args.bot_authors_file)
    if not authors:
        raise SelectionError("a review notice needs a bot author list")
    bot_login = f"{args.bot_slug}[bot]"
    ref = f"{args.repository}#{args.number}"
    reason = why_not(args, bot_login, authors)
    if reason is not None:
        return f"{SOURCE}: no notice on {ref}: {reason}"
    payload = {
        "body": notice_body(
            args.reason,
            args.head,
            bot_login,
            args.run_url,
            args.run_id,
            args.workflow_repository,
        )
    }
    endpoint = f"repos/{args.repository}/issues/{args.number}/comments"
    recovery.post_once(
        lambda: post(endpoint, payload),
        lambda: own_notice(
            args.repository, args.number, bot_login, args.head, args.reason, args.run_id
        ),
        f"notice on {ref}",
    )
    return f"{SOURCE}: posted a {args.reason} notice on {ref} for head {args.head[:12]}"


def run_list(args: argparse.Namespace) -> None:
    """Print the ``count=``, ``pages=`` and ``matrix=`` lines of one page owed."""
    authors = load_bot_authors(args.bot_authors_file)
    if not authors:
        raise SelectionError("a review notice needs a bot author list")
    if args.selection is not None:
        selection = files.load_json(
            args.selection, evidence.MAX_EVIDENCE_BYTES, "selection"
        )
        found, pages = from_selection(selection, args.org, args.page)
    else:
        found, pages = from_names(args.pull_requests, args.org, authors, args.page)
    owed = unique(found)
    print(f"count={len(owed)}")
    print(f"pages={pages}")
    print(f"matrix={json.dumps({'include': owed}, separators=(',', ':'))}")
    print(
        f"{SOURCE}: {len(owed)} pull request(s) owed a notice on page"
        f" {args.page} of {pages}",
        file=sys.stderr,
    )


def build_parser() -> argparse.ArgumentParser:
    """Describe the list and post commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="the pull requests owed a notice")
    listing.add_argument("--org", required=True)
    listing.add_argument("--bot-authors-file", type=Path, required=True)
    source = listing.add_mutually_exclusive_group(required=True)
    source.add_argument("--selection", type=Path)
    source.add_argument("--pull-requests")
    listing.add_argument("--page", type=int, default=0)
    posting = commands.add_parser("post", help="post one notice or say why not")
    posting.add_argument("--repository", required=True)
    posting.add_argument("--number", type=int, required=True)
    posting.add_argument("--head", required=True)
    posting.add_argument("--reason", required=True)
    posting.add_argument("--bot-slug", required=True)
    posting.add_argument("--bot-authors-file", type=Path, required=True)
    posting.add_argument("--run-id", type=int, required=True)
    posting.add_argument("--run-url", required=True)
    posting.add_argument("--workflow-repository", required=True)
    posting.add_argument("--summary", type=Path)
    return parser


def usage_error(args: argparse.Namespace) -> str | None:
    """What makes the command line unusable, or None."""
    if args.command == "list":
        if not NAME_RE.fullmatch(args.org):
            return "--org must be an owner name"
        return None if args.page >= 0 else "--page must be zero or more"
    if not in_org(args.repository, args.repository.partition("/")[0]):
        return "--repository must be OWNER/REPO"
    if not in_org(args.workflow_repository, args.workflow_repository.partition("/")[0]):
        return "--workflow-repository must be OWNER/REPO"
    if args.number <= 0 or args.run_id <= 0:
        return "--number and --run-id must be positive integers"
    if not github.SHA_RE.fullmatch(args.head):
        return "--head must be a commit SHA"
    if args.reason not in SENTENCES:
        return f"--reason must be one of {', '.join(sorted(SENTENCES))}"
    if not BOT_LOGIN_RE.fullmatch(args.bot_slug):
        return "--bot-slug must be an App slug"
    expected = (
        f"https://github.com/{args.workflow_repository}/actions/runs/{args.run_id}"
    )
    if args.run_url != expected:
        return f"--run-url must be {expected}"
    return None


def main(argv: list[str] | None = None) -> None:
    """Dispatch a command; a GitHub or input failure exits 1."""
    parser = build_parser()
    args = parser.parse_args(argv)
    problem = usage_error(args)
    if problem is not None:
        parser.error(problem)
    try:
        if args.command == "list":
            run_list(args)
            return
        line = run_post(args)
        print(line)
        if args.summary is not None:
            with args.summary.open("a", encoding="utf-8") as summary:
                summary.write(line + "\n")
    except (OSError, ValueError, SelectionError, github.GitHubError) as exc:
        parser.exit(1, f"{SOURCE}: {github.safe_message(exc)}\n")


if __name__ == "__main__":
    main()
