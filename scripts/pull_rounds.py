# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""This App's earlier rounds on a pull request: its reviews and its threads.

The antagonistic mode reviews a pull request again on each new head,
up to the round cap. Without the earlier rounds each session starts
blind: it cannot tell which of its findings the new head fixed, which
the author disputed and which still stand. This module reads them for
a pull request that reached selection, and only once this App has
reviewed it before (docs/DESIGN.md section 19.2).

This App's own text was sanitised when it was posted. A reply is the
pull request author's words, so each is bounded here and the prompt
treats it as a claim to check, never an instruction.
"""

from __future__ import annotations

import json
from typing import Any, cast

import bot_github as github
from pull_checks import nodes_of, optional_str, require_object
from pull_reads import is_bot_author, require_bool, truncate_utf8
from selection_inputs import bot_identity

MAX_FINDINGS = 50
MAX_FINDINGS_BYTES = 64 * 1024
MAX_REPLIES = 3
MAX_REPLY_BYTES = 1024
# A pending review was never submitted and a dismissed one no longer
# stands, so neither is an earlier round.
INERT_REVIEW_STATES = frozenset({"PENDING", "DISMISSED"})

# The author's replies are sought among a thread's newest 50 comments,
# and in older pages of COMMENTS_QUERY while fewer than three are found.
THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id isResolved isOutdated path line
          root: comments(first: 1) { nodes { id body
            author { __typename login }
            pullRequestReview { commit { oid } } } }
          recent: comments(last: 50) {
            pageInfo { hasPreviousPage startCursor }
            nodes { id body author { __typename login } } } } } } }
}
"""

COMMENTS_QUERY = """
query($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on PullRequestReviewThread {
      comments(last: 50, before: $cursor) {
        pageInfo { hasPreviousPage startCursor }
        nodes { id body author { __typename login } } } } }
}
"""


def is_own(review: dict[str, Any], own: str) -> bool:
    """Whether a history entry is a submitted review by the App ``own`` folds to.

    Only an author GitHub marks as a bot counts, so a human account
    named like the App contributes nothing.
    """
    author = review.get("author")
    return (
        isinstance(author, str)
        and review.get("is_bot") is True
        and bot_identity(author) == own
        and review.get("state") not in INERT_REVIEW_STATES
    )


def earlier_reviews(
    history: list[dict[str, Any]], bot_login: str
) -> list[dict[str, Any]]:
    """This App's submitted reviews, oldest first, from the full history.

    The history read keeps this App's bodies already cut to size
    (``pull_changes.pull_reviews``).
    """
    own = bot_identity(bot_login)
    return [
        {
            "head_sha": review.get("commit"),
            "submitted_at": review.get("submitted_at"),
            "body": review.get("body") or "",
        }
        for review in history
        if is_own(review, own)
    ]


def same_actor(raw: Any, login: str, is_bot: bool) -> bool:
    """Whether a GraphQL actor is the account ``login`` of the given kind."""
    if not isinstance(raw, dict):
        return False
    actor = cast("dict[str, Any]", raw)
    name = actor.get("login")
    return (
        isinstance(name, str)
        and bot_identity(name) == bot_identity(login)
        and is_bot_author(actor) == is_bot
    )


def review_head(root: dict[str, Any], context: str) -> str | None:
    """The head SHA of the review a thread's first comment came with."""
    review = root.get("pullRequestReview")
    if review is None:
        return None
    commit = require_object(review, context).get("commit")
    if commit is None:
        return None
    return github.require_sha(require_object(commit, context), "oid", context)


def older_comments(thread_id: str, cursor: str, context: str) -> dict[str, Any]:
    """The page of a thread's comments just before ``cursor``."""
    variables = {"id": thread_id, "cursor": cursor}
    thread = github.graphql(COMMENTS_QUERY, variables, read=True).get("node")
    if thread is None:
        raise github.GitHubError(f"{context}: thread not found")
    return require_object(require_object(thread, context).get("comments"), context)


