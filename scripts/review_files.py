# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Reads and writes of the apply job's files, shared by both review modes.

The approve mode's ``apply_review`` and the antagonistic mode's
``comment_review`` each turn a session into a check and a check into
one write. The files they read and write have one shape, kept here so
that neither applier has to import the other: the module that
comments never loads the code that approves.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import bot_evidence as evidence
import bot_github as github
from review_policy import Rejected

MAX_CHECK_BYTES = 1024 * 1024


def note(source: str, message: str) -> None:
    """Log to stderr so stdout stays a clean list of workflow outputs."""
    print(f"{source}: {message}", file=sys.stderr)


def load_json(path: Path, limit: int, context: str) -> dict[str, Any]:
    """Read a bounded regular JSON file that must hold an object."""
    try:
        parsed: Any = json.loads(evidence.read_regular(path, limit))
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"{context}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{context}: expected a JSON object")
    return cast("dict[str, Any]", parsed)


def find_entry(selection: dict[str, Any], key: str) -> dict[str, Any]:
    """Locate the per-PR entry by key or fail the step."""
    for item in cast("list[Any]", selection.get("pull_requests") or []):
        if isinstance(item, dict):
            candidate = cast("dict[str, Any]", item)
            if candidate.get("key") == key:
                return candidate
    raise ValueError(f"key {key!r} is not in the selection")


def require_mode(data: dict[str, Any], mode: str, context: str) -> None:
    """Refuse a selection or check that another mode's run produced."""
    if data.get("mode") != mode:
        raise ValueError(f"{context} is not from a {mode} run")


def read_verdict(
    session_dir: Path, extract: Callable[[str], dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None]:
    """Read the agent's verdict with ``extract``, or explain why none could be read."""
    path = session_dir / "session-summary.md"
    if not path.exists():
        return None, "no session summary"
    try:
        content = evidence.read_regular(path, evidence.MAX_SUMMARY_BYTES)
    except (OSError, ValueError) as exc:
        return None, f"session summary unreadable: {github.safe_message(exc)}"
    try:
        return extract(content.decode("utf-8", errors="replace")), None
    except Rejected as exc:
        return None, str(exc)


def read_usage(session_dir: Path, source: str) -> dict[str, Any] | None:
    """Read usage.json when present; a bad file costs the spend figures only."""
    path = session_dir / "usage.json"
    if not path.exists():
        return None
    try:
        return load_json(path, evidence.MAX_USAGE_BYTES, "usage.json")
    except (OSError, ValueError) as exc:
        note(source, f"ignoring usage.json: {github.safe_message(exc)}")
        return None


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Write a JSON artefact, creating its directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
