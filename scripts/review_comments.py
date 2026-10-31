# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Decide which of an antagonistic agent's comments a trusted job may post.

The agent's final message is untrusted text. ``extract_verdict`` reads
it into a bounded, validated shape; ``anchor`` keeps the comments
whose path and lines lie inside the diff GitHub will accept them on;
``check`` produces the antagonistic mode's ``check.json``. Nothing here
talks to GitHub, and nothing here has a way to express an approval:
the verdict carries no tier and the check no approvable flag.
"""

from __future__ import annotations

import json
import re
from typing import Any, cast

import review_policy as policy
from comment_markdown import comment_text
from review_policy import Rejected

SCHEMA = 1
MODE = "antagonistic"
SEVERITIES = ("blocker", "major", "minor", "nit")
MAX_COMMENTS = 25
MAX_PATH_CHARS = 400
MAX_DROPPED = 50
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
PATH_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")

__all__ = [
    "MODE",
    "SEVERITIES",
    "Rejected",
    "anchor",
    "check",
    "extract_verdict",
]


def right_lines(patch: str) -> dict[int, int]:
    """Map each line of the new file that a patch shows to the hunk it is in.

    Review comments on the right side of a diff may land on an added
    or a context line, and a range must stay within one hunk.
    """
    lines: dict[int, int] = {}
    hunk = -1
    right = 0
    for raw in patch.split("\n"):
        found = HUNK_RE.match(raw)
        if found is not None:
            hunk += 1
            right = int(found.group(1))
        elif hunk >= 0 and raw[:1] in ("+", " "):
            lines[right] = hunk
            right += 1
    return lines


def comment_of(item: object, index: int) -> tuple[dict[str, Any] | None, str | None]:
    """Validate one raw comment; return it, or why it was dropped."""
    where = f"comment {index + 1}"
    if not isinstance(item, dict):
        return None, f"{where}: not an object"
    data = cast("dict[str, Any]", item)
    path = data.get("path")
    if (
        not isinstance(path, str)
        or not path
        or len(path) > MAX_PATH_CHARS
        or PATH_CONTROL_RE.search(path)
    ):
        return None, f"{where}: invalid path"
    line = data.get("line")
    if type(line) is not int or line <= 0:
        return None, f"{where}: line must be a positive integer"
    start = data.get("start_line")
    if start is not None and (type(start) is not int or start <= 0):
        return None, f"{where}: start_line must be a positive integer"
    # A range must open above its last line; one that does not names a
    # single line at most, so it degrades to that line, not to nothing.
    if start is not None and start >= line:
        start = None
    severity = data.get("severity")
    if severity not in SEVERITIES:
        return None, f"{where}: severity must be one of {', '.join(SEVERITIES)}"
    body = data.get("body")
    text = comment_text(body) if isinstance(body, str) else ""
    if not text:
        return None, f"{where}: empty body"
    comment: dict[str, Any] = {
        "path": path,
        "line": line,
        "severity": severity,
        "body": text,
    }
    if start is not None:
        comment["start_line"] = start
    return comment, None


def comments_of(value: object) -> tuple[list[dict[str, Any]], list[str], int]:
    """Validate the comments list up to the cap.

    Returns the well-formed comments, one reason per malformed comment,
    and how many comments lay beyond the cap unread: those were never
    validated, so they count apart from the malformed ones.
    """
    if not isinstance(value, list):
        raise Rejected("verdict 'comments' must be a list")
    items = cast("list[Any]", value)
    comments: list[dict[str, Any]] = []
    dropped: list[str] = []
    for index, item in enumerate(items[:MAX_COMMENTS]):
        comment, reason = comment_of(item, index)
        if comment is not None:
            comments.append(comment)
        elif reason is not None:
            dropped.append(reason)
    return comments, dropped, max(len(items) - MAX_COMMENTS, 0)


def summary_finding(value: object) -> bool:
    """Whether the summary itself names a defect to fix; absent means no.

    A defect in removed lines, or in the change as a whole, has no line
    to anchor on and lives in the summary; this flag carries it to the
    hand-back, which a clean summary must not trigger.
    """
    if value is None:
        return False
    if not isinstance(value, bool):
        raise Rejected("verdict 'summary_finding' must be true or false")
    return value


def extract_verdict(text: str) -> dict[str, Any]:
    """Pull the antagonistic verdict out of the agent's final message, validated.

    The whole verdict is refused for a shape problem outside the
    comments; one malformed comment costs that comment alone.
    """
    block = policy.final_json_block(text)
    try:
        parsed: Any = json.loads(block)
    except (ValueError, RecursionError) as exc:
        raise Rejected(f"the final verdict block is malformed: {exc}") from exc
    if not isinstance(parsed, dict):
        raise Rejected("the final verdict block is not an object")
    data = cast("dict[str, Any]", parsed)
    if data.get("schema") != SCHEMA or isinstance(data.get("schema"), bool):
        raise Rejected("verdict 'schema' must be 1")
    repository = data.get("repository")
    if not isinstance(repository, str) or not policy.REPO_RE.fullmatch(repository):
        raise Rejected("verdict 'repository' must be owner/name")
    head_sha = data.get("head_sha")
    if not isinstance(head_sha, str) or not policy.SHA_RE.fullmatch(head_sha):
        raise Rejected("verdict 'head_sha' must be a 40-hex commit SHA")
    comments, dropped, capped = comments_of(data.get("comments"))
    return {
        "schema": SCHEMA,
        "repository": repository,
        "pull_request": policy.positive_int(data.get("pull_request"), "pull_request"),
        "head_sha": head_sha,
        "summary": policy.text_field(
            data.get("summary"), "summary", policy.MAX_SUMMARY_CHARS
        ),
        "comments": comments,
        "dropped": dropped,
        "capped": capped,
        "summary_finding": summary_finding(data.get("summary_finding")),
        "injection_attempts": policy.injections_of(data.get("injection_attempts")),
    }


def anchor(
    entry: dict[str, Any], comments: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split the comments into those GitHub can place inline and the rest.

    A comment goes inline when its path is a file the pull request
    changes, with a patch, and its line, and the start of a range, are
    lines the patch shows on its right side, a range within one hunk.
    Any other comment is still a finding: it comes back as unplaced,
    with the reason, for the review body to carry. Exact repeats
    collapse.
    """
    patches: dict[str, dict[int, int] | None] = {}
    for item in cast("list[Any]", entry.get("files") or []):
        if isinstance(item, dict):
            file = cast("dict[str, Any]", item)
            path, patch = file.get("path"), file.get("patch")
            if isinstance(path, str):
                patches[path] = right_lines(patch) if isinstance(patch, str) else None
    inline: list[dict[str, Any]] = []
    unplaced: list[dict[str, Any]] = []
    seen: set[tuple[str, int | None, int, str]] = set()
    for comment in comments:
        path, line = str(comment["path"]), int(comment["line"])
        start = cast("int | None", comment.get("start_line"))
        key = (path, start, line, str(comment["body"]))
        if key in seen:
            continue
        seen.add(key)
        reason = placement_problem(patches, path, line, start)
        if reason is None:
            inline.append(comment)
        else:
            unplaced.append({**comment, "reason": reason})
    return inline, unplaced


