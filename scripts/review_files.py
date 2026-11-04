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
import review_policy as policy
from review_policy import Rejected

MAX_CHECK_BYTES = 1024 * 1024
MAX_REF_CHARS = 100


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


def require_comparison(check: dict[str, Any], context: str) -> None:
    """Refuse a check that does not name both ends of the reviewed diff."""
    github.require_sha(check, "head_sha", context)
    github.require_str(check, "base_ref", context)
    github.require_sha(check, "base_sha", context)


def comparison_reasons(check: dict[str, Any], state: dict[str, Any]) -> list[str]:
    """Reasons the live pull request no longer shows the comparison reviewed.

    A pull request's diff runs from its base to its head, and the
    evidence, the files the classifier corroborated and the patches the
    comments anchor on, was read for one pair. Retargeting changes the
    base without a push. A new commit on the same base ref leaves the
    diff alone unless it moves the merge base, which a force push or a
    merge of the head's own commits does; telling those apart takes
    another read, so any change of base SHA refuses: the next run
    reviews the pull request as it then stands.
    """
    reasons: list[str] = []
    if state.get("head_sha") != check["head_sha"]:
        reasons.append(f"head moved to {str(state.get('head_sha'))[:12]}")
    base_ref = policy.sanitise(check["base_ref"], MAX_REF_CHARS)
    if state.get("base_ref") != check["base_ref"]:
        live_ref = policy.sanitise(state.get("base_ref"), MAX_REF_CHARS)
        reasons.append(f"base retargeted from {base_ref} to {live_ref}")
    elif state.get("base_sha") != check["base_sha"]:
        reasons.append(f"base {base_ref} moved to {str(state.get('base_sha'))[:12]}")
    return reasons


def read_summary(session_dir: Path) -> tuple[str | None, str | None]:
    """Read the session's final message, or explain why it could not be read."""
    path = session_dir / "session-summary.md"
    if not path.exists():
        return None, "no session summary"
    try:
        content = evidence.read_regular(path, evidence.MAX_SUMMARY_BYTES)
    except (OSError, ValueError) as exc:
        return None, f"session summary unreadable: {github.safe_message(exc)}"
    return content.decode("utf-8", errors="replace"), None


def parse_verdict(
    text: str, extract: Callable[[str], dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None]:
    """Extract the verdict from a read summary, or explain why it is unusable."""
    try:
        return extract(text), None
    except Rejected as exc:
        return None, str(exc)


def read_verdict(
    session_dir: Path, extract: Callable[[str], dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None]:
    """Read the agent's verdict with ``extract``, or explain why none could be read."""
    text, failure = read_summary(session_dir)
    if text is None:
        return None, failure
    return parse_verdict(text, extract)


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
