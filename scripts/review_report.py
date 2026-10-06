# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Summarise one run's results and carry the ledger forward.

The report job downloads every ``code-review-result-*`` artifact into
one directory, one subdirectory each, and this script folds them into
``report.json`` (machine-readable), ``report.md`` (for the step
summary) and the ledger the next run's select job will read.

Result files are written by the apply job and are trusted, but one
that is unreadable or malformed must not cost the run: it is left out
with a note, so a single broken matrix leg cannot hide every other
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

SCHEMA = 1
# The verdicts that make a head a skip next run; see ledger.record.
RECORDED_VERDICTS = frozenset({"approved", "would-approve", "needs-human"})
VERDICTS = ("approved", "would-approve", "needs-human", "failed")
MAX_RESULT_BYTES = 1024 * 1024
LINE_LIMIT = 120
# C0 and C1 controls plus the Unicode line separators: any of them
# could break a table row or smuggle a workflow command into a log.
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
WHITESPACE_RE = re.compile(r"\s+")
LINK_RE = re.compile(r"^https://github\.com/[A-Za-z0-9._/#?=&%-]+$")


def one_line(text: object, limit: int = LINE_LIMIT) -> str:
    """Render untrusted text as one bounded markdown table cell.

    ``::`` and ``##[`` are workflow command syntax should the file ever
    be echoed to a log; ``@`` becomes an entity so a summary can never
    mention anyone; ``|`` would end the cell.
    """
    folded = CONTROL_RE.sub(" ", "" if text is None else str(text))
    escaped = WHITESPACE_RE.sub(" ", folded).strip()
    # One pass leaves ":::" holding "::"; repeat until none remains.
    while "::" in escaped:
        escaped = escaped.replace("::", ": :")
    escaped = escaped.replace("##[", "# #[").replace("@", "&#64;")
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


def check_result(raw: Any) -> dict[str, Any]:
    """Validate one result file's shape, raising ValueError when it is off."""
    if not isinstance(raw, dict):
        raise ValueError("result is not an object")
    result = cast("dict[str, Any]", raw)
    repository = result.get("repository")
    number = result.get("number")
    head_sha = result.get("head_sha")
    key = result.get("key")
    if result.get("schema") != SCHEMA:
        raise ValueError(f"result schema is not {SCHEMA}")
    if result.get("verdict") not in VERDICTS:
        raise ValueError("result verdict is not one of the four values")
    if (
        not isinstance(key, str)
        or not key
        or not isinstance(repository, str)
        or not github.REPO_RE.fullmatch(repository)
        or type(number) is not int
        or number <= 0
        or not isinstance(head_sha, str)
        or not github.SHA_RE.fullmatch(head_sha)
    ):
        raise ValueError("result has an invalid key, repository, number or head")
    # The ledger needs the mode; checking here keeps a bad file out of
    # the report rather than failing the ledger write later.
    if not isinstance(result.get("dry_run"), bool):
        raise ValueError("result dry_run is not a boolean")
    return result


def result_files(directory: Path) -> list[Path]:
    """Locate ``result.json`` at the top level and one level down.

    download-artifact lays several matching artifacts out one per
    subdirectory, but writes a single match straight into the path
    with no subdirectory, so both shapes are read.
    """
    found = [directory / "result.json"]
    found.extend(child / "result.json" for child in sorted(directory.iterdir()))
    return [path for path in found if path.is_file()]


def load_results(directory: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Read every result file, ordered by key, naming the ones left out."""
    results: list[dict[str, Any]] = []
    unreadable: list[str] = []
    if not directory.is_dir():
        return results, unreadable
    for path in result_files(directory):
        try:
            content = read_regular(path, MAX_RESULT_BYTES)
            results.append(check_result(json.loads(content)))
        except (OSError, ValueError, RecursionError):
            unreadable.append(path.parent.name)
    results.sort(key=lambda result: str(result["key"]))
    return results, unreadable


def load_selection(path: Path) -> dict[str, Any]:
    """Read the trusted selection, which must be a JSON object."""
    parsed: Any = json.loads(read_regular(path, MAX_EVIDENCE_BYTES))
    if not isinstance(parsed, dict):
        raise ValueError("selection is not an object")
    return cast("dict[str, Any]", parsed)


def rows_of(value: Any) -> list[dict[str, Any]]:
    """The object entries of a list field, or nothing when it is not a list."""
    if not isinstance(value, list):
        return []
    return [
        cast("dict[str, Any]", item)
        for item in cast("list[Any]", value)
        if isinstance(item, dict)
    ]


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
    counts: dict[str, int] = dict.fromkeys(VERDICTS, 0)
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


def build_ledger(
    prior: dict[str, Any], results: list[dict[str, Any]], run_id: int
) -> dict[str, Any]:
    """The prior ledger plus this run's assessments."""
    for result in results:
        ledger.record(prior, result, run_id=run_id, recorded=RECORDED_VERDICTS)
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
        "dry_run": selection.get("dry_run") is True,
        "counts": count_verdicts(selection, results),
        "results": results,
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
    report: dict[str, Any], selection: dict[str, Any], unreadable: list[str]
) -> str:
    """The step-summary view of the report."""
    authors = {
        entry["key"]: entry.get("author")
        for entry in rows_of(selection.get("pull_requests"))
        if isinstance(entry.get("key"), str)
    }
    counts = cast("dict[str, int]", report["counts"])
    lines = [
        "# Code review run",
        "",
        f"Dry run: {'yes' if report['dry_run'] else 'no'}",
        "",
        ", ".join(
            f"{name.replace('_', ' ')}: {count}" for name, count in counts.items()
        ),
    ]
    if unreadable:
        names = ", ".join(one_line(name, 60) for name in unreadable)
        lines.extend(["", f"{len(unreadable)} result file(s) unreadable: {names}"])
    result_rows = [
        [
            link(pull_label(result), result.get("url")),
            one_line(result.get("title")),
            one_line(authors.get(result["key"]) or "?"),
            one_line(result.get("tier") or "?"),
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
    lines.extend(
        table(["PR", "Title", "Author", "Tier", "Verdict", "Detail"], result_rows)
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
        prior = ledger.load_ledger(args.ledger)
        results, unreadable = load_results(args.results)
        new_ledger = build_ledger(prior, results, run_id)
        report = build_report(selection, results, len(new_ledger["entries"]))
        write_json(args.output_json, report)
        write_json(args.output_ledger, new_ledger)
        args.output_md.parent.mkdir(parents=True, exist_ok=True)
        args.output_md.write_text(
            render_markdown(report, selection, unreadable), encoding="utf-8"
        )
    except (OSError, ValueError, RecursionError, ledger.LedgerError) as exc:
        parser.exit(1, f"report: {github.safe_message(exc)}\n")
    counts = cast("dict[str, int]", report["counts"])
    print(
        f"report: {len(results)} result(s), {counts['skipped']} skipped, "
        f"{len(unreadable)} unreadable -> {args.output_md}"
    )


if __name__ == "__main__":
    main()
