# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""How GitHub reads posted Markdown, modelled for the fuzz oracles.

The oracles judge a sanitiser's output by what GitHub would make of
it, never by the sanitiser's own patterns: this module imports
nothing from ``scripts/``. Each rule cites the specification it
follows, CommonMark 0.31 or the GitHub Flavored Markdown spec, or the
GitHub behaviour ``docs/DESIGN.md`` records (§8.3 and §19).
"""

from __future__ import annotations

import bisect
import html.entities
import re
from collections.abc import Iterator
from dataclasses import dataclass

ZWSP = "\u200b"
# CommonMark §2.4 and §6.2: a backslash before ASCII punctuation
# leaves the mark as text, and an entity or a decimal or hexadecimal
# character reference decodes to its character. DESIGN.md §8.3:
# GitHub decodes both before it links mentions and references.
TEXT_TOKEN = re.compile(
    r"\\([!-/:-@\[-`{-~])"
    r"|&#([0-9]{1,7});"
    r"|&#[xX]([0-9A-Fa-f]{1,6});"
    r"|&([A-Za-z][A-Za-z0-9]{0,31});"
)
# What GitHub links in text: an @-mention of a user or team, an issue
# or pull request number after ``#`` (``owner/repo#12`` included),
# ``GH-12``, and the number in an issue, pull or discussion URL.
LIVE_REFERENCES = (
    ("a mention", re.compile(r"@[A-Za-z0-9]")),
    ("an issue reference", re.compile(r"#[0-9]")),
    ("a GH- reference", re.compile(r"(?<!\w)gh-[0-9]", re.IGNORECASE)),
    (
        "a GitHub URL reference",
        re.compile(r"/(?:issues|pulls?|discussions)/[0-9]", re.IGNORECASE),
    ),
)
# CommonMark §6.6 and §6.5: raw HTML opens with ``<`` and a tag name,
# ``/``, ``!`` or ``?``; an autolink with ``<`` and a scheme.
RAW_HTML = re.compile(r"<[A-Za-z/!?]")
# GFM §6.9: GitHub autolinks ``www.``, ``http(s)://`` and ``ftp://`` text.
WWW = re.compile(r"www\.", re.IGNORECASE | re.ASCII)
SCHEME = re.compile(r"(?:https?|ftp)://", re.IGNORECASE | re.ASCII)
# A scheme with any zero-width spaces between its characters, each of
# which breaks it, kept whole; or any other zero-width space.
SCHEME_MARK = re.compile(
    f"((?:h{ZWSP}*t{ZWSP}*t{ZWSP}*p(?:{ZWSP}*s)?|f{ZWSP}*t{ZWSP}*p)"
    f"{ZWSP}*:{ZWSP}*/{ZWSP}*/)|{ZWSP}",
    re.IGNORECASE | re.ASCII,
)
# DESIGN.md §8.3: a link runs to the next ASCII whitespace or ``<``,
# and GitHub trims trailing punctuation and an unmatched ``)``. GFM
# §6.9 lists ``?!.,:*_~`` as the punctuation; cmark-gfm also trims
# quotes. A link counts as inside only when it is inside either way.
URL_END = re.compile(r"[\s<]", re.ASCII)
TRAILING = "?!.,:*_~"
QUOTES = "'\""
# WHATWG URL, path state: a ``.`` or ``..`` segment, spelt with dots
# or ``%2e``, resolves away. Read loosely, so ``/..x`` counts too.
DOT_SEGMENT = re.compile(
    r"[/\\](?:\.|%2e){1,2}(?![A-Za-z0-9])", re.IGNORECASE | re.ASCII
)
# What may follow ``https://github.com/<org>`` to the end of a run for
# containment to keep the URL: GFM §6.9's trailing punctuation and a
# ``)``, which GitHub trims.
ROOT_TAIL = re.compile(r"[)?!.,:*_~]*")
# CommonMark §5.1 to §5.3: block quote and list item markers.
CONTAINER = re.compile(r"(?:[ \t]*(?:>|[*+-][ \t]|[0-9]{1,9}[.)][ \t]))*[ \t]*")


class Breach(AssertionError):
    """An output breaks the contract its harness checks."""


@dataclass(frozen=True)
class Autolink:
    """A URL from its scheme: where it starts, if inside, how far it links."""

    start: int
    url: str
    inside: bool
    reach: int


@dataclass(frozen=True)
class Block:
    """A fenced code block: the line it opens on, its info string, its body."""

    opener: int
    info: str
    body: list[str]
    closed: bool


def text_view(markdown: str) -> str:
    """The text GitHub links mentions in: escapes dropped, references decoded."""

    def one(found: re.Match[str]) -> str:
        if found[1] is not None:
            return found[1]
        if found[2] is not None or found[3] is not None:
            code = int(found[2], 10) if found[2] is not None else int(found[3], 16)
            invalid = code == 0 or code > 0x10FFFF or 0xD800 <= code <= 0xDFFF
            return "\ufffd" if invalid else chr(code)
        return html.entities.html5.get(found[4] + ";", found[0])

    return TEXT_TOKEN.sub(one, markdown)


def live_references(markdown: str) -> list[str]:
    """Each mention, reference or raw HTML tag GitHub would render live."""
    text = text_view(markdown)
    live = [
        f"{name} at {found.start()}: {text[found.start() : found.end() + 8]!r}"
        for name, pattern in LIVE_REFERENCES
        for found in pattern.finditer(text)
    ]
    live.extend(f"raw HTML at {found.start()}" for found in RAW_HTML.finditer(markdown))
    return live


def ascii_equal(text: str, name: str) -> bool:
    """Whether ``text`` spells ``name`` with ASCII case folded alone.

    GitHub matches organisation and host names over ASCII: the Kelvin
    sign lowercases to ``k`` but names no organisation.
    """
    return text.isascii() and text.lower() == name.lower()


def resolves_inside(path: str, org: str) -> bool:
    """Whether a URL path, resolved as a browser would, stays under ``/<org>``.

    A backslash separates segments, and ``%2e`` spells a dot (WHATWG
    URL, path state). The first resolved segment, whole, must name
    ``org``: ``/<org>)x/`` is another path, not one under ``/<org>``.
    """
    resolved: list[str] = []
    for segment in path.replace("\\", "/").split("/")[1:]:
        plain = re.sub("%2e", ".", segment, flags=re.IGNORECASE)
        if plain == "..":
            if resolved:
                resolved.pop()
        elif plain != ".":
            resolved.append(segment)
    return bool(resolved) and ascii_equal(resolved[0], org)


def trimmed(url: str, trailing: str = TRAILING) -> str:
    """The URL GitHub links: ``trailing`` and an unmatched ``)`` trimmed."""
    while url and (
        url[-1] in trailing or (url[-1] == ")" and url.count(")") > url.count("("))
    ):
        url = url[:-1]
    return url


def url_inside(url: str, org: str) -> bool:
    """Whether a live URL points into the organisation, quotes trimmed or not."""
    return linked_inside(trimmed(url), org) and linked_inside(
        trimmed(url, TRAILING + QUOTES), org
    )


def linked_inside(linked: str, org: str) -> bool:
    """Whether the URL GitHub links, already trimmed, is under ``/<org>``."""
    if not linked.lower().startswith("https://"):
        return False
    rest = linked[len("https://") :]
    host_end = min(
        (rest.find(mark) for mark in "/\\?#" if mark in rest), default=len(rest)
    )
    if not ascii_equal(rest[:host_end], "github.com"):
        return False
    path = re.split(r"[?#]", rest[host_end:], maxsplit=1)[0]
    return resolves_inside(path, org)


def autolinks(markdown: str, org: str) -> Iterator[Autolink]:
    """Each URL GitHub may autolink from its scheme, and whether it stays inside.

    A URL runs to the next ASCII whitespace or ``<``. One inside the
    organisation is on ``github.com``, a host GitHub always links, so a
    scheme later in its run is part of that link and starts none of its
    own (GFM §6.9), if the URL is surely a link. cmark-gfm reads a
    scheme back over ASCII letters, so it links none after one
    (``xhttps://``), and a backtick may close a code span the URL sits
    in (CommonMark §6.1), so the link reaches no further than the first
    backtick. The scan resumes where it ends. Any other URL is read from
    each scheme it holds, as GitHub may link none of it.
    """
    at = 0
    while (found := SCHEME.search(markdown, at)) is not None:
        end = URL_END.search(markdown, found.start())
        stop = len(markdown) if end is None else end.start()
        url = markdown[found.start() : stop]
        inside = url_inside(url, org)
        before = markdown[found.start() - 1 : found.start()]
        reach = 0
        if inside and not (before.isascii() and before.isalpha()):
            tick = url.find("`")
            reach = len(url) if tick < 0 else tick
        yield Autolink(found.start(), url, inside, reach)
        at = found.start() + reach if reach else found.end()


def live_links(markdown: str, org: str) -> list[str]:
    """Each live link outside ``https://github.com/<org>/`` GitHub would render.

    A cut URL is read as GitHub links it, cut mark and all: a prefix of
    an organisation URL, ``https://github.com/<o…``, links outside it.
    """
    live: list[str] = []
    for syntax in ("](", "]:"):
        if syntax in markdown:
            live.append(f"link syntax {syntax!r} at {markdown.index(syntax)}")
    live.extend(f"www. link at {found.start()}" for found in WWW.finditer(markdown))
    live.extend(
        f"link {link.url!r}" for link in autolinks(markdown, org) if not link.inside
    )
    return live


def scheme_marks(text: str) -> str:
    """``text`` with each zero-width space gone but those inside a scheme."""
    return SCHEME_MARK.sub(lambda found: found[1] or "", text)


def kept_links(run: str, org: str) -> Iterator[tuple[int, int, str]]:
    """Each link in ``run`` to keep as written: the backticks before it,
    where it starts, and the text it surely links.

    A link ends a stretch between backticks (``autolinks``), so the
    count of backticks before it names the stretch it ends.
    """
    ticks = [at for at, char in enumerate(run) if char == "`"]
    for link in autolinks(run, org):
        if link.reach:
            yield bisect.bisect(ticks, link.start), link.start, link.url[: link.reach]


def always_kept(run: str, start: int, kept: str, org: str, last_dot: int) -> bool:
    """Whether containment keeps the link ``kept``, at ``start`` in ``run``.

    It keeps one naming ``https://github.com/<org>`` and then a ``/``,
    or nothing to the end of the run but what GitHub trims, when no dot
    segment in the run comes after its scheme starts (``last_dot`` is
    where the last one does).
    """
    base = f"https://github.com/{org}"
    if last_dot >= start or not ascii_equal(kept[: len(base)], base):
        return False
    tail = kept[len(base) :]
    return tail.startswith("/") or (
        start + len(kept) == len(run) and ROOT_TAIL.fullmatch(tail) is not None
    )


def altered_links(source: str, result: str, org: str) -> list[str]:
    """Each organisation link a broken scheme changed from ``source`` to ``result``.

    ``result`` is ``source`` with zero-width spaces added, none of them
    ASCII whitespace, ``<`` or a backtick, so the two split into the
    same runs, and each run into the same stretches between backticks.
    A link inside the organisation is one to keep live and as written
    as far as it surely reaches: a scheme within that starts no link
    (``autolinks``), so breaking it, after its colon or between any two
    of its characters, only changes where the link goes. Each such link
    in ``result`` must read as ``source`` wrote it. So must each in
    ``source`` that containment always keeps (``always_kept``), even
    when a broken scheme leaves it no link in ``result``.
    Other marks, after ``]`` or ``www``, are left out of the comparison.
    """
    sources = URL_END.split(source)
    results = URL_END.split(result)
    if len(sources) != len(results):
        return ["the text around the links moved"]
    altered: list[str] = []
    for written, run in zip(sources, results, strict=True):
        stretches = written.split("`")
        contained = run.split("`")
        if len(stretches) != len(contained):
            altered.append("the backticks around the links moved")
            continue
        checks = [(stretches[index], kept) for index, _, kept in kept_links(run, org)]
        last_dot = max(
            (dot.start() for dot in DOT_SEGMENT.finditer(written)), default=-1
        )
        checks.extend(
            (contained[index], kept)
            for index, start, kept in kept_links(written, org)
            if always_kept(written, start, kept, org, last_dot)
        )
        for other, kept in checks:
            if not scheme_marks(other).endswith(scheme_marks(kept)):
                altered.append(f"a scheme broken in the kept link {kept!r}")
    return altered


def indent_of(line: str) -> int:
    """The columns of leading whitespace, tabs to the next multiple of four."""
    column = 0
    for char in line:
        if char == " ":
            column += 1
        elif char == "\t":
            column += 4 - column % 4
        else:
            break
    return column


def fence_run(line: str) -> tuple[str, str] | None:
    """The fence run and info string a line opens with (CommonMark §4.5)."""
    if indent_of(line) > 3:
        return None
    found = re.match(r"(`{3,}|~{3,})(.*)", line.lstrip(" \t"))
    if found is None or (found[1][0] == "`" and "`" in found[2]):
        return None
    return found[1], found[2]


def closes(line: str, run: str) -> bool:
    """Whether a line closes a block ``run`` opened (CommonMark §4.5)."""
    if indent_of(line) > 3:
        return False
    stripped = line.lstrip(" \t")
    width = len(stripped) - len(stripped.lstrip(run[0]))
    return width >= len(run) and not stripped[width:].strip(" \t")


def blocks(lines: list[str]) -> Iterator[Block]:
    """The top-level fenced code blocks GitHub renders, in order.

    CommonMark §4.5: an unclosed block runs to the end of the document.
    """
    index = 0
    while index < len(lines):
        opened = fence_run(lines[index])
        if opened is None:
            index += 1
            continue
        run, info = opened
        end = index + 1
        while end < len(lines) and not closes(lines[end], run):
            end += 1
        yield Block(index, info, lines[index + 1 : end], end < len(lines))
        index = end + 1


def prose_lines(lines: list[str]) -> list[str]:
    """The lines GitHub renders as prose: every line outside fenced code."""
    code: set[int] = set()
    for block in blocks(lines):
        code.update(range(block.opener, block.opener + len(block.body) + 2))
    return [line for index, line in enumerate(lines) if index not in code]


def opens_suggestion(line: str) -> bool:
    """Whether a line opens a suggestion fence, in a container or not.

    The container markers and the fence run are read raw: an escaped
    or encoded backtick is text (CommonMark §2.4, §6.2), never a fence.
    GitHub decodes references in an info string before it reads the
    language (DESIGN.md §19), so the info string alone is decoded. The
    language is its first word, and ``suggestion:-0+2`` names one too,
    with the lines it replaces around the comment's own.
    """
    found = CONTAINER.match(line)
    opened = re.match(r"(`{3,}|~{3,})(.*)", line[found.end() if found else 0 :])
    if opened is None:
        return False
    language = re.match(r"[ \t]*(\S*)", text_view(opened[2]))
    word = language[1].lower() if language is not None else ""
    return word == "suggestion" or word.startswith("suggestion:")


def require(breaches: list[str], what: str) -> None:
    """Raise ``Breach`` naming each breach found, if any."""
    if breaches:
        raise Breach(f"{what}: " + "; ".join(breaches[:5]))
