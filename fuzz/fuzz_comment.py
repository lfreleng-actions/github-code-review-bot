# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Fuzz the comment Markdown pass of ``comment_markdown``.

"Make an antagonistic agent's comment Markdown safe to post, code kept
as written." The first input byte picks the limit, the rest is the
comment body, decoded as UTF-8 with replacement. Both the check's
``comment_text`` and the body ``comment_body.fitted`` posts from it
are held to what the code documents:

- ``fit``: "the result never passes the limit", and "a block left
  open never swallows what follows": GitHub, reading the result as
  CommonMark does, finds no fenced block left open.
- ``comment_text``: "Outside fenced code, mentions and references are
  defused and raw HTML stays text, so a comment can never mention
  anyone, leave a cross-reference or load anything; inside, code stays
  as written. Suggestion fences become plain code blocks." A fenced
  block GitHub is sure to render as code is one that opens at column
  0 with a run of at most ``MAX_FENCE_RUN`` (10) and closes
  (DESIGN.md §19); these blocks, and no others, come back as written,
  in order and once per occurrence, control characters folded, in both
  the check's result and the posted body, whenever the body is short
  enough not to be cut: its outer whitespace stripped, its code at its
  own size, and the rest grown by the most each character and line
  outside it can gain.
- ``comment_body.contained``: "Untrusted Markdown with no live link
  outside the organisation, code aside." A link into the organisation
  stays as written (``review_policy.contain_links``).

Run by hand with ``python fuzz/fuzz_comment.py``; see the README.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from importlib import import_module
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "scripts")]

from github_markdown import (  # noqa: E402
    Breach,
    altered_links,
    blocks,
    live_links,
    live_references,
    opens_suggestion,
    prose_lines,
    require,
)

markdown = import_module("comment_markdown")
body = import_module("comment_body")

ORG = "lfreleng-actions"
MAX_FENCE_RUN = 10
# Every control character but tab and newline folds to a space.
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")
# Outside fenced code, the ``&`` that starts a character reference can
# grow to ``&amp;``, four characters more; any other stays as written,
# ``&amp;`` itself among them.
# A zero-width space can follow a character a mention, a reference, raw
# HTML, a link or a definition starts or turns on, or the scheme of a
# URL ends on: ``@ # < h H / ] :``, or come before the ``.`` of
# ``www.`` or the fence run that opens a line, one per line.
# Code stays as written. A body whose code and grown rest fit the limit
# is never cut.
AMP_GROWTH = 4
ENTITY = re.compile(r"&(?!amp;)(?=#?[A-Za-z0-9]+;)")
GROWS = "@#<hH/]:."
OPENER = re.compile(r"(`{3,}+|~{3,}+)(.*)")
# A space other than a blank or a tab after the run leaves no closer.
CLOSER = re.compile(r" {0,3}+(`++|~++)[ \t]*+")


def limit_of(data: bytes) -> int:
    """The limit the first byte picks: 64 to 2104, the default 2000 among them."""
    return 2000 if not data or data[0] == 0xFF else 64 + data[0] * 8


SEEDS: list[bytes] = [
    b"\xffcc @team, see #12, org/repo#3 and GH-4 <img src=x>\n"
    b"```python\n@decorator\ndef f(): return '#1 <b>'\n```\n"
    b"[the issue](https://github.com/org/repo/issues/9) www.evil.example\n",
    b"\xff```suggestion\nretry(read)\n```\n> ```Suggestion\n> y\n> ```\n"
    b"- list:\n  ~~~~suggestion\n  z\n  ~~~~\n```sugges&#116;ion\nw\n```\n",
    b"\x10" + b"```\n" + b"x @y\n" * 600 + b"```\ntail\n",
    b"\xff" + b"````\n```\nnested\n```\n````\n" + b"~~~ unclosed\n@a\n",
    b"\x20" + b"https://github.com/lfreleng-actions/repo/pull/" * 40,
    b"\xff" + b"`" * 11 + b"\nlong run @x\n" + b"`" * 11 + b"\n",
    b"\x00" + "\r\n\u2028\x00\x1b[31m&#64;x &commat;y\t\\@z".encode(),
    b"\xff" + b"```\n" * 300,
    # Found by fuzzing: an indented first line is no fence, even after
    # the strip that leaves it at column 0.
    b"\xff\x00```\n```\n```\n``\n```\n\x00```\nx\n```\n",
    # Reported in review: GFM autolinks ftp:// as well as http(s)://.
    b"\xffsee ftp://evil.example/x\n",
    # Reported in review: the cut left https://github.com/lfr\u2026 live.
    b"\x00" + b"http://x " * 3 + b"https://github.com/lfreleng-actions/",
    # Reported in review: a code span or a letter keeps no later scheme.
    b"\xff`https://github.com/lfreleng-actions/r`_https://evil.example\n"
    b"xhttps://github.com/lfreleng-actions/r_https://evil.example\n",
    # Reported in review: a scheme inside a kept organisation link is
    # part of it, and stays as written.
    b"\xffsee https://github.com/lfreleng-actions/repo/search?q=ftp://server\n",
    # Reported in review: ``&amp;`` and outer whitespace never grow a body.
    b"\xff" + b"&amp; " * 250 + b"\n```\nimportant\n```",
    b"\xff" + b" " * 2000 + b"\n```\nimportant\n```",
]


