# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""GitHub reads the pull request selection depends on.

Every function here reads and never writes. ``select_pulls`` owns the
policy (what to skip, how to order); this module answers the questions
it asks: which pull requests are open, what state each one is in, what
it changes and which commits it carries. Anyone can open a pull request
against a public repository, so every field is checked for shape and
bounded in size before it is handed on.
"""

from __future__ import annotations

from typing import Any, cast

import review_github as github

COPILOT_LOGINS = frozenset(
    {
        "copilot-pull-request-reviewer",
        "copilot-pull-request-reviewer[bot]",
        "copilot",
        "Copilot",
    }
)
COPILOT_LOGINS_FOLDED = frozenset(login.lower() for login in COPILOT_LOGINS)
SEARCH_LIMIT = 1000
MAX_FILES = 200
MAX_DIFF_BYTES = 512 * 1024
MAX_PATCH_BYTES = 64 * 1024
MAX_BODY_BYTES = 64 * 1024
MAX_MESSAGE_BYTES = 4 * 1024
SEARCH_FIELDS = "repository,number,title,url,author,isDraft,createdAt,updatedAt,labels"
PULL_STATES = frozenset({"OPEN", "CLOSED", "MERGED"})
MERGEABLE_STATES = frozenset({"MERGEABLE", "CONFLICTING", "UNKNOWN"})
REVIEW_STATES = frozenset(
    {"PENDING", "COMMENTED", "APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
)
# A pending review was never submitted and a dismissed one no longer
# counts towards merging, so neither is a reviewer's standing verdict.
INERT_REVIEW_STATES = frozenset({"PENDING", "DISMISSED"})
ROLLUP_CI = {
    "SUCCESS": "success",
    "PENDING": "pending",
    "EXPECTED": "pending",
    "FAILURE": "failure",
    "ERROR": "failure",
}

PULL_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner isArchived
    pullRequest(number: $number) {
      number url title body state isDraft mergeable createdAt updatedAt
      authorAssociation author { __typename login }
      headRefOid headRefName baseRefName baseRefOid isCrossRepository
      headRepository { nameWithOwner }
      labels(first: 50) { nodes { name } }
      commits(last: 1) { totalCount nodes { commit { statusCheckRollup { state
        contexts(first: 100) { nodes { __typename
          ... on CheckRun { name status conclusion }
          ... on StatusContext { context state } } } } } } }
      reviews(last: 100) {
        nodes { author { __typename login } state submittedAt commit { oid } } }
      reviewRequests(first: 50) { nodes { requestedReviewer { __typename
        ... on User { login } ... on Bot { login } ... on Team { slug }
        ... on Mannequin { login } } } }
      reviewThreads(first: 100) { nodes { isResolved
        comments(first: 1) { nodes { author { __typename login } } } } }
    }
  }
}
"""


class SelectionError(Exception):
    """Operational failure of the selection, not a skip."""


def require_object(value: Any, context: str) -> dict[str, Any]:
    """Return a JSON object or fail the operation."""
    if not isinstance(value, dict):
        raise github.GitHubError(f"{context}: expected an object")
    return cast("dict[str, Any]", value)


def require_bool(data: dict[str, Any], key: str, context: str) -> bool:
    """Return a boolean field or fail the operation."""
    value = data.get(key)
    if not isinstance(value, bool):
        raise github.GitHubError(f"{context}: missing or invalid {key!r}")
    return value


def require_count(data: dict[str, Any], key: str, context: str) -> int:
    """Return a non-negative integer field or fail the operation."""
    value = data.get(key)
    if type(value) is not int or value < 0:
        raise github.GitHubError(f"{context}: missing or invalid {key!r}")
    return value


def optional_str(value: Any, context: str) -> str | None:
    """Return a string or null field, refusing any other type."""
    if value is not None and not isinstance(value, str):
        raise github.GitHubError(f"{context}: expected a string or null")
    return value


def nodes_of(connection: Any, context: str) -> list[dict[str, Any]]:
    """Return the object nodes of a GraphQL connection.

    GraphQL returns a null node for an item the token cannot see; such
    an item carries nothing to act on, so it is left out rather than
    failing the read.
    """
    raw = require_object(connection, context).get("nodes")
    if not isinstance(raw, list):
        raise github.GitHubError(f"{context}: missing nodes")
    return [
        require_object(node, context)
        for node in cast("list[Any]", raw)
        if node is not None
    ]


def truncate_utf8(text: str, limit: int) -> str:
    """Bound a string by encoded bytes, never splitting a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", "ignore")


def bot_login(slug: str) -> str | None:
    """The App's bot login, or None when the run has no App."""
    return f"{slug}[bot]" if slug else None


