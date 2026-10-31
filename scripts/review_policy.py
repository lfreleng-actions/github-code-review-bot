# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Decide whether an agent's review verdict may become an approval.

The agent's final message is untrusted text. ``extract_verdict``
reads it into a bounded, validated shape; ``check`` corroborates the
claimed tier against the diff, applies the vetoes and the enabled
tiers, and produces the ``check.json`` contract the apply step acts
on. Nothing here talks to GitHub.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, cast

from review_vetoes import (
    ACTION_RE,
    SHA_RE,
    WORKFLOW_RE,
    classify_trivial,
    find_vetoes,
)

__all__ = [
    "ACTION_RE",
    "WORKFLOW_RE",
    "Rejected",
    "check",
    "classify_trivial",
    "contain_links",
    "defuse",
    "disarm_html",
    "extract_verdict",
    "find_vetoes",
    "parse_approve_tiers",
    "sanitise",
]

SCHEMA = 1
TIERS = ("trivial", "low-risk", "needs-human")
APPROVABLE = frozenset({"trivial", "low-risk"})
FINDING_AREAS = frozenset({"security", "ci", "correctness", "scope", "other"})
MAX_SUMMARY_CHARS = 600
MAX_FINDINGS = 20
MAX_FINDING_CHARS = 400
MAX_INJECTIONS = 20
MAX_REASON_CHARS = 400
MAX_TITLE_CHARS = 200
MAX_LISTED_PATHS = 5
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+$")
# Track all fences, not just completed JSON blocks: otherwise a
# truncated or mislabelled final answer falls back to an earlier
# example block. Opening and closing delimiters must match. The
# quantifiers are possessive: giving characters back never makes a
# line match, and a backtracking run would read a long line once per
# character in it.
FENCE_RE = re.compile(r"^[ \t]*+(`{3,}+|~{3,}+)([^\r\n]*+)\r?$", re.MULTILINE)
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
NO_VERDICT = "no verdict"
HUMAN_REQUESTED = "agent judged the change needs a human review"
# GitHub decodes character references before it links mentions and
# references, so ``&#64;name`` still mentions and ``&#35;12`` still
# links. A zero-width space after the trigger is what stops both; the
# ampersand of any character reference is escaped first, so text that
# spells one out stays literal. Markdown also drops the backslash of an
# escaped punctuation mark before linking, so ``GH\-12`` and
# ``issues\/12`` link like their plain forms; the patterns allow one.
# Each pattern skips text it already defused, so a second pass changes
# nothing.
ZWSP = "\u200b"
ENTITY_RE = re.compile(r"&(?!amp;)(?=#?[A-Za-z0-9]+;)")
REFERENCE_RES = (
    (re.compile("@(?!" + ZWSP + ")"), "@" + ZWSP),
    (re.compile(r"#(?=[0-9])"), "#" + ZWSP),
    (re.compile(r"\b(gh)(?=\\?-[0-9])", re.IGNORECASE), r"\1" + ZWSP),
    (
        re.compile(r"/(issues|pull|pulls|discussions)(\\?/)(?=[0-9])", re.IGNORECASE),
        r"/\1\2" + ZWSP,
    ),
)
# Raw HTML and ``<...>`` autolinks need the character after ``<`` to
# be a tag name or a scheme; a zero-width space there leaves both as
# text (an ``<img>`` beacon, ``<details>``-hidden text).
HTML_RE = re.compile("<(?!" + ZWSP + ")")
# A line that opens with three backticks or tildes opens a code block
# that runs on until a matching fence, wherever that is. Sanitised
# text often stands as a paragraph of its own, so a zero-width space
# goes before such a run and the line stays text.
FENCE_OPENING_RE = re.compile("^(?=```|~~~)")
# Markdown link and image syntax, ``[text](url)`` and ``![alt](url)``,
# and link reference definitions, ``[label]: url``, which also feed
# ``[text][label]`` links and images. A zero-width space breaks each.
# Every ``]:`` breaks, not the top-level ones alone: a definition in a
# blockquote, a list item or over two lines defines a label for the
# whole document.
INLINE_LINK_RE = re.compile(r"\]\(")
DEFINITION_RE = re.compile(r"\]:")
# Bare ``www.`` and ``http(s)://`` text that GitHub autolinks. Neither
# needs a word boundary to link: GitHub links one after ``_``, a digit
# or punctuation, all of which ``\b`` can miss, so both are matched
# wherever they appear. Case folds over ASCII alone (``re.ASCII``):
# Unicode folding would let a lookalike host, ``g\u0131thub.com`` with
# a dotless i, pass as GitHub.
WWW_RE = re.compile(r"(www)\.", re.IGNORECASE | re.ASCII)
SCHEME_RE = re.compile(r"(https?):(?=//)", re.IGNORECASE | re.ASCII)
# A URL GitHub autolinks runs to ASCII whitespace or ``<``, a
# no-break space included, so every URL in such a run ends with it.
URL_RUN_RE = re.compile(r"[^\s<]+", re.ASCII)
# A path segment a browser resolves away, ``.`` or ``..``, spelt with
# dots or percent-encoded: ``https://github.com/<org>/../x`` leaves the
# organisation once resolved. A backslash counts as a separator, as
# browsers read it in an http(s) URL. GitHub trims trailing punctuation
# and an unmatched ``)`` from a link before the browser resolves it, so
# a run of one or two dots counts unless a letter or digit carries the
# segment on: ``(https://github.com/<org>/%2e%2e)`` links to ``..``.
DOT_SEGMENT_RE = re.compile(
    r"[/\\](?:\.|%2e){1,2}+(?![A-Za-z0-9])", re.IGNORECASE | re.ASCII
)


