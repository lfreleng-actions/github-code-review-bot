# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""How a text field reads once ``review_policy.sanitise`` has kept it.

The verdict oracle holds each retained field to its source: "Fold
untrusted text into one bounded line safe for logs and Markdown". The
field reads as the source, folded, with only the marks the sanitiser
adds where it adds them, and cut at the limit when too long. Like
``github_markdown``, this module imports nothing from ``scripts/``.
"""

from __future__ import annotations

import re

from github_markdown import Breach

CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
SURROGATE = re.compile("[\ud800-\udfff]")
ZWSP = "\u200b"
CUT = "..."
FENCE_RUNS = ("```", "~~~")
# What a sanitiser breaks with a zero-width space, read on the source:
# the text before the mark, and the text the source carries on with.
# A mention or a raw HTML tag after ``@`` or ``<``, an issue number
# after ``#``, ``GH-12`` or ``GH\\-12`` after the ``h`` of a ``gh``
# that starts a word, and the number after an issue, pull or discussion
# path. Case folds as Python's does, Unicode included.
ZWSP_TRIGGERS = (
    (re.compile(r"[@<]\Z"), re.compile(f"(?!{ZWSP})")),
    (re.compile(r"#\Z"), re.compile(r"[0-9]")),
    (re.compile(r"(?<!\w)gh\Z", re.IGNORECASE), re.compile(r"\\?-[0-9]")),
    (
        re.compile(r"/(?:issues|pulls?|discussions)\\?/\Z", re.IGNORECASE),
        re.compile(r"[0-9]"),
    ),
)
# The longest text before a mark a trigger reads, ``/discussions\\/``,
# and one character more for the start of a word.
TRIGGER_CHARS = 16
# What follows the ``&`` of a character reference, which a sanitiser
# escapes as ``&amp;``: ``&amp;`` itself stays as written.
ENTITY = re.compile(r"(?!amp;)#?[A-Za-z0-9]+;")


def folded(text: str) -> str:
    """Text as one line, each control character and whitespace run a space.

    A lone surrogate reads as U+FFFD, and the line is trimmed at both
    ends.
    """
    return " ".join(CONTROL.sub(" ", SURROGATE.sub("\ufffd", text)).split())


def zwsp_due(source: str, matched: int) -> bool:
    """Whether a sanitiser adds a zero-width space after ``source[:matched]``.

    Before a fence run that opens the text, or where the source spells
    out one of ``ZWSP_TRIGGERS`` in full around that point.
    """
    if not matched:
        return source.startswith(FENCE_RUNS)
    before = source[max(0, matched - TRIGGER_CHARS) : matched]
    return any(
        behind.search(before) is not None and after.match(source, matched) is not None
        for behind, after in ZWSP_TRIGGERS
    )


def colon_pairs(source: str) -> frozenset[int]:
    """Where each ``::`` the sanitiser breaks starts in ``source``.

    "folded.replace('::', ': :')" breaks the pairs a scan from the left
    finds, which never overlap: ``:::`` holds one, at its start. Folding
    whitespace neither joins nor parts two colons, so the folded source
    holds the same pairs.
    """
    return frozenset(found.start() for found in re.finditer("::", source))


def mark_at(
    kept: str, at: int, source: str, matched: int, cut: bool, pairs: frozenset[int]
) -> int:
    """How many characters of ``kept`` from ``at`` are a mark, not text.

    A sanitiser may add a zero-width space where ``zwsp_due`` finds a
    trigger in the source; a space after the first colon of a pair in
    ``pairs``, or between ``#`` and ``#[``, which breaks a
    workflow-command marker, or the ``amp;`` of ``escaped_amp``. A text
    that was cut may end part way through a mark. Zero otherwise: the
    character must then be the source's own.
    """
    char = kept[at]
    before = kept[at - 1] if at else ""
    after = kept[at + 1 :]
    if char == ZWSP:
        return int(zwsp_due(source, matched))
    if char == " " and before == ":":
        return int(matched - 1 in pairs and after.startswith(":"))
    if char == " " and before == "#":
        return int(after.startswith("#[") or (cut and after == "#"))
    return 0


def escaped_amp(kept: str, at: int, source: str, matched: int, cut: bool) -> int | None:
    """The length of the ``amp;`` due at ``at``, or of its start a cut kept.

    One is due right after the ``&`` of a character reference in the
    source (``ENTITY``), and nowhere else: zero where none is due, None
    where one is due and ``kept`` does not carry it.
    """
    if kept[at - 1 : at] != "&" or ENTITY.match(source, matched) is None:
        return 0
    mark = kept[at : at + 4]
    if mark == "amp;" or (
        cut and at + len(mark) == len(kept) and "amp;".startswith(mark)
    ):
        return len(mark)
    return None


def aligned(kept: str, source: str, cut: bool = False) -> tuple[int, int]:
    """How far ``kept`` reads as ``source``: the characters of each read.

    Each character of ``kept`` is the source's next one or a mark a
    sanitiser adds (``mark_at``, ``escaped_amp``); the first that is
    neither stops the reading, as does an escape due and missing. A
    trigger takes one zero-width space; zero-width spaces the source
    carries are its own text.
    """
    pairs = colon_pairs(source)
    at = matched = 0
    marked = -1
    while at < len(kept):
        escape = escaped_amp(kept, at, source, matched, cut) if at else 0
        if escape is None:
            break
        if escape:
            at += escape
            continue
        if matched < len(source) and kept[at] == source[matched]:
            at += 1
            matched += 1
            continue
        zwsp = kept[at] == ZWSP
        repeated = zwsp and marked == matched
        mark = 0 if repeated else mark_at(kept, at, source, matched, cut, pairs)
        if not mark:
            break
        if zwsp:
            marked = matched
        at += mark
    return at, matched


def space_cut(raw: str, whole: str, read: int) -> bool:
    """Whether the escaped source carries a space right after ``raw``.

    The source's own, or a mark ``rstrip`` takes as one: the space
    between the colons of a pair, or between ``#`` and ``#[``. The
    escaped text holds no other space and never ends on one, so it then
    runs on past the space too. A zero-width space due after ``raw``
    and not yet in it comes first, and ``rstrip`` keeps it.
    """
    if mark_left(raw, whole, read):
        return False
    if whole[read : read + 1] == " ":
        return True
    if raw.endswith(":"):
        return read - 1 in colon_pairs(whole)
    return raw.endswith("#") and whole.startswith("#[", read)


def mark_left(raw: str, whole: str, read: int) -> bool:
    """Whether a mark due after ``read`` characters of ``whole`` lies beyond ``raw``.

    ``raw`` read them; the escaped text carries a zero-width space next
    only where one is due there and ``raw`` did not take it. Ending on
    one, ``raw`` took it: the source's own would leave none due.
    """
    return zwsp_due(whole, read) and not raw.endswith(ZWSP)


def verify_text(kept: str, source: object, limit: int, what: str) -> None:
    """A retained text field reads as its source field, or as its start when cut.

    ``sanitise`` folds the text into one line and adds its marks
    (``mark_at``); "if len(collapsed) > limit: return
    collapsed[:limit].rstrip() + '...'". So an uncut text fits the
    limit, and a cut is due only when the escaped source runs past it.
    A cut keeps the first ``limit`` characters, less a trailing space,
    and reads as the start of its source. Kept whole, those characters
    leave out some of the source's text, or a mark after the last of
    them; one short, the next was a space (``space_cut``).
    """
    if not isinstance(source, str):
        raise Breach(f"{what} accepted from a field that is not a string")
    whole = folded(source)
    if len(kept) <= limit and aligned(kept, whole) == (len(kept), len(whole)):
        return
    raw = kept.removesuffix(CUT)
    done, read = aligned(raw, whole, cut=True)
    cut = (
        kept.endswith(CUT)
        and done == len(raw)
        and (
            (len(raw) == limit and (read < len(whole) or mark_left(raw, whole, read)))
            or (len(raw) == limit - 1 and space_cut(raw, whole, read))
        )
    )
    if not cut:
        raise Breach(f"{what} does not read as the final block's")
