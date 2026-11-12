# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Read a head commit's checks and judge its CI state.

``pull_reads`` asks this module what a status check rollup says. It
also holds the stale commit status rule (docs/DESIGN.md section 5):
some services post a commit status and never complete it, and a
listed status still waiting past a bound must not hold the CI state
at ``pending``; CI then follows the other contexts of the rollup.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

import bot_github as github

ROLLUP_CI = {
    "SUCCESS": "success",
    "PENDING": "pending",
    "EXPECTED": "pending",
    "FAILURE": "failure",
    "ERROR": "failure",
}

# A commit status in one of these states has not reported yet.
WAITING_STATUSES = frozenset({"PENDING", "EXPECTED"})
# Completed check run conclusions and commit status states that pass.
PASSING_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})


@dataclass(frozen=True)
class StalePolicy:
    """Which commit statuses may go stale, after how long, judged at ``now``.

    A listed commit status still waiting ``minutes`` after GitHub
    created it is stale. ``0`` minutes disables the rule, and it never
    covers a check run.
    """

    contexts: frozenset[str]
    minutes: int
    now: datetime


def parse_timestamp(value: str, context: str) -> datetime:
    """Parse a GraphQL ``DateTime``, refusing one without a time zone."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise github.GitHubError(f"{context}: invalid timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise github.GitHubError(f"{context}: timestamp {value!r} has no zone")
    return parsed


def is_stale(node: dict[str, Any], policy: StalePolicy | None, context: str) -> bool:
    """Whether one rollup context is a listed commit status waiting too long."""
    if policy is None or policy.minutes <= 0:
        return False
    if node.get("__typename") != "StatusContext":
        return False
    if github.require_str(node, "context", context) not in policy.contexts:
        return False
    if github.require_str(node, "state", context) not in WAITING_STATUSES:
        return False
    created = parse_timestamp(github.require_str(node, "createdAt", context), context)
    return policy.now - created > timedelta(minutes=policy.minutes)


def ci_of_checks(checks: list[dict[str, Any]]) -> str:
    """The CI state a set of normalised checks reports on its own."""
    if not checks:
        return "none"
    waiting = False
    for check in checks:
        conclusion = check["conclusion"]
        if check["status"] != "COMPLETED" or conclusion in WAITING_STATUSES:
            waiting = True
        elif conclusion not in PASSING_CONCLUSIONS:
            return "failure"
    return "pending" if waiting else "success"


def lift_stale(
    ci: str,
    counted: Any,
    contexts: list[dict[str, Any]],
    checks: list[dict[str, Any]],
    policy: StalePolicy | None,
    context: str,
) -> tuple[str, list[str]]:
    """Apply the rule to a rollup state and its contexts.

    ``counted`` is the rollup's ``totalCount``, ``contexts`` the raw
    nodes the read holds and ``checks`` their normalised form, in the
    same order. Returns the CI state and the names of the stale
    statuses left out of it. A stale status only ever lifts a
    ``pending`` state, to what the other contexts report, and only
    when the read holds every context the rollup counts.
    """
    complete = type(counted) is int and counted == len(contexts)
    if ci != "pending" or not complete:
        return ci, []
    flags = [is_stale(node, policy, context) for node in contexts]
    if not any(flags):
        return ci, []
    names = [check["name"] for check, flag in zip(checks, flags, strict=True) if flag]
    others = [check for check, flag in zip(checks, flags, strict=True) if not flag]
    return ci_of_checks(others), names


def require_object(value: Any, context: str) -> dict[str, Any]:
    """Return a JSON object or fail the operation."""
    if not isinstance(value, dict):
        raise github.GitHubError(f"{context}: expected an object")
    return cast("dict[str, Any]", value)


def optional_str(value: Any, context: str) -> str | None:
    """Return a string or null field, refusing any other type."""
    if value is not None and not isinstance(value, str):
        raise github.GitHubError(f"{context}: expected a string or null")
    return value


def nodes_of(connection: Any, context: str) -> list[dict[str, Any]]:
    """Return the object nodes of a GraphQL connection.

    GraphQL returns a null node for an item the token cannot see; such
    an item carries nothing to act on, so it is left out rather than
    failing the read.
    """
    raw = require_object(connection, context).get("nodes")
    if not isinstance(raw, list):
        raise github.GitHubError(f"{context}: missing nodes")
    return [
        require_object(node, context)
        for node in cast("list[Any]", raw)
        if node is not None
    ]


def check_of(node: dict[str, Any], context: str) -> dict[str, Any]:
    """Normalise one rollup context: a check run, or a commit status.

    A commit status has no lifecycle of its own: once posted it is
    final, so it maps onto a completed check run.
    """
    kind = node.get("__typename")
    if kind == "CheckRun":
        return {
            "name": github.require_str(node, "name", context),
            "status": github.require_str(node, "status", context),
            "conclusion": optional_str(node.get("conclusion"), context),
        }
    if kind == "StatusContext":
        return {
            "name": github.require_str(node, "context", context),
            "status": "COMPLETED",
            "conclusion": github.require_str(node, "state", context),
        }
    raise github.GitHubError(f"{context}: unexpected check type {kind!r}")


def rollup_of(
    connection: Any, context: str, stale: StalePolicy | None = None
) -> tuple[str, list[dict[str, Any]], list[str], int]:
    """Read the head commit's rollup: CI state, checks, stale statuses, commits."""
    data = require_object(connection, context)
    total = github.require_int(data, "totalCount", context)
    nodes = nodes_of(data, context)
    if not nodes:
        raise github.GitHubError(f"{context}: no head commit")
    rollup = require_object(nodes[-1].get("commit"), context).get("statusCheckRollup")
    if rollup is None:
        return "none", [], [], total
    rollup_data = require_object(rollup, context)
    state = github.require_str(rollup_data, "state", context)
    ci = ROLLUP_CI.get(state)
    if ci is None:
        raise github.GitHubError(f"{context}: unexpected rollup state {state!r}")
    connection_data = require_object(rollup_data.get("contexts"), context)
    contexts = nodes_of(connection_data, context)
    checks = [check_of(node, context) for node in contexts]
    counted = connection_data.get("totalCount")
    ci, names = lift_stale(ci, counted, contexts, checks, stale, context)
    return ci, checks, names, total