def is_bot_author(author: dict[str, Any] | None) -> bool:
    """Whether an author object names something other than a human.

    An absent author is a deleted account, and a login that cannot be
    read is no better: neither is a human the bot should review for.
    """
    if author is None or author.get("is_bot") is True:
        return True
    if author.get("type") == "Bot" or author.get("__typename") == "Bot":
        return True
    login = author.get("login")
    return not isinstance(login, str) or login.endswith("[bot]")


def is_copilot(login: str | None) -> bool:
    """Whether a login belongs to the Copilot code review bot."""
    return login is not None and login.lower() in COPILOT_LOGINS_FOLDED


def author_of(raw: Any, context: str) -> dict[str, Any]:
    """Normalise a GraphQL actor into a login and a bot flag."""
    if raw is None:
        return {"login": None, "is_bot": True}
    data = require_object(raw, context)
    return {
        "login": github.require_str(data, "login", context),
        "is_bot": is_bot_author(data),
    }


def search_open_pulls(org: str, repositories: list[str]) -> list[dict[str, Any]]:
    """Return every open, non-draft pull request of the owner or named repositories."""
    args = ["search", "prs", "--owner", org, "--state", "open", "--draft=false"]
    args += ["--limit", str(SEARCH_LIMIT), "--json", SEARCH_FIELDS]
    for name in repositories:
        args += ["--repo", f"{org}/{name}"]
    parsed = github.decode_response(github.run_gh(args))
    if not isinstance(parsed, list):
        raise github.GitHubError("expected a pull request array from search")
    entries = cast("list[Any]", parsed)
    if len(entries) >= SEARCH_LIMIT:
        raise SelectionError(
            f"search returned {SEARCH_LIMIT} results; narrow the repository scope"
        )
    pulls: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise github.GitHubError("expected pull request objects from search")
        pulls.append(cast("dict[str, Any]", entry))
    return pulls


def check_of(node: dict[str, Any], context: str) -> dict[str, Any]:
    """Normalise one rollup context: a check run, or a commit status.

    A commit status has no lifecycle of its own: once posted it is
    final, so it maps onto a completed check run.
    """
    kind = node.get("__typename")
    if kind == "CheckRun":
        return {
            "name": github.require_str(node, "name", context),
            "status": github.require_str(node, "status", context),
            "conclusion": optional_str(node.get("conclusion"), context),
        }
    if kind == "StatusContext":
        return {
            "name": github.require_str(node, "context", context),
            "status": "COMPLETED",
            "conclusion": github.require_str(node, "state", context),
        }
    raise github.GitHubError(f"{context}: unexpected check type {kind!r}")


def rollup_of(connection: Any, context: str) -> tuple[str, list[dict[str, Any]], int]:
    """Read the head commit's status rollup: CI state, checks and commit count."""
    data = require_object(connection, context)
    total = github.require_int(data, "totalCount", context)
    nodes = nodes_of(data, context)
    if not nodes:
        raise github.GitHubError(f"{context}: no head commit")
    rollup = require_object(nodes[-1].get("commit"), context).get("statusCheckRollup")
    if rollup is None:
        return "none", [], total
    rollup_data = require_object(rollup, context)
    state = github.require_str(rollup_data, "state", context)
    ci = ROLLUP_CI.get(state)
    if ci is None:
        raise github.GitHubError(f"{context}: unexpected rollup state {state!r}")
    contexts = nodes_of(rollup_data.get("contexts"), context)
    return ci, [check_of(node, context) for node in contexts], total


def reviews_of(
    connection: Any, head_sha: str, context: str
) -> tuple[list[dict[str, Any]], bool, list[str]]:
    """Normalise the reviews and derive each reviewer's standing verdict.

    Returns the reviews, whether any reviewer's latest verdict requests
    changes, and the reviewers whose latest verdict approves the head
    commit itself: an approval of an earlier head says nothing about
    what was pushed since.
    """
    reviews: list[dict[str, Any]] = []
    latest: dict[str | None, dict[str, Any]] = {}
    for node in nodes_of(connection, context):
        author = author_of(node.get("author"), context)
        state = github.require_str(node, "state", context)
        if state not in REVIEW_STATES:
            raise github.GitHubError(f"{context}: unexpected review state {state!r}")
        commit = node.get("commit")
        review = {
            "author": author["login"],
            "is_bot": author["is_bot"],
            "state": state,
            "commit": (
                github.require_sha(require_object(commit, context), "oid", context)
                if commit is not None
                else None
            ),
            "submitted_at": optional_str(node.get("submittedAt"), context),
        }
        reviews.append(review)
        # Nodes arrive oldest first, so the last standing review wins.
        if state not in INERT_REVIEW_STATES:
            latest[author["login"]] = review
    changes_requested = any(r["state"] == "CHANGES_REQUESTED" for r in latest.values())
    approved_by = sorted(
        login
        for login, r in latest.items()
        if login is not None and r["state"] == "APPROVED" and r["commit"] == head_sha
    )
    return reviews, changes_requested, approved_by


