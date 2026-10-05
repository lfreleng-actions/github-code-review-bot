# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Write the select job's outputs: selection, matrix, exclusions and summary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

MAX_SUMMARY_TITLE_CHARS = 120


def summary_text(text: object, limit: int = MAX_SUMMARY_TITLE_CHARS) -> str:
    """Render public text for one line of the step summary.

    Control characters are folded, workflow command markers and
    Markdown structure are escaped, and ``@`` is encoded so a title
    can never mention anyone.
    """
    folded = "".join(ch if ch.isprintable() else " " for ch in str(text))
    collapsed = " ".join(folded.split())
    escaped = collapsed.replace("::", ": :").replace("##[", "# #[")
    for raw, safe in (
        ("@", "&#64;"),
        ("<", "&lt;"),
        (">", "&gt;"),
        ("|", "\\|"),
        ("[", "\\["),
        ("]", "\\]"),
    ):
        escaped = escaped.replace(raw, safe)
    if len(escaped) > limit:
        return escaped[:limit].rstrip() + "..."
    return escaped


def summary_markdown(selection: dict[str, Any]) -> str:
    """Render the selection for the step summary."""
    chosen = cast("list[dict[str, Any]]", selection["pull_requests"])
    counts = cast("dict[str, int]", selection["skip_counts"])
    tiers = ", ".join(cast("list[str]", selection["approve_tiers"])) or "none"
    lines = [
        "## Pull request selection",
        "",
        f"Dry run `{selection['dry_run']}`, model `{selection['model']}`, "
        f"approve tiers `{tiers}`. Candidates seen: {selection['candidates_seen']}; "
        f"selected: {len(chosen)}; skipped: {sum(counts.values())}.",
        "",
    ]
    for entry in chosen:
        lines.append(
            f"- [{entry['repo_name']}#{entry['number']}]({entry['url']}) "
            f"{summary_text(entry['title'])}"
        )
    if not chosen:
        lines.append("Nothing selected.")
    rows = [(reason, count) for reason, count in counts.items() if count]
    if rows:
        lines += ["", "| Skipped because | Count |", "| --- | --- |"]
        lines += [f"| {reason} | {count} |" for reason, count in rows]
    return "\n".join(lines) + "\n"


def write_outputs(directory: Path, selection: dict[str, Any]) -> None:
    """Write every file the workflow and the later jobs consume."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "selection.json").write_text(
        json.dumps(selection, indent=2) + "\n", encoding="utf-8"
    )
    include = [
        {
            "key": entry["key"],
            "repository": entry["repository"],
            "repo_name": entry["repo_name"],
            "number": entry["number"],
            "head_sha": entry["head_sha"],
            "head_repository": entry["head_repository"],
        }
        for entry in cast("list[dict[str, Any]]", selection["pull_requests"])
    ]
    (directory / "matrix.json").write_text(
        json.dumps({"include": include}) + "\n", encoding="utf-8"
    )
    (directory / "excluded-repos.txt").write_text(
        "".join(f"{name}\n" for name in cast("list[str]", selection["exclusions"])),
        encoding="utf-8",
    )
    (directory / "selection-summary.md").write_text(
        summary_markdown(selection), encoding="utf-8"
    )
