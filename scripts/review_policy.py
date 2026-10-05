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
# example block. Opening and closing delimiters must match.
FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})([^\r\n]*)\r?$", re.MULTILINE)
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
NO_VERDICT = "no verdict"
HUMAN_REQUESTED = "agent judged the change needs a human review"


class Rejected(Exception):
    """The verdict is not usable."""


def sanitise(text: object, limit: int) -> str:
    """Fold untrusted text into one bounded line safe for logs and Markdown.

    Control characters become spaces, workflow-command markers are
    broken, and ``@`` is encoded so the agent can never mention or
    assign anyone through a summary.
    """
    raw = text if isinstance(text, str) else ""
    if isinstance(text, int | float) and not isinstance(text, bool):
        raw = str(text)
    folded = CONTROL_RE.sub(" ", raw)
    escaped = folded.replace("::", ": :").replace("##[", "# #[")
    collapsed = " ".join(escaped.replace("@", "&#64;").split())
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
