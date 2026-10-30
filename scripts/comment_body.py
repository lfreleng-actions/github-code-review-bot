# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""What the antagonistic review says: its body, its inline comments, its summary.

Split from ``comment_review``, which decides whether and where to
post, so the wording and the containment of the agent's text read in
one place. Nothing here talks to GitHub. Every piece of agent text
that reaches a body passes ``contained``, which leaves no live link
outside the organisation, on top of the sanitising the check did.
"""

from __future__ import annotations

import re
from typing import Any, cast

import comment_markdown as markdown
import review_comments as comments
import review_policy as policy
from selection_rules import MAX_ROUNDS

DEFAULT_BOT = "the antagonistic review bot"
OPENING = "🤖 Antagonistic review by agent"
DISCLAIMER = (
    "This is not a human review and never an approval: a maintainer must "
    "still review and approve this pull request."
)
# GitHub refuses a review body over this many characters, and the
# 422 it answers would cost the whole review. Each finding the body
# folds in gets an equal share, its label included, leaving room for
# the opening, the summary, the notes, the footer and the marker.
REVIEW_BODY_CHARS = 65_536
FOLDED_CHARS = (REVIEW_BODY_CHARS - 4096) // comments.MAX_COMMENTS


def findings_of(check: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """One of the check's finding lists: ``comments`` inline, or ``unplaced``."""
    return cast("list[dict[str, Any]]", check.get(key) or [])


def code_span(text: str) -> str:
    """Render text as one literal code span, whatever backticks it holds.

    The delimiter is one backtick longer than the longest run inside,
    and padded with spaces, so no run in the text can close the span
    early and turn the rest into live Markdown.
    """
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    return f"{fence} {text} {fence}"


def finding_count(check: dict[str, Any]) -> int:
    """Every finding the review carries, inline or in its body."""
    return len(findings_of(check, "comments")) + len(findings_of(check, "unplaced"))


def summary_markdown(check: dict[str, Any]) -> str:
    """A short Markdown summary of the check for the job summary.

    Names every finding left out, and why, since the posted review
    sends readers here for them.
    """
    dropped = cast("list[str]", check["dropped"])
    lines = [
        f"## Antagonistic review check: [{check['repository']}#{check['number']}]"
        f"({check['url']})",
        "",
        f"- Title: {check['title']}",
        f"- Commentable: {'yes' if check['commentable'] else 'no'}",
        f"- Inline comments: {len(findings_of(check, 'comments'))}",
        f"- In the body, no line to sit on: {len(findings_of(check, 'unplaced'))}",
        f"- Malformed, left out: {len(dropped)}",
        f"- Beyond the cap of {comments.MAX_COMMENTS}, not read: {check['capped']}",
    ]
    lines.extend(
        f"  - {code_span(policy.sanitise(c['path'], 120))} line {c['line']}: "
        f"{policy.sanitise(c.get('reason'), policy.MAX_REASON_CHARS)}"
        for c in findings_of(check, "unplaced")
    )
    lines.extend(f"  - left out: {reason}" for reason in dropped)
    reasons = cast("list[str]", check.get("reasons") or [])
    if reasons:
        lines.append(f"- Reasons: {'; '.join(reasons)}")
    return "\n".join(lines) + "\n"


def reviewer_of(selection: dict[str, Any]) -> str:
    """The App login from the selection, or a generic name in a secretless run."""
    bot_login = selection.get("bot_login")
    return bot_login if isinstance(bot_login, str) and bot_login else DEFAULT_BOT


def inline(comment: dict[str, Any], org: str) -> dict[str, Any]:
    """One review comment as the reviews API takes it, on the new file's side.

    The severity sits on a line of its own, so a body that opens with a
    code fence still opens it at column 0, where the sanitiser assumed
    it would be.
    """
    payload: dict[str, Any] = {
        "path": comment["path"],
        "line": comment["line"],
        "side": "RIGHT",
        "body": f"**{comment['severity']}**:\n\n{fitted(comment['body'], org)}",
    }
    start = comment.get("start_line")
    if type(start) is int:
        payload["start_line"] = start
        payload["start_side"] = "RIGHT"
    return payload