def review_requests_of(connection: Any, context: str) -> list[str]:
    """The logins and team slugs still asked to review."""
    requested: list[str] = []
    for node in nodes_of(connection, context):
        reviewer = node.get("requestedReviewer")
        if reviewer is None:
            continue
        data = require_object(reviewer, context)
        key = "slug" if data.get("__typename") == "Team" else "login"
        requested.append(github.require_str(data, key, context))
    return requested


def copilot_threads_of(connection: Any, context: str) -> int:
    """Count the unresolved review threads Copilot opened."""
    count = 0
    for node in nodes_of(connection, context):
        if require_bool(node, "isResolved", context):
            continue
        comments = nodes_of(node.get("comments"), context)
        if comments and is_copilot(
            author_of(comments[0].get("author"), context)["login"]
        ):
            count += 1
    return count


def pull_state(repository: str, number: int) -> dict[str, Any]:
    """Read everything the skip rules need about one pull request in one query."""
    owner, _, name = repository.partition("/")
    context = f"{repository}#{number}"
    variables = {"owner": owner, "name": name, "number": number}
    repo_node = github.graphql(PULL_QUERY, variables, read=True).get("repository")
    if repo_node is None:
        raise github.GitHubError(f"{context}: repository not found")
    repo_data = require_object(repo_node, context)
    full = github.require_str(repo_data, "nameWithOwner", context)
    if not github.REPO_RE.fullmatch(full) or full.lower() != repository.lower():
        raise github.GitHubError(f"{context}: repository reply does not match")
    pull = repo_data.get("pullRequest")
    if pull is None:
        raise github.GitHubError(f"{context}: no such pull request")
    pr = require_object(pull, context)
    if github.require_int(pr, "number", context) != number:
        raise github.GitHubError(f"{context}: pull request reply does not match")
    url = github.require_str(pr, "url", context)
    if not url.startswith("https://github.com/"):
        raise github.GitHubError(f"{context}: unexpected url")
    state = github.require_str(pr, "state", context)
    if state not in PULL_STATES:
        raise github.GitHubError(f"{context}: unexpected state {state!r}")
    mergeable = github.require_str(pr, "mergeable", context)
    if mergeable not in MERGEABLE_STATES:
        raise github.GitHubError(f"{context}: unexpected mergeable {mergeable!r}")
    head_repo = pr.get("headRepository")
    head_repository: str | None = None
    if head_repo is not None:
        head_data = require_object(head_repo, context)
        head_repository = github.require_str(head_data, "nameWithOwner", context)
        if not github.REPO_RE.fullmatch(head_repository):
            raise github.GitHubError(f"{context}: invalid head repository")
    head_sha = github.require_sha(pr, "headRefOid", context)
    ci, checks, commits_count = rollup_of(pr.get("commits"), context)
    reviews, changes_requested, approved_by = reviews_of(
        pr.get("reviews"), head_sha, context
    )
    return {
        "repository": full,
        "repo_name": full.partition("/")[2],
        "repository_archived": require_bool(repo_data, "isArchived", context),
        "number": number,
        "url": url,
        "title": github.require_str(pr, "title", context),
        "body": truncate_utf8(
            optional_str(pr.get("body"), context) or "", MAX_BODY_BYTES
        ),
        "state": state,
        "is_draft": require_bool(pr, "isDraft", context),
        "author": author_of(pr.get("author"), context),
        "author_association": github.require_str(pr, "authorAssociation", context),
        "head_sha": head_sha,
        "head_repository": head_repository,
        "head_ref": github.require_str(pr, "headRefName", context),
        "base_ref": github.require_str(pr, "baseRefName", context),
        "base_sha": github.require_sha(pr, "baseRefOid", context),
        "is_fork": require_bool(pr, "isCrossRepository", context),
        "labels": [
            github.require_str(node, "name", context)
            for node in nodes_of(pr.get("labels"), context)
        ],
        "created_at": github.require_str(pr, "createdAt", context),
        "updated_at": github.require_str(pr, "updatedAt", context),
        "mergeable": mergeable,
        "ci": ci,
        "checks": checks,
        "reviews": reviews,
        "review_requests": review_requests_of(pr.get("reviewRequests"), context),
        "unresolved_copilot_threads": copilot_threads_of(
            pr.get("reviewThreads"), context
        ),
        "changes_requested": changes_requested,
        "approved_by": approved_by,
        "commits_count": commits_count,
    }