class Rejected(Exception):
    """The verdict is not usable."""


def defuse(text: str) -> str:
    """Stop untrusted text from mentioning anyone or referencing anything.

    Covers ``@`` mentions of users and teams, ``#12``, ``GH-12``,
    ``owner/repo#12`` and GitHub issue, pull request and discussion
    URLs, in Markdown links and autolinks too. The text still reads the
    same; the zero-width space is invisible.
    """
    defused = ENTITY_RE.sub("&amp;", text)
    for pattern, replacement in REFERENCE_RES:
        defused = pattern.sub(replacement, defused)
    return defused


def disarm_html(text: str) -> str:
    """Leave raw HTML and ``<...>`` autolinks in untrusted text as plain text."""
    return HTML_RE.sub("<" + ZWSP, text)


def contain_links(text: str, org: str) -> str:
    """Leave no live link in untrusted text but those into the organisation.

    Link and image syntax and link definitions stop working, so no
    agent-written text hides a destination behind a label or loads an
    image from anywhere. A bare URL stays a link when it points under
    ``https://github.com/<org>/``, over ``https`` alone, and no dot
    segment takes it back out once resolved, and becomes text
    otherwise; the URL still reads in full either way.
    """
    contained = INLINE_LINK_RE.sub("]" + ZWSP + "(", text)
    contained = DEFINITION_RE.sub("]" + ZWSP + ":", contained)
    contained = WWW_RE.sub(r"\1" + ZWSP + ".", contained)
    inside = (
        re.compile(
            rf"//github\.com/{re.escape(org)}(?:[/)\]>\"']|$)",
            re.IGNORECASE | re.ASCII,
        )
        if org
        else None
    )
    return URL_RUN_RE.sub(lambda run: contain_run(run.group(), inside), contained)


def contain_run(run: str, inside: re.Pattern[str] | None) -> str:
    """Disarm each URL in one run GitHub would link to its end, but those inside.

    A URL's link runs to the end of the run, so a dot segment anywhere
    after its scheme is in its path. Finding the last one first keeps
    the pass linear, however many URLs the run holds.
    """
    last_dot = max(
        (found.start() for found in DOT_SEGMENT_RE.finditer(run)), default=-1
    )

    def disarm(found: re.Match[str]) -> str:
        stays = (
            inside is not None
            and found.group(1).lower() == "https"
            and inside.match(run, found.end()) is not None
            and last_dot < found.end()
        )
        return found.group() if stays else found.group(1) + ":" + ZWSP

    return SCHEME_RE.sub(disarm, run)