def folded(text: str) -> str:
    """The body with CRLF line ends and control characters folded."""
    return CONTROL.sub(" ", text.replace("\r\n", "\n"))


def written_code(text: str) -> list[list[str]]:
    """The bodies of the fenced blocks GitHub is sure to render as code.

    Each opens at column 0 with a run of three to ``MAX_FENCE_RUN``
    backticks or tildes, a backtick run with no backtick after it, and
    closes on a line of at least as many of the same character,
    indented at most three spaces, with nothing after the run but
    blanks and tabs. An opener nothing closes is prose.
    """
    lines = text.split("\n")
    closing = closers(lines)
    found: list[list[str]] = []
    index = 0
    while index < len(lines):
        end = closing.get(index)
        if end is None:
            index += 1
            continue
        found.append(lines[index + 1 : end])
        index = end + 1
    return found


def closers(lines: list[str]) -> dict[int, int]:
    """The line that closes each opener in ``lines``, if one does.

    Each line is read once, from the last back, keeping the nearest
    closer for each fence character and width an opener can have, so
    no opener reads the lines after it again.
    """
    nearest: dict[tuple[str, int], int] = {}
    closing: dict[int, int] = {}
    for index in range(len(lines) - 1, -1, -1):
        opener = OPENER.match(lines[index])
        if (
            opener is not None
            and len(opener[1]) <= MAX_FENCE_RUN
            and not (opener[1][0] == "`" and "`" in opener[2])
        ):
            end = nearest.get((opener[1][0], len(opener[1])))
            if end is not None:
                closing[index] = end
        closer = CLOSER.fullmatch(lines[index])
        if closer is not None:
            run = closer[1]
            for width in range(3, min(len(run), MAX_FENCE_RUN) + 1):
                nearest[run[0], width] = index
    return closing


def verify_safe(result: str, limit: int, what: str) -> None:
    """``result`` fits, leaves no block open, and renders nothing live."""
    breaches: list[str] = []
    if len(result) > limit:
        breaches.append(f"{len(result)} characters, over the limit of {limit}")
    lines = result.split("\n")
    for block in blocks(lines):
        if not block.closed:
            breaches.append(f"the fence on line {block.opener + 1} is left open")
        if opens_suggestion(lines[block.opener]):
            breaches.append(f"a suggestion fence on line {block.opener + 1}")
    prose = prose_lines(lines)
    breaches.extend(
        f"{line!r} opens a suggestion" for line in prose if opens_suggestion(line)
    )
    breaches.extend(live_references("\n".join(prose)))
    require(breaches, what)


def growth(text: str) -> int:
    """The most the characters of ``text`` can grow outside fenced code."""
    escapes = AMP_GROWTH * len(ENTITY.findall(text))
    return escapes + sum(text.count(char) for char in GROWS)


def verify_code_kept(source: str, result: str, limit: int) -> None:
    """The blocks sure to render as code, and no others, come back as written.

    The rendered blocks are the written ones, uncut, in order and once
    per occurrence: none dropped, duplicated or added. ``comment_text``
    strips the outer whitespace before it fits the body to the limit,
    so only what is left counts against it. A body longer than the
    limit once stripped may be cut whatever its code, so the check of
    its code is skipped for one. The
    blocks are read from the body before the strip, as the scanner
    reads them, so an indented fence never reaches column 0.
    """
    text = folded(source)
    kept = text.strip()
    if len(kept) > limit:
        return
    written = written_code(text)
    code = [line for block in written for line in block]
    grown = growth(kept) - sum(growth(line) for line in code) + kept.count("\n") + 1
    if len(kept) + grown > limit:
        return
    rendered = [block.body for block in blocks(result.split("\n"))]
    if rendered != written:
        raise Breach(
            f"fenced code {[block[:3] for block in rendered][:3]!r} is not "
            f"{[block[:3] for block in written][:3]!r} as written"
        )


def verify_contained(result: str) -> None:
    """The posted body has no live link outside the organisation, code aside."""
    prose = "\n".join(prose_lines(result.split("\n")))
    require(live_links(prose, ORG), "posted body")


def verify_links_kept(source: str, contained: str) -> None:
    """Each link containment keeps reads as ``source`` wrote it."""
    require(altered_links(source, contained, ORG), "contained body")


def check(data: bytes) -> None:
    """Run one input through every contract this harness checks."""
    limit = limit_of(data)
    source = data[1:].decode("utf-8", errors="replace")
    checked = markdown.comment_text(source, limit)
    verify_safe(checked, limit, "comment_text")
    verify_code_kept(source, checked, limit)
    posted = body.fitted(checked, ORG, limit)
    verify_safe(posted, limit, "posted body")
    verify_code_kept(source, posted, limit)
    verify_contained(posted)
    verify_links_kept(checked, body.contained(checked, ORG))


def main() -> None:
    """Fuzz ``check``, from the seeds unless an argument names an input file."""
    import atheris  # type: ignore[import-not-found]  # pyright: ignore[reportMissingImports]

    fuzzer: Any = atheris

    args = list(sys.argv)
    if not any(os.path.isfile(arg) for arg in args[1:]):
        seeds = tempfile.mkdtemp(prefix="fuzz-comment-")
        for index, seed in enumerate(SEEDS):
            Path(seeds, f"seed-{index}").write_bytes(seed)
        args.append(seeds)
    fuzzer.instrument_all()
    fuzzer.Setup(args, check)
    fuzzer.Fuzz()


if __name__ == "__main__":
    main()
