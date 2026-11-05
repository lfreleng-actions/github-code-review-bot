# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Summarise one run's results and carry the ledger forward.

The report job gathers every ``code-review-result-<namespace>-*``
artifact through ``result_fetch.py``, which writes each into its own
subdirectory with a ``provenance.json`` manifest, and this script
folds them into ``report.json`` (machine-readable), ``report.md`` (for
the step summary) and the ledger the next run's select job will read.

Results are read and bound to the selection by ``result_binding``;
only one whose artifact the gather verified enters the ledger, so a
forged result cannot suppress a future review.

A result that is unreadable or malformed must not cost the run: it is
left out with a note, so a single broken matrix leg cannot hide every other
verdict or stop the ledger from being carried forward.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any, cast

import bot_github as github
import ledger
from bot_evidence import MAX_EVIDENCE_BYTES, read_regular
from result_binding import load_results, rows_of
from review_policy import defuse

SCHEMA = 1
# Per mode: the verdicts a result may carry, and those that make a head
# a skip next run (see ledger.record). An antagonistic refusal counts
# only when settled (see recordable); a gate refusal and any failure
# are looked at again.
VERDICTS_BY_MODE = {
    "approve": ("approved", "would-approve", "needs-human", "failed"),
    "antagonistic": ("commented", "would-comment", "refused", "failed"),
}
RECORDED_BY_MODE = {
    "approve": frozenset({"approved", "would-approve", "needs-human"}),
    "antagonistic": frozenset({"commented", "would-comment", "refused"}),
}
LINE_LIMIT = 120
# C0 and C1 controls plus the Unicode line separators: any of them
# could break a table row or smuggle a workflow command into a log.
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
WHITESPACE_RE = re.compile(r"\s+")
LINK_RE = re.compile(r"^https://github\.com/[A-Za-z0-9._/#?=&%-]+$")


def one_line(text: object, limit: int = LINE_LIMIT) -> str:
    """Render untrusted text as one bounded markdown table cell.

    ``::`` and ``##[`` are workflow command syntax should the file ever
    be echoed to a log; mentions and references are defused so a
    summary can never mention or cross-reference anything; ``|`` would
    end the cell.
    """
    folded = CONTROL_RE.sub(" ", "" if text is None else str(text))
    escaped = WHITESPACE_RE.sub(" ", folded).strip()
    # One pass leaves ":::" holding "::"; repeat until none remains.
    while "::" in escaped:
        escaped = escaped.replace("::", ": :")
    escaped = defuse(escaped.replace("##[", "# #["))
    escaped = escaped.replace("|", "\\|")
    if len(escaped) > limit:
        return escaped[: limit - 3].rstrip() + "..."
    return escaped


def link(label: str, url: object) -> str:
    """A markdown link to a GitHub URL, or the bare label when there is none."""
    if isinstance(url, str) and LINK_RE.fullmatch(url):
        return f"[{one_line(label)}]({url})"
    return one_line(label)


def pull_label(data: dict[str, Any]) -> str:
    """The ``owner/repo#N`` name of a result or skipped row."""
    return f"{data.get('repository', '?')}#{data.get('number', '?')}"


def mode_of(selection: dict[str, Any]) -> str:
    """The mode the trusted selection recorded, refusing an unknown one."""
    mode = selection.get("mode")
    if not isinstance(mode, str) or mode not in VERDICTS_BY_MODE:
        raise ValueError("selection names no known mode")
    return mode


def load_selection(path: Path) -> dict[str, Any]:
    """Read the trusted selection, which must be a JSON object."""
    parsed: Any = json.loads(read_regular(path, MAX_EVIDENCE_BYTES))
    if not isinstance(parsed, dict):
        raise ValueError("selection is not an object")
    return cast("dict[str, Any]", parsed)