def replies_of(
    node: dict[str, Any], root_id: str, author: dict[str, Any], context: str
) -> list[dict[str, Any]]:
    """Up to the newest three later comments by the pull request's author.

    Other comments can push the author's replies out of the newest 50
    the thread read carries, so older pages are read, newest first,
    until three are found or the thread is exhausted.
    """
    login = author.get("login")
    if not isinstance(login, str):
        return []
    is_bot = author.get("is_bot") is True
    replies: list[dict[str, Any]] = []
    connection = require_object(node.get("recent"), context)
    while True:
        found: list[dict[str, Any]] = []
        for comment in nodes_of(connection, context):
            raw = comment.get("author")
            if comment.get("id") == root_id or not same_actor(raw, login, is_bot):
                continue
            body = optional_str(comment.get("body"), context) or ""
            found.append(
                {
                    "author": require_object(raw, context)["login"],
                    "body": truncate_utf8(body, MAX_REPLY_BYTES),
                }
            )
        replies = found + replies
        page = require_object(connection.get("pageInfo"), context)
        if len(replies) >= MAX_REPLIES or not require_bool(
            page, "hasPreviousPage", context
        ):
            return replies[-MAX_REPLIES:]
        connection = older_comments(
            github.require_str(node, "id", context),
            github.require_str(page, "startCursor", context),
            context,
        )


def finding_of(
    node: dict[str, Any], bot_login: str, author: dict[str, Any], context: str
) -> dict[str, Any] | None:
    """One thread as an earlier finding, or None when this App did not open it."""
    roots = nodes_of(node.get("root"), context)
    if not roots or not same_actor(roots[0].get("author"), bot_login, True):
        return None
    root = roots[0]
    line = node.get("line")
    if line is not None and type(line) is not int:
        raise github.GitHubError(f"{context}: invalid 'line'")
    return {
        "path": github.require_str(node, "path", context),
        "line": line,
        "body": optional_str(root.get("body"), context) or "",
        "head_sha": review_head(root, context),
        "resolved": require_bool(node, "isResolved", context),
        "outdated": require_bool(node, "isOutdated", context),
        "replies": replies_of(
            node, github.require_str(root, "id", context), author, context
        ),
    }


def threads_page(repository: str, number: int, cursor: str | None) -> dict[str, Any]:
    """One page of the pull request's review threads."""
    owner, _, name = repository.partition("/")
    context = f"{repository}#{number} threads"
    variables = {"owner": owner, "name": name, "number": number, "cursor": cursor}
    repo_node = github.graphql(THREADS_QUERY, variables, read=True).get("repository")
    if repo_node is None:
        raise github.GitHubError(f"{context}: repository not found")
    pull = require_object(repo_node, context).get("pullRequest")
    if pull is None:
        raise github.GitHubError(f"{context}: no such pull request")
    return require_object(require_object(pull, context).get("reviewThreads"), context)


def earlier_findings(
    repository: str, number: int, bot_login: str, author: dict[str, Any]
) -> tuple[list[dict[str, Any]], bool]:
    """This App's review threads, oldest first, and whether more exist.

    GitHub lists threads oldest first. A thread is this App's when its
    first comment's author is; at most ``MAX_FINDINGS`` threads and
    ``MAX_FINDINGS_BYTES`` as a serialised list are kept, and the read
    stops at the first that does not fit. ``author`` is the pull
    request's author as the state read normalises it.
    """
    context = f"{repository}#{number} threads"
    findings: list[dict[str, Any]] = []
    used = 0
    cursor: str | None = None
    while True:
        connection = threads_page(repository, number, cursor)
        for node in nodes_of(connection, context):
            finding = finding_of(node, bot_login, author, context)
            if finding is None:
                continue
            # json.dumps writes a list as "[a, b]": two bytes per item.
            size = len(json.dumps(finding).encode("utf-8")) + 2
            if len(findings) >= MAX_FINDINGS or used + size > MAX_FINDINGS_BYTES:
                return findings, True
            used += size
            findings.append(finding)
        page = require_object(connection.get("pageInfo"), context)
        if not require_bool(page, "hasNextPage", context):
            return findings, False
        cursor = github.require_str(page, "endCursor", context)