def placement_problem(
    patches: dict[str, dict[int, int] | None], path: str, line: int, start: int | None
) -> str | None:
    """Why GitHub could not place a comment on this diff, or None when it can."""
    if path not in patches:
        return "not a file this pull request changes"
    lines = patches[path]
    if lines is None:
        return "the file has no diff to anchor on"
    if line not in lines:
        return "not a line the diff shows"
    if start is not None and lines.get(start) != lines[line]:
        return "the range leaves its hunk"
    return None


def check(
    entry: dict[str, Any],
    verdict: dict[str, Any] | None,
    *,
    failure: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce the antagonistic check.json for one pull request.

    A verdict with nothing to post, every finding it read malformed and
    no defect flagged in its summary, is not commentable: posting it
    would read as a clean review, and the review would stop the next run
    looking at the head again. A flagged summary is a finding in its
    own right, so it posts and can go back for revision.
    """
    inline: list[dict[str, Any]] = []
    unplaced: list[dict[str, Any]] = []
    dropped: list[str] = []
    capped = 0
    reasons: list[str] = []
    commentable = False
    if verdict is None:
        reasons = [
            policy.sanitise(failure or policy.NO_VERDICT, policy.MAX_REASON_CHARS)
        ]
    elif (mismatch := policy.target_mismatch(entry, verdict)) is not None:
        reasons = [mismatch]
    else:
        inline, unplaced = anchor(entry, verdict["comments"])
        dropped = list(verdict["dropped"])
        capped = int(verdict["capped"])
        commentable = bool(
            inline or unplaced or verdict["summary_finding"] or not (dropped or capped)
        )
        if not commentable:
            reasons = ["every proposed finding read was malformed; nothing to post"]
    premium_requests, agent_seconds = policy.usage_numbers(usage)
    return {
        "schema": SCHEMA,
        "mode": MODE,
        "key": str(entry.get("key", "")),
        "repository": str(entry.get("repository", "")),
        "number": entry.get("number"),
        "head_sha": str(entry.get("head_sha", "")),
        "url": str(entry.get("url", "")),
        "title": policy.sanitise(entry.get("title"), policy.MAX_TITLE_CHARS),
        "commentable": commentable,
        "reasons": reasons,
        "summary": "" if verdict is None else verdict["summary"],
        "summary_finding": verdict is not None and verdict["summary_finding"] is True,
        "comments": inline,
        "unplaced": unplaced,
        "dropped": [
            policy.sanitise(reason, policy.MAX_REASON_CHARS)
            for reason in dropped[:MAX_DROPPED]
        ],
        "capped": capped,
        "injection_attempts": [] if verdict is None else verdict["injection_attempts"],
        "premium_requests": premium_requests,
        "agent_seconds": agent_seconds,
    }
