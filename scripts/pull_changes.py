# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The changed files and commits of one pull request, bounded.

Split from ``pull_reads`` so the state reads the skip rules depend
on stay apart from the diff reads, which are the costly part and run
for survivors alone.
"""

from __future__ import annotations

from typing import Any, cast

import review_github as github
from pull_reads import (
    MAX_DIFF_BYTES,
    MAX_FILES,
    MAX_MESSAGE_BYTES,
    MAX_PATCH_BYTES,
    optional_str,
    require_count,
    require_object,
    truncate_utf8,
)


def pull_files(repository: str, number: int) -> tuple[list[dict[str, Any]], int, bool]:
    """Read the changed files with bounded patches.

    Returns the files, the total patch bytes GitHub reported, and
    whether the change exceeds what a review session may receive;
    once it does the files read so far are returned as they stand.
    """
    context = f"{repository}#{number} file"
    entries = github.api_list(f"repos/{repository}/pulls/{number}/files?per_page=100")
    files: list[dict[str, Any]] = []
    diff_bytes = 0
    for entry in entries:
        if len(files) >= MAX_FILES:
            return files, diff_bytes, True
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


def pull_commits(repository: str, number: int) -> list[dict[str, Any]]:
    """Read the commits with bounded messages and their verification flag."""
    context = f"{repository}#{number} commit"
    commits: list[dict[str, Any]] = []
    for entry in github.api_list(
        f"repos/{repository}/pulls/{number}/commits?per_page=100"
    ):
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
        commits.append(
            {
                "sha": github.require_sha(entry, "sha", context),
                "message": truncate_utf8(message, MAX_MESSAGE_BYTES),
                "verified": isinstance(verification, dict)
                and cast("dict[str, Any]", verification).get("verified") is True,
                "author": login if isinstance(login, str) else None,
            }
        )
    return commits
