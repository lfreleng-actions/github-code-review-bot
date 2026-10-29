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
from collections.abc import Callable
from typing import Any, cast

import review_policy as policy
from review_policy import Rejected

SCHEMA = 1
MODE = "antagonistic"
SEVERITIES = ("blocker", "major", "minor", "nit")
MAX_COMMENTS = 25
MAX_COMMENT_CHARS = 2000
MAX_PATH_CHARS = 400
MAX_DROPPED = 50
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
# A fence GitHub renders as a one-click suggested commit. A human must
# write such a change, so the fence becomes a plain code block. Matched
# on a line that can open a fence: one whose run of fence characters
# follows nothing but a container's markers, as in a blockquote or a
# list item, where a fence still renders. Inside a fenced block the
# same text is code, and stays as written. Anchored, and the run is
# taken whole, so a long run is read once.
SUGGESTION_RE = re.compile(
    r"^([ \t>*+\-0-9.)]*+)(`{3,}+|~{3,}+)[ \t]*+suggestion\b", re.IGNORECASE
)
# Every control character except tab and newline.
BODY_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")
# An opening fence GitHub is sure to render as one, wherever the comment
# lands: at column 0, so at the top level whatever precedes it, since a
# fence never continues a list item, a blockquote or a paragraph
# lazily. A backtick fence's info string holds no backtick.
TOP_FENCE_RE = re.compile(r"^(`{3,}|~{3,})(.*)$")
# A line GitHub reads as a closing fence for an opener of its character
# and at most its length: indented at most three spaces, with nothing
# after the run.
CLOSER_RE = re.compile(r"^ {0,3}(`+|~+)[ \t]*$")
# A line GitHub might read as a fence that the scanner does not pair:
# indented, inside a blockquote or list item, or never closed. Left
# live, it could pair differently from the scanner and turn what the
# scanner left as code into prose GitHub renders, or the reverse; a
# zero-width space before its run means the only fences GitHub sees
# are the ones the scanner found.
FENCE_LIKE_RE = re.compile(r"^([ \t>*+\-0-9.)]*)(?=`{3,}|~{3,})", re.MULTILINE)
PATH_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")

__all__ = [
    "MODE",
    "SEVERITIES",
    "Rejected",
    "anchor",
    "check",
    "extract_verdict",
    "outside_code",
]


def fences(lines: list[str]) -> tuple[set[int], str | None]:
    """The lines of closed top-level fences, fence lines included, and any open one."""
    spans, dangling = fence_spans(lines)
    return {line for start, end in spans for line in range(start, end + 1)}, dangling


def fence_spans(lines: list[str]) -> tuple[list[tuple[int, int]], str | None]:
    """The first and last line of each closed top-level fence, and any open one.

    A closing fence repeats the opening character at least as many
    times, indented at most three spaces, with nothing after it, as
    GitHub reads it. An opening fence with no closer is not code here:
    the prose pass disarms it into text, so GitHub renders it as text
    too; its marker comes back so a body cut mid-block can close it.

    Linear in the lines: the longest closer still ahead tells at once
    whether an opener has one, and the search for an opener's closer
    covers lines the scan then skips past.
    """
    closers: list[tuple[str, int] | None] = []
    for line in lines:
        found = CLOSER_RE.match(line)
        closers.append(None if found is None else (found[1][0], len(found[1])))
    ahead = {"`": [0] * (len(lines) + 1), "~": [0] * (len(lines) + 1)}
    for index in range(len(lines) - 1, -1, -1):
        for longest in ahead.values():
            longest[index] = longest[index + 1]
        closer = closers[index]
        if closer is not None:
            ahead[closer[0]][index] = max(ahead[closer[0]][index], closer[1])
    spans: list[tuple[int, int]] = []
    dangling: str | None = None
    index = 0
    while index < len(lines):
        found = TOP_FENCE_RE.match(lines[index])
        if found is None or (found.group(1)[0] == "`" and "`" in found.group(2)):
            index += 1
            continue
        marker = found.group(1)
        if ahead[marker[0]][index + 1] < len(marker):
            dangling = dangling or marker
            index += 1
            continue
        end = index + 1
        while not closes(closers[end], marker):
            end += 1
        spans.append((index, end))
        index = end + 1
    return spans, dangling


def closes(closer: tuple[str, int] | None, marker: str) -> bool:
    """Whether a closing-fence line closes a block its marker opened."""
    return closer is not None and closer[0] == marker[0] and closer[1] >= len(marker)


def outside_code(text: str, transform: Callable[[str], str]) -> str:
    """Apply ``transform`` to the text outside fenced code, leaving code as written.

    GitHub links no mention, reference or URL inside a fenced block,
    so code needs no defusing, and code left alone pastes cleanly. Code
    spans are not skipped: telling one apart from stray backticks needs
    paragraph structure, and guessing wrong would leave a mention live.
    """
    lines = text.split("\n")
    code, _ = fences(lines)
    out: list[str] = []
    prose: list[str] = []
    for index, line in enumerate(lines):
        if index not in code:
            prose.append(line)
            continue
        if prose:
            out.append(transform("\n".join(prose)))
            prose = []
        out.append(line)
    if prose:
        out.append(transform("\n".join(prose)))
    return "\n".join(out)


def disarm_prose(text: str) -> str:
    """Disarm stray fences, mentions and references, and raw HTML, in prose."""
    unfenced = FENCE_LIKE_RE.sub(r"\1" + policy.ZWSP, text)
    return policy.disarm_html(policy.defuse(unfenced))


def plain_fences(text: str) -> str:
    """Turn each suggestion fence into a plain one, leaving fenced code as written.

    A fenced block's opening line is rewritten like any line in prose;
    the lines within it are code, so a suggestion fence quoted there
    stays as written. The rewrite keeps the fence run, so the fences
    pair as they did before it.
    """
    lines = text.split("\n")
    spans, _ = fence_spans(lines)
    within = {line for start, end in spans for line in range(start + 1, end + 1)}
    return "\n".join(
        line if index in within else SUGGESTION_RE.sub(r"\1\2text", line)
        for index, line in enumerate(lines)
    )


def comment_text(text: str, limit: int = MAX_COMMENT_CHARS) -> str:
    """Make untrusted Markdown safe to post as a review comment, keeping its lines.

    Outside fenced code, mentions and references are defused and raw
    HTML stays text, so a comment can never mention anyone, leave a
    cross-reference or load anything; inside, code stays as written.
    Suggestion fences become plain code blocks. A fence the body leaves
    open, as written or once cut to the limit, is closed, so it never
    swallows the rest of the review.
    """
    folded = BODY_CONTROL_RE.sub(" ", text.replace("\r\n", "\n"))
    plain = plain_fences(folded)
    escaped = outside_code(plain, disarm_prose).strip()
    if len(escaped) > limit:
        escaped = escaped[:limit].rstrip() + "..."
    return close_fence(escaped)


def close_fence(text: str) -> str:
    """Close a top-level code fence the text leaves open, with its own marker."""
    _, dangling = fences(text.split("\n"))
    return text if dangling is None else f"{text}\n{dangling}"


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

    A verdict with nothing to post, every finding it read malformed, is
    not commentable: posting it would read as a clean review, and the
    review would stop the next run looking at the head again.
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
        commentable = bool(inline or unplaced or not (dropped or capped))
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
