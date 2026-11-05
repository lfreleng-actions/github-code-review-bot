# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Read the gathered results and bind each to the trusted selection.

``result_fetch.py`` writes each result artifact into its own
directory with a ``provenance.json`` manifest. A result file is a
claim, not a fact: an artifact name is open to any job in the run,
the untrusted review job included. A result counts only for a key the
digest-verified selection holds, at the repository, number and head
the selection recorded, for the attempt its artifact name gives; its
``dry_run`` comes from the selection and ``verified`` from the
manifest. One result per key stands: the latest verified attempt,
else the latest unverified one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import bot_github as github
from bot_evidence import MAX_EVIDENCE_BYTES, read_regular

SCHEMA = 1
MAX_RESULT_BYTES = 1024 * 1024
# result_fetch.py writes provenance.json and one directory per artifact,
# named after it.
MANIFEST = "provenance.json"
DIRECTORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def check_result(raw: Any, verdicts: tuple[str, ...]) -> dict[str, Any]:
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
    if result.get("verdict") not in verdicts:
        raise ValueError("result verdict is not one this mode produces")
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


def selected_entries(selection: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The selection's pull request entries by key."""
    return {
        entry["key"]: entry
        for entry in rows_of(selection.get("pull_requests"))
        if isinstance(entry.get("key"), str)
    }


def bind(
    result: dict[str, Any],
    artifact: dict[str, Any],
    selection: dict[str, Any],
) -> dict[str, Any]:
    """Tie a result to its artifact and the trusted selection, or raise.

    The key must be one the selection holds, at the repository, number
    and head it recorded, and the attempt the artifact name gives;
    ``dry_run`` comes from the selection and ``verified`` from the
    manifest, never from the file.
    """
    key = artifact.get("key")
    entry = selected_entries(selection).get(str(key))
    if entry is None or result["key"] != key:
        raise ValueError("result names a key outside the selection")
    for field in ("repository", "number", "head_sha"):
        if result[field] != entry.get(field):
            raise ValueError(f"result {field} differs from the selection")
    attempt = result.get("run_attempt")
    if type(attempt) is not int or attempt != artifact.get("attempt"):
        raise ValueError("result attempt differs from its artifact name")
    result["dry_run"] = selection.get("dry_run") is True
    result["verified"] = artifact.get("verified") is True
    return result


def manifest_entries(
    directory: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    """The gathered artifacts and the gather's notes, from ``provenance.json``.

    No manifest means nothing was gathered. One that cannot be read
    leaves every result out, with a note, rather than trusting any.
    """
    path = directory / MANIFEST
    if not path.is_file():
        return [], []
    try:
        parsed: Any = json.loads(read_regular(path, MAX_EVIDENCE_BYTES))
    except (OSError, ValueError, RecursionError):
        return [], ["provenance manifest unreadable"]
    if not isinstance(parsed, dict):
        return [], ["provenance manifest unreadable"]
    manifest = cast("dict[str, Any]", parsed)
    notes = [str(note) for note in cast("list[Any]", manifest.get("notes") or [])]
    return rows_of(manifest.get("artifacts")), notes


def latest(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One result per key: the latest verified attempt, else the latest one."""
    chosen: dict[str, dict[str, Any]] = {}
    for result in results:
        key = str(result["key"])
        held = chosen.get(key)
        rank = (result["verified"], result["run_attempt"])
        if held is None or rank > (held["verified"], held["run_attempt"]):
            chosen[key] = result
    return sorted(chosen.values(), key=lambda result: str(result["key"]))


def load_results(
    directory: Path, selection: dict[str, Any], verdicts: tuple[str, ...]
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Read every gathered result, ordered by key.

    Also returns the directories whose result was left out and the
    gather's notes on artifacts it never fetched.
    """
    results: list[dict[str, Any]] = []
    unreadable: list[str] = []
    if not directory.is_dir():
        return results, unreadable, []
    artifacts, notes = manifest_entries(directory)
    for artifact in artifacts:
        name = str(artifact.get("directory") or "?")
        try:
            if not DIRECTORY_RE.fullmatch(name):
                raise ValueError("artifact directory is not a plain name")
            content = read_regular(directory / name / "result.json", MAX_RESULT_BYTES)
            result = check_result(json.loads(content), verdicts)
            results.append(bind(result, artifact, selection))
        except (OSError, ValueError, RecursionError):
            unreadable.append(name)
    return latest(results), unreadable, notes


def rows_of(value: Any) -> list[dict[str, Any]]:
    """The object entries of a list field, or nothing when it is not a list."""
    if not isinstance(value, list):
        return []
    return [
        cast("dict[str, Any]", item)
        for item in cast("list[Any]", value)
        if isinstance(item, dict)
    ]