def skipped_rows(selection: dict[str, Any]) -> list[dict[str, Any]]:
    """The selection's skipped pull requests, grouped by reason."""

    def order(row: dict[str, Any]) -> tuple[str, str, int]:
        """Reason first so the table reads as groups."""
        number = row.get("number")
        return (
            str(row.get("reason") or ""),
            str(row.get("repository") or ""),
            number if type(number) is int else 0,
        )

    return sorted(rows_of(selection.get("skipped")), key=order)


def count_verdicts(
    selection: dict[str, Any], results: list[dict[str, Any]]
) -> dict[str, int]:
    """Verdict counts, plus the selection's skips and candidates seen."""
    counts: dict[str, int] = dict.fromkeys(VERDICTS_BY_MODE[mode_of(selection)], 0)
    for result in results:
        counts[str(result["verdict"])] += 1
    seen = selection.get("candidates_seen")
    counts["skipped"] = len(skipped_rows(selection))
    counts["candidates_seen"] = seen if type(seen) is int and seen >= 0 else 0
    return counts


def spend(results: list[dict[str, Any]]) -> tuple[float, int]:
    """Total premium requests and agent seconds, ignoring absent or odd values."""
    requests = 0.0
    seconds = 0
    for result in results:
        premium: Any = result.get("premium_requests")
        # bool is an int subclass; True must not count as one request.
        if (
            isinstance(premium, (int, float))
            and not isinstance(premium, bool)
            and math.isfinite(premium)
            and premium >= 0
        ):
            requests += float(premium)
        agent = result.get("agent_seconds")
        if type(agent) is int and agent >= 0:
            seconds += agent
    return round(requests, 2), seconds


def recordable(result: dict[str, Any]) -> bool:
    """Whether a result may enter the ledger at all.

    Only a verified result may: one whose provenance is unknown could be
    a forgery written to make the next run skip a head.

    A refusal is recorded only when the applier marked it settled: the
    session's own verdict gave nothing to post, and a second paid
    session reads the same head with the same prompt. A gate refusal
    (a draft, a moved head) is left out, so the head is looked at again.
    """
    if result.get("verified") is not True:
        return False
    return result.get("verdict") != "refused" or result.get("settled") is True


def build_ledger(
    prior: dict[str, Any], results: list[dict[str, Any]], run_id: int, mode: str
) -> dict[str, Any]:
    """The prior ledger plus this run's assessments."""
    for result in results:
        if recordable(result):
            ledger.record(prior, result, run_id=run_id, recorded=RECORDED_BY_MODE[mode])
    return prior


def build_report(
    selection: dict[str, Any],
    results: list[dict[str, Any]],
    ledger_entries: int,
) -> dict[str, Any]:
    """The machine-readable report."""
    premium_requests, agent_seconds = spend(results)
    return {
        "schema": SCHEMA,
        "mode": mode_of(selection),
        "dry_run": selection.get("dry_run") is True,
        "counts": count_verdicts(selection, results),
        "results": results,
        "unverified": sum(
            1 for result in results if result.get("verified") is not True
        ),
        "skipped": skipped_rows(selection),
        "premium_requests": premium_requests,
        "agent_seconds": agent_seconds,
        "ledger_entries": ledger_entries,
    }


def detail_of(result: dict[str, Any]) -> str:
    """The review link, else the first reason, else the summary."""
    review_url = result.get("review_url")
    if isinstance(review_url, str) and LINK_RE.fullmatch(review_url):
        return f"[review]({review_url})"
    reasons = result.get("reasons")
    if isinstance(reasons, list) and cast("list[Any]", reasons):
        return one_line(cast("list[Any]", reasons)[0])
    return one_line(result.get("summary"))


def assessment_of(result: dict[str, Any], mode: str) -> str:
    """The approve mode's tier, or the antagonistic mode's comment count."""
    if mode == "approve":
        return one_line(result.get("tier") or "?")
    count = result.get("comment_count")
    return str(count) if type(count) is int and count >= 0 else "?"


