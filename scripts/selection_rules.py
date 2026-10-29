# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The skip rules a pull request's state is judged by, in table order.

Kept apart from ``select_pulls``, which reads and orders, so the rules
read as one table (docs/DESIGN.md section 5) and so the antagonistic
applier can re-check the author rule live without loading the
selection flow.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import ledger
import pull_reads as reads
from selection_inputs import bot_identity

SKIP_REASONS = (
    "repository",
    "bot_author",
    "human_author",
    "closed",
    "draft",
    "conflicting",
    "changes_requested",
    "already_approved",
    "copilot_pending",
    "copilot_feedback",
    "already_reviewed",
    "copilot_unreviewed",
    "ci_pending",
    "ci_failing",
    "ci_none",
    "already_assessed",
    "too_large",
    "cap",
)
# The CI rule: the rollup states that keep the approve mode waiting.
CI_SKIPS = {"pending": "ci_pending", "failure": "ci_failing", "none": "ci_none"}
# A pending review was never submitted and a dismissed one no longer
# stands, so neither shows that its author read the head.
INERT_REVIEW_STATES = frozenset({"PENDING", "DISMISSED"})


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


@dataclass(frozen=True)
class Rules:
    """What the rules need beyond the pull request: the mode and the memory.

    ``history`` reads a pull request's full review history; the
    antagonistic rules call it last, so a pull request an earlier rule
    skips costs no paginated read.
    """

    mode: str
    bot_authors: list[str] = field(default_factory=list[str])
    prior: dict[str, Any] = field(default_factory=ledger.empty_ledger)
    dry_run: bool = False
    reassess: bool = False
    bot_login: str | None = None
    history: Callable[[str, int], list[dict[str, Any]]] | None = None


def author_skip(
    author: dict[str, Any] | None, *, mode: str, bot_authors: list[str]
) -> str | None:
    """The author rule's skip reason for this mode, or None to keep the pull request.

    The approve mode keeps humans alone. The antagonistic mode keeps
    the listed bots alone; an author that cannot be read is no listed
    bot, so it is a bot to skip in either mode.
    """
    if mode == "approve":
        return "bot_author" if reads.is_bot_author(author) else None
    if author is None:
        return "bot_author"
    if not reads.is_bot_author(author):
        return "human_author"
    login = author.get("login")
    if isinstance(login, str) and bot_identity(login) in bot_authors:
        return None
    return "bot_author"


def submitted_bot_reviews(reviews: list[dict[str, Any]], head_sha: str) -> list[str]:
    """The folded logins of every bot that submitted a review of this head.

    Folding drops the ``[bot]`` suffix, after which an App and a human
    account with the same name would match; only reviews GitHub marks
    as a bot's count, so a human can never stand in for an App.
    """
    return [
        bot_identity(str(review["author"]))
        for review in reviews
        if isinstance(review.get("author"), str)
        and review.get("is_bot") is True
        and review.get("commit") == head_sha
        and review.get("state") not in INERT_REVIEW_STATES
    ]


def reviewed_by(reviews: list[dict[str, Any]], login: str, head_sha: str) -> bool:
    """Whether the bot ``login`` has submitted a review of this head.

    GitHub keeps the review, so this outlives any ledger: a head this
    App already reviewed is never worth another session.
    """
    return bot_identity(login) in submitted_bot_reviews(reviews, head_sha)


def copilot_reviewed(reviews: list[dict[str, Any]], head_sha: str) -> bool:
    """Whether Copilot has submitted a review of this head itself."""
    return any(
        reads.is_copilot(login) for login in submitted_bot_reviews(reviews, head_sha)
    )


def mode_skip(state: dict[str, Any], rules: Rules) -> str | None:
    """The rules one mode applies and the other does not.

    The approve mode waits for green CI, which an approval relies on.
    The antagonistic mode reviews a head once, and only after Copilot
    has: a review of an earlier head, or none, does not count. It reads
    the full review history, since the state query's last 100 reviews
    can miss an old one of this head.
    """
    if rules.mode == "approve":
        return CI_SKIPS.get(str(state["ci"]))
    reviews = (
        rules.history(str(state["repository"]), int(state["number"]))
        if rules.history is not None
        else cast("list[dict[str, Any]]", state["reviews"])
    )
    if rules.bot_login and reviewed_by(reviews, rules.bot_login, state["head_sha"]):
        return "already_reviewed"
    if not copilot_reviewed(reviews, state["head_sha"]):
        return "copilot_unreviewed"
    return None


def first_skip(state: dict[str, Any], rules: Rules) -> str | None:
    """The first skip rule the pull request's state trips, in table order."""
    if state["repository_archived"] or state["head_repository"] is None:
        return "repository"
    author = author_skip(
        state["author"], mode=rules.mode, bot_authors=rules.bot_authors
    )
    if author is not None:
        return author
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
    by_mode = mode_skip(state, rules)
    if by_mode is not None:
        return by_mode
    if not rules.reassess and ledger.already_assessed(
        rules.prior,
        state["repository"],
        state["number"],
        state["head_sha"],
        dry_run=rules.dry_run,
    ):
        return "already_assessed"
    return None
