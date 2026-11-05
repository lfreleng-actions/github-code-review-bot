# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The changed files, commits and full review history of one pull request.

Split from ``pull_reads`` so the state reads the skip rules depend
on stay apart from the paginated reads, which are the costly part and
run for the candidates that need them alone.
"""

from __future__ import annotations

from typing import Any, cast

import bot_github as github
from pull_reads import (
    MAX_DIFF_BYTES,
    MAX_FILES,
    MAX_MESSAGE_BYTES,
    MAX_PATCH_BYTES,
    is_copilot,
    optional_str,
    require_count,
    require_object,
    truncate_utf8,
)
from review_verdict import MAX_BODY_BYTES as MAX_REVIEW_BODY_BYTES
from selection_inputs import bot_identity


def pull_comparison(
    repository: str, base_sha: str, head_sha: str
) -> tuple[list[dict[str, Any]], int, bool, list[dict[str, Any]]]:
    """Read the changed files and commits of one fixed comparison.

    The pull request's own files and commits listings follow its live
    head and base, so a push or a retarget undone between two reads
    would leave another comparison's diff under the selected SHAs. The
    three-dot compare names both ends by SHA and diffs from their merge
    base, as the pull request does, so what it returns belongs to the
    selected comparison whatever the pull request does meanwhile.

    Returns the files, the total patch bytes GitHub reported, whether
    the change exceeds what a review session may receive, and the
    commits. GitHub lists at most 300 files for a comparison, above
    ``MAX_FILES``, so a longer list is still reported too large.
    """
    context = f"{repository} {base_sha[:12]}...{head_sha[:12]}"
    endpoint = f"repos/{repository}/compare/{base_sha}...{head_sha}?per_page=100"
    pages = github.decode_response(
        github.run_gh(["api", endpoint, "--paginate", "--slurp"])
    )
    if not isinstance(pages, list) or not pages:
        raise github.GitHubError(f"{context}: expected paginated comparison pages")
    file_entries: list[Any] = []
    commit_entries: list[Any] = []
    for index, page in enumerate(cast("list[Any]", pages)):
        data = require_object(page, context)
        # GitHub lists the files on the first page alone.
        file_entries.extend(page_list(data, "files", context, required=index == 0))
        commit_entries.extend(page_list(data, "commits", context, required=True))
    files, diff_bytes, too_large = bounded_files(file_entries, f"{context} file")
    commits = [commit_entry(entry, f"{context} commit") for entry in commit_entries]
    return files, diff_bytes, too_large, commits


def page_list(
    page: dict[str, Any], key: str, context: str, *, required: bool
) -> list[Any]:
    """One list field of a comparison page; absent is empty unless required."""
    value = page.get(key)
    if value is None and not required:
        return []
    if not isinstance(value, list):
        raise github.GitHubError(f"{context}: missing or invalid '{key}'")
    return cast("list[Any]", value)


def bounded_files(
    entries: list[Any], context: str
) -> tuple[list[dict[str, Any]], int, bool]:
    """Bound the changed files' patches and flag a change too large to review.

    Once the change is too large the files read so far are returned as
    they stand.
    """
    files: list[dict[str, Any]] = []
    diff_bytes = 0
    for item in entries:
        if len(files) >= MAX_FILES:
            return files, diff_bytes, True
        entry = require_object(item, context)
        raw_patch = optional_str(entry.get("patch"), context)
        size = len(raw_patch.encode("utf-8")) if raw_patch is not None else 0
        diff_bytes += size
        if diff_bytes > MAX_DIFF_BYTES:
            return files, diff_bytes, True
        files.append(
            {
                "path": github.require_str(entry, "filename", context),
                "status": github.require_str(entry, "status", context),
                "additions": require_count(entry, "additions", context),
                "deletions": require_count(entry, "deletions", context),
                "previous_path": optional_str(entry.get("previous_filename"), context),
                "patch": (
                    truncate_utf8(raw_patch, MAX_PATCH_BYTES)
                    if raw_patch is not None
                    else None
                ),
                "patch_truncated": size > MAX_PATCH_BYTES,
            }
        )
    return files, diff_bytes, False


def commit_entry(item: Any, context: str) -> dict[str, Any]:
    """One commit with a bounded message and its verification flag."""
    entry = require_object(item, context)
    detail = require_object(entry.get("commit"), context)
    message = detail.get("message")
    if not isinstance(message, str):
        raise github.GitHubError(f"{context}: missing or invalid 'message'")
    verification = detail.get("verification")
    # The author is null when the commit email matches no account.
    author = entry.get("author")
    login = (
        cast("dict[str, Any]", author).get("login")
        if isinstance(author, dict)
        else None
    )
    return {
        "sha": github.require_sha(entry, "sha", context),
        "message": truncate_utf8(message, MAX_MESSAGE_BYTES),
        "verified": isinstance(verification, dict)
        and cast("dict[str, Any]", verification).get("verified") is True,
        "author": login if isinstance(login, str) else None,
    }


def pull_reviews(repository: str, number: int) -> list[dict[str, Any]]:
    """Every review of the pull request, oldest first, however many there are.

    The state query reads the last 100. A rule that must know whether a
    reviewer ever reviewed a head, rather than recently, reads them all.
    Each entry has the ``author``, ``state``, ``commit`` and
    ``submitted_at`` keys of the state query's reviews, plus ``is_bot``
    and ``body``; a deleted account leaves the author null.

    The body is kept for Copilot's reviews alone, which the
    antagonistic mode classifies (DESIGN.md section 19.2), and only up
    to the size the classifier reads: a larger one is dropped to null,
    which the classifier reads as no assessment, never clean. Every
    other review's body is null, so no other text grows the run.
    """
    context = f"{repository}#{number} review"
    reviews: list[dict[str, Any]] = []
    for entry in github.api_list(
        f"repos/{repository}/pulls/{number}/reviews?per_page=100"
    ):
        user = entry.get("user")
        data = cast("dict[str, Any]", user) if isinstance(user, dict) else {}
        login = data.get("login")
        commit = entry.get("commit_id")
        is_bot = data.get("type") == "Bot"
        body = entry.get("body")
        kept = (
            body
            if is_bot
            and isinstance(login, str)
            and is_copilot(bot_identity(login))
            and isinstance(body, str)
            and len(body.encode("utf-8")) <= MAX_REVIEW_BODY_BYTES
            else None
        )
        reviews.append(
            {
                "author": login if isinstance(login, str) else None,
                "is_bot": is_bot,
                "state": github.require_str(entry, "state", context),
                "commit": commit if isinstance(commit, str) else None,
                "submitted_at": optional_str(entry.get("submitted_at"), context),
                "body": kept,
            }
        )
    return reviews