def sanitise(text: object, limit: int) -> str:
    """Fold untrusted text into one bounded line safe for logs and Markdown.

    Control characters become spaces, workflow-command markers are
    broken, raw HTML stays text, a code fence cannot open, and
    mentions and references are defused so the agent can never
    mention, assign or cross-reference anything through a summary.
    """
    raw = text if isinstance(text, str) else ""
    if isinstance(text, int | float) and not isinstance(text, bool):
        raw = str(text)
    folded = CONTROL_RE.sub(" ", raw)
    escaped = folded.replace("::", ": :").replace("##[", "# #[")
    collapsed = FENCE_OPENING_RE.sub(
        ZWSP, " ".join(disarm_html(defuse(escaped)).split())
    )
    if len(collapsed) > limit:
        return collapsed[:limit].rstrip() + "..."
    return collapsed


def final_json_block(text: str) -> str:
    """Return the body of the final fenced block, which must be json."""
    opening: re.Match[str] | None = None
    block: str | None = None
    for fence in FENCE_RE.finditer(text):
        if opening is None:
            opening = fence
            block = None
        elif (
            fence[1][0] == opening[1][0]
            and len(fence[1]) >= len(opening[1])
            and not fence[2].strip()
        ):
            if opening[2].strip() == "json":
                block = text[opening.end() : fence.start()]
            opening = None
    if opening is not None:
        raise Rejected("the final verdict fence is unterminated")
    if block is None:
        raise Rejected("the final fenced block must be json")
    return block


def positive_int(value: object, name: str) -> int:
    """Return a positive integer field, refusing bools and floats."""
    if type(value) is not int or value <= 0:
        raise Rejected(f"verdict {name!r} must be a positive integer")
    return value


def text_field(value: object, name: str, limit: int) -> str:
    """Return a string field, sanitised and bounded."""
    if not isinstance(value, str):
        raise Rejected(f"verdict {name!r} must be a string")
    return sanitise(value, limit)


def findings_of(value: object) -> list[dict[str, str]]:
    """Validate the findings list, keeping at most MAX_FINDINGS entries."""
    if not isinstance(value, list):
        raise Rejected("verdict 'findings' must be a list")
    findings: list[dict[str, str]] = []
    for item in cast("list[Any]", value)[:MAX_FINDINGS]:
        if not isinstance(item, dict):
            raise Rejected("each finding must be an object")
        finding = cast("dict[str, Any]", item)
        area = finding.get("area")
        if not isinstance(area, str) or area not in FINDING_AREAS:
            raise Rejected("each finding needs a known 'area'")
        note = text_field(finding.get("note"), "findings[].note", MAX_FINDING_CHARS)
        findings.append({"area": area, "note": note})
    return findings


def injections_of(value: object) -> list[str]:
    """Validate the injection attempts list, bounded in count and length."""
    if not isinstance(value, list):
        raise Rejected("verdict 'injection_attempts' must be a list")
    return [
        text_field(item, "injection_attempts[]", MAX_FINDING_CHARS)
        for item in cast("list[Any]", value)[:MAX_INJECTIONS]
    ]


def extract_verdict(text: str) -> dict[str, Any]:
    """Pull the verdict object out of the agent's final message, validated."""
    block = final_json_block(text)
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
    if not isinstance(repository, str) or not REPO_RE.fullmatch(repository):
        raise Rejected("verdict 'repository' must be owner/name")
    head_sha = data.get("head_sha")
    if not isinstance(head_sha, str) or not SHA_RE.fullmatch(head_sha):
        raise Rejected("verdict 'head_sha' must be a 40-hex commit SHA")
    tier = data.get("tier")
    if tier not in TIERS:
        raise Rejected("verdict 'tier' must be trivial, low-risk or needs-human")
    return {
        "schema": SCHEMA,
        "repository": repository,
        "pull_request": positive_int(data.get("pull_request"), "pull_request"),
        "head_sha": head_sha,
        "tier": tier,
        "summary": text_field(data.get("summary"), "summary", MAX_SUMMARY_CHARS),
        "findings": findings_of(data.get("findings")),
        "injection_attempts": injections_of(data.get("injection_attempts")),
    }


def parse_approve_tiers(text: str) -> frozenset[str]:
    """Parse the approve-tiers input; "none" or empty means approve nothing."""
    tokens = [token.strip() for token in text.split(",") if token.strip()]
    if tokens == ["none"]:
        return frozenset()
    unknown = [token for token in tokens if token not in APPROVABLE]
    if unknown:
        raise ValueError(f"unknown approve tier {unknown[0]!r}")
    return frozenset(tokens)