def table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Markdown table lines, or ``None.`` when there are no rows."""
    if not rows:
        return ["None."]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def render_markdown(
    report: dict[str, Any],
    selection: dict[str, Any],
    unreadable: list[str],
    notes: tuple[str, ...] | list[str] = (),
) -> str:
    """The step-summary view of the report."""
    authors = {
        entry["key"]: entry.get("author")
        for entry in rows_of(selection.get("pull_requests"))
        if isinstance(entry.get("key"), str)
    }
    counts = cast("dict[str, int]", report["counts"])
    mode = str(report["mode"])
    lines = [
        "# Code review run",
        "",
        f"Mode: {mode}; dry run: {'yes' if report['dry_run'] else 'no'}",
        "",
        ", ".join(
            f"{name.replace('_', ' ')}: {count}" for name, count in counts.items()
        ),
    ]
    if unreadable:
        names = ", ".join(one_line(name, 60) for name in unreadable)
        lines.extend(["", f"{len(unreadable)} result file(s) unreadable: {names}"])
    unverified = [
        pull_label(result)
        for result in cast("list[dict[str, Any]]", report["results"])
        if result.get("verified") is not True
    ]
    if unverified:
        names = ", ".join(one_line(name, 60) for name in unverified)
        lines.extend(
            [
                "",
                f"{len(unverified)} result(s) unverified, kept out of the ledger:"
                f" {names}",
            ]
        )
    if notes:
        lines.extend(["", "Left out while gathering:", ""])
        lines.extend(f"- {one_line(note, 200)}" for note in notes)
    result_rows = [
        [
            link(pull_label(result), result.get("url")),
            one_line(result.get("title")),
            one_line(authors.get(result["key"]) or "?"),
            assessment_of(result, mode),
            one_line(result["verdict"]),
            detail_of(result),
        ]
        for result in cast("list[dict[str, Any]]", report["results"])
    ]
    skipped = [
        [
            link(pull_label(row), row.get("url")),
            one_line(row.get("title")),
            one_line(row.get("reason")),
        ]
        for row in cast("list[dict[str, Any]]", report["skipped"])
    ]
    lines.extend(["", "## Results", ""])
    assessment = "Tier" if mode == "approve" else "Comments"
    lines.extend(
        table(["PR", "Title", "Author", assessment, "Verdict", "Detail"], result_rows)
    )
    lines.extend(["", "## Skipped", ""])
    lines.extend(table(["PR", "Title", "Reason"], skipped))
    lines.extend(
        [
            "",
            "## Spend",
            "",
            f"Premium requests: {report['premium_requests']}; "
            f"agent seconds: {report['agent_seconds']}.",
            "",
            f"Ledger entries: {report['ledger_entries']}.",
            "",
        ]
    )
    return "\n".join(lines)


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Write a JSON document with a trailing newline, creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    """Build the report and the next ledger from this run's results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-ledger", type=Path, required=True)
    args = parser.parse_args(argv)
    run_id = cast("int", args.run_id)
    if run_id <= 0:
        parser.error("--run-id must be a positive integer")
    try:
        selection = load_selection(args.selection)
        mode = mode_of(selection)
        prior = ledger.load_ledger(args.ledger)
        results, unreadable, notes = load_results(
            args.results, selection, VERDICTS_BY_MODE[mode]
        )
        new_ledger = build_ledger(prior, results, run_id, mode)
        report = build_report(selection, results, len(new_ledger["entries"]))
        write_json(args.output_json, report)
        write_json(args.output_ledger, new_ledger)
        args.output_md.parent.mkdir(parents=True, exist_ok=True)
        args.output_md.write_text(
            render_markdown(report, selection, unreadable, notes), encoding="utf-8"
        )
    except (OSError, ValueError, RecursionError, ledger.LedgerError) as exc:
        parser.exit(1, f"report: {github.safe_message(exc)}\n")
    counts = cast("dict[str, int]", report["counts"])
    print(
        f"report: {len(results)} result(s), {report['unverified']} unverified, "
        f"{counts['skipped']} skipped, {len(unreadable)} unreadable"
        f" -> {args.output_md}"
    )


if __name__ == "__main__":
    main()
