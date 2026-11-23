# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The fixed text of a review notice: its reasons, sentences and marker.

Kept apart from ``review_notice``, which reads and writes, so every
word a notice can carry reads as one table, and so each skip reason
of ``selection_rules`` is classified here as notifying or silent.
"""

from __future__ import annotations

import re

from pull_reads import MAX_DIFF_BYTES, MAX_FILES
from selection_rules import MAX_ROUNDS

ASK_AGAIN = (
    "To ask again, dispatch `code-review-antagonistic.yaml` in `{workflow}`"
    " naming this pull request, with `reassess` on."
)
# Each reason a notice gives: its sentence, and whether the ask-again
# sentence follows it. ``no_review`` and ``selection_failed`` are the
# notice's own; every other key is a skip reason of selection_rules.
SENTENCES: dict[str, tuple[str, bool]] = {
    "no_review": (
        "The run selected this head and ended without posting a review: a"
        " session or a step failed, the base branch moved during the session,"
        " or the session gave nothing the bot could post. The run's summary"
        " says which.",
        True,
    ),
    "selection_failed": (
        "The run failed while selecting pull requests, before any session started.",
        True,
    ),
    "moved": ("The pull request changed while the bot was reading it.", True),
    "cap": (
        "The run was asked for more than it reviews at once, and this one was"
        " left out.",
        True,
    ),
    "already_assessed": (
        "The bot's ledger holds an earlier run's verdict on this head, so this"
        " run did not review it again.",
        True,
    ),
    "draft": ("The pull request is a draft, and this mode reviews none.", False),
    "conflicting": ("The head conflicts with its base branch.", False),
    "changes_requested": ("A reviewer has requested changes.", False),
    "already_approved": (
        "A reviewer has already approved this head, and this mode leaves an"
        " approved head alone.",
        False,
    ),
    "copilot_pending": (
        "Copilot's review of this head has been requested and has not arrived.",
        False,
    ),
    "copilot_feedback": (
        "Copilot review threads on this pull request are unresolved.",
        False,
    ),
    "reviewer_feedback": (
        "A maintainer's or this bot's review threads on this pull request are"
        " unresolved.",
        False,
    ),
    "copilot_unreviewed": ("Copilot has not reviewed this head.", False),
    "copilot_findings": ("Copilot's latest review of this head is not clean.", False),
    "round_cap": (
        f"This pull request has had its {MAX_ROUNDS} antagonistic reviews; a"
        " maintainer takes it from here.",
        False,
    ),
    "too_large": (
        "The change is larger than this mode reads: over"
        f" {MAX_FILES} files or {MAX_DIFF_BYTES // 1024} KiB of patch.",
        False,
    ),
}
OWN_REASONS = frozenset({"no_review", "selection_failed"})
# The skip reasons that earn a notice when a named run meets them.
NOTIFYING = frozenset(SENTENCES) - OWN_REASONS
# The skip reasons that never do: the pull request is not this mode's
# to review, is closed, or carries this App's review of the head; the
# rest are the reasons the approve mode alone records.
SILENT = frozenset(
    {
        "repository",
        "bot_author",
        "human_author",
        "closed",
        "already_reviewed",
        "governing_repository",
        "untrusted_author",
        "ci_pending",
        "ci_failing",
        "ci_none",
    }
)
MARKER_RE = re.compile(
    r"<!-- antagonistic-notice reason=(?P<reason>[a-z_]+)"
    r" head=(?P<head>[0-9a-f]{40}) run=(?P<run>[1-9][0-9]*) -->\Z"
)


def notice_body(
    reason: str, head: str, bot_login: str, run_url: str, run_id: int, workflow: str
) -> str:
    """The comment, ending with the marker that names reason, head and run."""
    sentence, ask = SENTENCES[reason]
    if ask:
        sentence += " " + ASK_AGAIN.format(workflow=workflow)
    return "\n".join(
        [
            f"🤖 Antagonistic review bot: no review of head `{head[:12]}`.",
            "",
            sentence,
            "",
            "---",
            f"Automated notice by `{bot_login}`; see the [run]({run_url}). This"
            " is not a review: the head stays unreviewed, and the notice counts"
            " as none of the pull request's antagonistic rounds.",
            "",
            f"<!-- antagonistic-notice reason={reason} head={head} run={run_id} -->",
        ]
    )
