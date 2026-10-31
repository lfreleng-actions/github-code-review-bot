# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Make an antagonistic agent's comment Markdown safe to post, code kept as written.

Split from ``review_comments``, which validates and anchors the
comments, so the Markdown rules read in one place: which fences GitHub
will pair, what in the prose is disarmed, and what in fenced code is
left alone. Every pass here reads a body once, in time linear in its
length, so no session can hold the trusted job that runs them.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import review_policy as policy

MAX_COMMENT_CHARS = 2000
# The longest fence run that opens a block here. Nesting code never
# needs more, so a longer run is not a fence: the prose pass disarms
# it like any stray fence. A cut body can then always fit the closer it
# may need, its marker no longer than this, inside the limit.
MAX_FENCE_RUN = 10
# What ends a cut body. One character GitHub neither trims from a link
# nor links itself: three dots after a cut URL are trimmed, and could
# leave it ending in a dot segment, and ``www`` and a dot links.
CUT_MARK = "\u2026"
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

__all__ = ["MAX_COMMENT_CHARS", "comment_text", "fences", "fit", "outside_code"]


def fences(lines: list[str]) -> tuple[set[int], str | None]:
    """The lines of closed top-level fences, fence lines included, and any open one."""
    spans, dangling = fence_spans(lines)
    return {line for start, end in spans for line in range(start, end + 1)}, dangling


def fence_spans(lines: list[str]) -> tuple[list[tuple[int, int]], str | None]:
    """The first and last line of each closed top-level fence, and any open one.

    A closing fence repeats the opening character at least as many
    times, indented at most three spaces, with nothing after it, as
    GitHub reads it. An opening fence with no closer, or a run longer
    than ``MAX_FENCE_RUN``, is not code here: the prose pass disarms
    it into text, so GitHub renders it as text too. An unclosed
    opener's marker comes back so a body cut mid-block can close it.

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
        if (
            found is None
            or len(found.group(1)) > MAX_FENCE_RUN
            or (found.group(1)[0] == "`" and "`" in found.group(2))
        ):
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
    stays as written. GitHub decodes a character reference in an info
    string before it reads the language, so ``sugges&#116;ion`` opens a
    suggestion too: an opener's references stay literal. Neither
    rewrite touches the fence run, so the fences pair as they did.
    """
    lines = text.split("\n")
    spans, _ = fence_spans(lines)
    openers = {start for start, _ in spans}
    within = {line for start, end in spans for line in range(start + 1, end + 1)}
    out: list[str] = []
    for index, line in enumerate(lines):
        if index in within:
            out.append(line)
            continue
        if index in openers:
            line = policy.ENTITY_RE.sub("&amp;", line)
        out.append(SUGGESTION_RE.sub(r"\1\2text", line))
    return "\n".join(out)


def comment_text(text: str, limit: int = MAX_COMMENT_CHARS) -> str:
    """Make untrusted Markdown safe to post as a review comment, keeping its lines.

    Outside fenced code, mentions and references are defused and raw
    HTML stays text, so a comment can never mention anyone, leave a
    cross-reference or load anything; inside, code stays as written.
    Suggestion fences become plain code blocks, and the result is
    fitted to the limit.
    """
    folded = BODY_CONTROL_RE.sub(" ", text.replace("\r\n", "\n"))
    plain = plain_fences(folded)
    return fit(outside_code(plain, disarm_prose).strip(), limit)


def fit(text: str, limit: int = MAX_COMMENT_CHARS) -> str:
    """Cut text to the limit, leaving no fenced block open.

    Text over the limit is cut short enough that the cut mark and any
    closing fence the cut leaves it needing fit within it, so a block
    left open never swallows what follows and the result never passes
    the limit. Text already within it comes back as it was.
    """
    if len(text) <= limit:
        return text
    room = limit - len(CUT_MARK) - len("\n") - MAX_FENCE_RUN
    return close_fence(text[:room].rstrip() + CUT_MARK)


def close_fence(text: str) -> str:
    """Close a top-level code fence the text leaves open, with its own marker."""
    _, dangling = fences(text.split("\n"))
    return text if dangling is None else f"{text}\n{dangling}"
