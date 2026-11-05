# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Find this run's own review, so an ambiguous write is never sent twice.

Each mode's applier makes one review write. A 5xx or a timeout on it
is ambiguous: GitHub may have created the review and lost the reply.
Every review an applier posts therefore ends with a hidden marker
naming the mode, the head and the run, and this module reads it back
from GitHub. The read decides only whether the write landed; it never
resends, because a review found by a marker is not repeat-safe: a
timed-out request can still land after the look.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, cast

import bot_github as github
from selection_inputs import bot_identity

HEAD = r"head=(?P<head>[0-9a-f]{40})"
RUN = r"run=(?P<run>[1-9][0-9]*)"
APPROVE_RE = re.compile(rf"<!-- code-review-approval {HEAD} {RUN} -->\Z")
# The antagonistic mode ends a review with one of two markers: the
# hand-back marker (comment_body.handback_marker) when the review goes
# back for a revision round, this run's review marker otherwise.
ANTAGONISTIC_RE = re.compile(
    r"<!-- antagonistic-(?P<kind>handback|review) round=(?P<round>[1-9][0-9]*) "
    rf"{HEAD} {RUN} -->\Z"
)


def approval_marker(head_sha: str, run_id: int) -> str:
    """The hidden last line of an approval this run posts."""
    return f"<!-- code-review-approval head={head_sha} run={run_id} -->"


def own_review(
    repository: str,
    number: int,
    bot_login: str | None,
    head_sha: str,
    run_id: int,
    pattern: re.Pattern[str],
) -> tuple[dict[str, Any], re.Match[str]] | None:
    """This App's review of ``head_sha`` that ends with this run's marker.

    Returns the review as the REST API gives it, with the marker found,
    or None. Only a review GitHub marks as a bot's, by this App, on this
    head, whose body ends with a marker naming this head and this run
    counts: another actor's review, or this App's review for another
    run, is never mistaken for this run's. With no App login, as in a
    secretless run, nothing can be this App's. A failed read raises.
    """
    if not bot_login:
        return None
    own = bot_identity(bot_login)
    for review in github.api_list(
        f"repos/{repository}/pulls/{number}/reviews?per_page=100"
    ):
        user = review.get("user")
        data = cast("dict[str, Any]", user) if isinstance(user, dict) else {}
        login, body = data.get("login"), review.get("body")
        if not (
            data.get("type") == "Bot"
            and isinstance(login, str)
            and bot_identity(login) == own
            and review.get("commit_id") == head_sha
            and isinstance(body, str)
        ):
            continue
        found = pattern.search(body.rstrip())
        if (
            found is not None
            and found.group("head") == head_sha
            and found.group("run") == str(run_id)
        ):
            return review, found
    return None


def post_once(
    write: Callable[[], dict[str, Any]],
    landed: Callable[[], dict[str, Any] | None],
    label: str,
) -> dict[str, Any]:
    """Send one review; after a transient failure, look for it, never resend.

    ``landed`` returns the review this write would have created, or
    None. Found, the write landed and its review is the reply; not
    found, the write's own failure stands, and the look each applier
    makes before it posts keeps a re-run from posting twice. A look
    that fails fails the write with both errors.
    """
    return github.write_with_recovery(write, landed, repeat_safe=False, label=label)
