# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Parse the select job's command-line inputs.

Kept apart from the selection flow so the accepted spellings of a
repository list, a pull request list and the approval tiers read as
one contract, and so the flow in ``select_pulls`` stays short enough
to follow.
"""

from __future__ import annotations

import re
from pathlib import Path

from pull_reads import SelectionError

REPO_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
# The five accepted spellings of one pull request. A URL must carry an
# owner, so the owner and the URL prefix share one optional group.
PULL_RE = re.compile(
    r"(?:(?:https://github\.com/)?(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)/)?"
    r"(?P<repo>[A-Za-z0-9_.-]+)(?:/pull/|#)(?P<number>[0-9]+)/?"
)
APPROVABLE_TIERS = frozenset({"trivial", "low-risk"})


def parse_repositories(text: str) -> list[str]:
    """Split a repository list on commas and whitespace, in given order."""
    names: list[str] = []
    for token in re.split(r"[,\s]+", text.strip()):
        if not token:
            continue
        if not REPO_NAME_RE.fullmatch(token) or token in (".", ".."):
            raise SelectionError(f"invalid repository name {token!r}")
        lowered = token.lower()
        if lowered not in names:
            names.append(lowered)
    return names


def parse_pull_requests(text: str, org: str) -> list[tuple[str, int]]:
    """Resolve named pull requests to ``(repo_name, number)`` pairs, in order.

    Accepts ``repo#N``, ``owner/repo#N``, ``repo/pull/N``,
    ``owner/repo/pull/N`` and the GitHub URL. An owner other than the
    organisation is refused: the run holds a token for one
    organisation, and a typo must not quietly review a stranger's
    pull request.
    """
    targets: list[tuple[str, int]] = []
    for token in re.split(r"[,\s]+", text.strip()):
        if not token:
            continue
        found = PULL_RE.fullmatch(token)
        if found is None:
            raise SelectionError(f"invalid pull request reference {token!r}")
        owner = found.group("owner")
        if owner is not None and owner.lower() != org.lower():
            raise SelectionError(f"pull request {token!r} is not in {org}")
        repo = found.group("repo").lower()
        number = int(found.group("number"))
        if repo in (".", "..") or number <= 0:
            raise SelectionError(f"invalid pull request reference {token!r}")
        if (repo, number) not in targets:
            targets.append((repo, number))
    return targets


def parse_max_pull_requests(text: str) -> int:
    """Accept a non-negative integer; zero lifts the cap."""
    if not re.fullmatch(r"\d+", text.strip()):
        raise SelectionError(
            f"max_pull_requests must be a non-negative integer, got {text!r}"
        )
    return int(text)


def parse_approve_tiers(text: str) -> list[str]:
    """Resolve the tiers the run may approve, sorted.

    ``none`` and an empty value both mean the run approves nothing;
    ``none`` alongside a tier is a contradiction and is refused.
    """
    tokens = [token for token in re.split(r"[,\s]+", text.strip()) if token]
    if tokens == ["none"]:
        return []
    tiers: set[str] = set()
    for token in tokens:
        if token not in APPROVABLE_TIERS:
            raise SelectionError(f"unknown approve tier {token!r}")
        tiers.add(token)
    return sorted(tiers)


def load_exclusions(file: Path | None, override: str) -> list[str]:
    """Resolve the exclusion list: an explicit override beats the bundled file."""
    if override.strip():
        return parse_repositories(override)
    if file is None:
        return []
    if not file.is_file():
        raise SelectionError(f"exclusion file {file} is not a regular file")
    names: list[str] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            names.extend(parse_repositories(stripped))
    return names