def finite_number(value: object) -> float | None:
    """A finite, non-negative number from usage.json, or None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def usage_numbers(usage: dict[str, Any] | None) -> tuple[float | None, int | None]:
    """Premium request cost and agent seconds from the session usage file."""
    if usage is None:
        return None, None
    cost = finite_number(usage.get("totalPremiumRequestCost"))
    duration = finite_number(usage.get("totalApiDurationMs"))
    return cost, None if duration is None else int(duration // 1000)


def target_mismatch(entry: dict[str, Any], verdict: dict[str, Any]) -> str | None:
    """A reason when the verdict describes a different pull request or head."""
    expected = (
        str(entry.get("repository", "")).lower(),
        entry.get("number"),
        str(entry.get("head_sha", "")).lower(),
    )
    actual = (
        str(verdict["repository"]).lower(),
        verdict["pull_request"],
        str(verdict["head_sha"]).lower(),
    )
    if expected == actual:
        return None
    return (
        f"verdict targets {actual[0]}#{actual[1]} at {actual[2][:12]}, "
        f"not {expected[0]}#{expected[1]} at {expected[2][:12]}"
    )


def listed_paths(paths: list[str]) -> str:
    """Name up to MAX_LISTED_PATHS paths and count the rest."""
    shown = ", ".join(sanitise(path, 120) for path in paths[:MAX_LISTED_PATHS])
    rest = len(paths) - MAX_LISTED_PATHS
    return shown if rest <= 0 else f"{shown} and {rest} more"


def effective_tier(
    verdict: dict[str, Any], trivial: bool, non_trivial: list[str], vetoes: list[str]
) -> tuple[str, list[str]]:
    """Apply corroboration and vetoes to the agent's tier."""
    tier = str(verdict["tier"])
    reasons: list[str] = []
    if tier == "needs-human":
        notes = [str(finding["note"]) for finding in verdict["findings"]]
        return tier, notes or [HUMAN_REQUESTED]
    if tier == "trivial" and not trivial:
        reasons.append(f"trivial claim not corroborated: {listed_paths(non_trivial)}")
    reasons.extend(sanitise(veto, MAX_REASON_CHARS) for veto in vetoes)
    return ("needs-human" if reasons else tier), reasons


def check(
    entry: dict[str, Any],
    verdict: dict[str, Any] | None,
    *,
    approve_tiers: frozenset[str],
    failure: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce the check.json content for one pull request."""
    files = cast("list[dict[str, Any]]", entry.get("files") or [])
    trivial, non_trivial = classify_trivial(files)
    vetoes = find_vetoes(files)
    tier = "needs-human"
    reasons: list[str] = []
    if verdict is None:
        reasons = [sanitise(failure or NO_VERDICT, MAX_REASON_CHARS)]
    elif (mismatch := target_mismatch(entry, verdict)) is not None:
        reasons = [mismatch]
    else:
        tier, reasons = effective_tier(verdict, trivial, non_trivial, vetoes)
    approvable = tier in approve_tiers
    if tier in APPROVABLE and not approvable:
        reasons.append(f"tier {tier} not enabled for approval")
    premium_requests, agent_seconds = usage_numbers(usage)
    return {
        "schema": SCHEMA,
        "key": str(entry.get("key", "")),
        "repository": str(entry.get("repository", "")),
        "number": entry.get("number"),
        "head_sha": str(entry.get("head_sha", "")),
        "url": str(entry.get("url", "")),
        "title": sanitise(entry.get("title"), MAX_TITLE_CHARS),
        "agent_tier": None if verdict is None else verdict["tier"],
        "tier": tier,
        "approvable": approvable,
        "reasons": reasons,
        "summary": "" if verdict is None else verdict["summary"],
        "findings": [] if verdict is None else verdict["findings"],
        "injection_attempts": [] if verdict is None else verdict["injection_attempts"],
        "classifier": {"trivial": trivial, "non_trivial_paths": non_trivial},
        "vetoes": vetoes,
        "premium_requests": premium_requests,
        "agent_seconds": agent_seconds,
    }