def contained(text: str, org: str) -> str:
    """Untrusted Markdown with no live link outside the organisation, code aside."""
    return markdown.outside_code(text, lambda prose: policy.contain_links(prose, org))


def fitted(text: str, org: str, limit: int = markdown.MAX_COMMENT_CHARS) -> str:
    """Contained text cut back to the limit, since containment lengthens it.

    Each disarmed link or definition gains a zero-width space, so a body
    the check cut to the limit can come back longer; the cut here keeps
    every fence closed, and its mark completes no link.
    """
    return markdown.fit(contained(text, org), limit)


def folded_finding(comment: dict[str, Any], org: str) -> str:
    """One finding in the review body, label and text within its share.

    The label is bounded: the path is cut and sits in a code span no
    backtick in it can close, and the line, which the agent chose and
    nothing placed, is cut too, so the text always keeps most of the
    share.
    """
    label = (
        f"- {code_span(policy.sanitise(comment['path'], 200))} line "
        f"{policy.sanitise(comment['line'], 12)}, **{comment['severity']}**:\n\n"
    )
    return label + fitted(comment["body"], org, FOLDED_CHARS - len(label))


def review_body(
    check: dict[str, Any],
    selection: dict[str, Any],
    run_url: str,
    folded: list[dict[str, Any]],
    note: str = "",
    marker: str = "",
) -> str:
    """Compose the review body; the agent never writes to GitHub itself.

    ``folded`` carries the findings that have no line to sit on, which
    appear in the body under their file and line instead. A note says
    how many proposed findings were malformed, so a short review never
    passes for a clean one.
    """
    count = finding_count(check)
    model = policy.sanitise(selection.get("model"), 100) or "unknown"
    org = str(check["repository"]).partition("/")[0]
    summary = policy.contain_links(
        policy.sanitise(check.get("summary"), policy.MAX_SUMMARY_CHARS), org
    )
    # With no line-level finding the summary carries the verdict, and
    # may name a whole-change blocker, so the opening claims nothing.
    if count:
        opening = f"{OPENING}: {count} finding(s) for the human reviewer."
    elif summary:
        opening = (
            f"{OPENING}: no line-level findings; the summary below is the verdict."
        )
    else:
        opening = f"{OPENING}: no findings on this head."
    parts = [opening, summary]
    if folded:
        parts.append("\n\n".join(folded_finding(c, org) for c in folded))
    dropped = len(cast("list[Any]", check.get("dropped") or []))
    if dropped:
        parts.append(
            f"_{dropped} further proposed finding(s) were malformed and are not "
            "shown; the run's check summary names them._"
        )
    capped = check.get("capped")
    if type(capped) is int and capped > 0:
        parts.append(
            f"_The agent proposed {capped} finding(s) beyond the cap of "
            f"{comments.MAX_COMMENTS}; they were not read._"
        )
    footer = (
        f"---\nAutomated antagonistic review by `{reviewer_of(selection)}` using "
        f"model `{model}`; see the [run]({run_url}). {DISCLAIMER}"
    )
    body = "\n\n".join([*(part for part in parts if part), note, footer])
    return f"{body}\n\n{marker}" if marker else body


def handback_marker(round_number: int, head_sha: str, run_id: int) -> str:
    """The hidden last line of a review that hands its pull request back.

    The hand-back job dispatches a pull request only when this App's
    review of its head ends with this line, read from GitHub, naming
    the run that dispatches. Agent text cannot forge it: ``<`` is
    disarmed everywhere outside code, and the line must be the body's
    last, after the footer this module writes. The run ties the
    dispatch to the applier that posted the review: another run holding
    the same head finds the marker but not its own run in it.
    """
    return (
        f"<!-- antagonistic-handback round={round_number} head={head_sha} "
        f"run={run_id} -->"
    )


def round_note(round_number: int, handing_back: bool) -> str:
    """The line that says which round this is and what happens next."""
    where = f"Antagonistic round {round_number} of {MAX_ROUNDS}."
    if handing_back:
        return f"_{where} Handed back to Code Monkey for a revision round._"
    if round_number >= MAX_ROUNDS:
        return f"_{where} That is the last one: a maintainer takes it from here._"
    return f"_{where}_"
