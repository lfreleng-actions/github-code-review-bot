# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Turn a review session into a check, then a check into an approval.

``check`` runs offline against the session artefact and writes the
typed ``check.json`` the workflow gates its write token on.
``apply`` re-reads the pull request live, applies the gates, and is
the only place that submits a review. A policy refusal is a typed
``needs-human`` result and exit 0; a GitHub failure is exit 1.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import bot_evidence as evidence
import bot_github as github
import pull_reads
import review_policy as policy

SCHEMA = 1
MAX_CHECK_BYTES = 1024 * 1024
DEFAULT_BOT = "the code review bot"
OPENINGS = {
    "trivial": (
        "🤖 Auto-approved by agent: trivial change (dependency/docs/formatting only)."
    ),
    "low-risk": (
        "🤖 Auto-approved by agent: reviewed workflow/code change for "
        "security and CI/CD impact, found low risk."
    ),
}


def note(message: str) -> None:
    """Log to stderr so stdout stays a clean list of workflow outputs."""
    print(f"apply_review: {message}", file=sys.stderr)


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


def read_verdict(session_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read the agent's verdict, or explain why none could be read."""
    path = session_dir / "session-summary.md"
    if not path.exists():
        return None, "no session summary"
    try:
        content = evidence.read_regular(path, evidence.MAX_SUMMARY_BYTES)
    except (OSError, ValueError) as exc:
        return None, f"session summary unreadable: {github.safe_message(exc)}"
    try:
        return policy.extract_verdict(content.decode("utf-8", errors="replace")), None
    except policy.Rejected as exc:
        return None, str(exc)


def read_usage(session_dir: Path) -> dict[str, Any] | None:
    """Read usage.json when present; a bad file costs the spend figures only."""
    path = session_dir / "usage.json"
    if not path.exists():
        return None
    try:
        return load_json(path, evidence.MAX_USAGE_BYTES, "usage.json")
    except (OSError, ValueError) as exc:
        note(f"ignoring usage.json: {github.safe_message(exc)}")
        return None


def summary_markdown(check: dict[str, Any]) -> str:
    """A short Markdown summary of the check for the job summary."""
    reasons = cast("list[str]", check.get("reasons") or [])
    agent_tier = check.get("agent_tier") or "none"
    lines = [
        f"## Code review check: [{check['repository']}#{check['number']}]"
        f"({check['url']})",
        "",
        f"- Title: {check['title']}",
        f"- Agent tier: `{agent_tier}` -> effective tier: `{check['tier']}`",
        f"- Approvable: {'yes' if check['approvable'] else 'no'}",
    ]
    if reasons:
        lines.append(f"- Reasons: {'; '.join(reasons)}")
    return "\n".join(lines) + "\n"


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Write a JSON artefact, creating its directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def run_check(args: argparse.Namespace) -> None:
    """Offline check: verdict, corroboration, vetoes, enabled tiers."""
    approve_tiers = policy.parse_approve_tiers(args.approve_tiers)
    selection = load_json(args.selection, evidence.MAX_EVIDENCE_BYTES, "selection")
    entry = find_entry(selection, args.key)
    verdict: dict[str, Any] | None = None
    failure: str | None = str(args.failure) if args.failure else None
    if failure is None:
        verdict, failure = read_verdict(args.session_dir)
    if failure is not None:
        note(f"no usable verdict: {github.safe_message(ValueError(failure))}")
    check = policy.check(
        entry,
        verdict,
        approve_tiers=approve_tiers,
        failure=failure,
        usage=read_usage(args.session_dir),
    )
    write_json(args.output, check)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(summary_markdown(check), encoding="utf-8")
    print(f"approvable={'true' if check['approvable'] else 'false'}")
    print(f"tier={check['tier']}")


def approval_body(
    check: dict[str, Any], selection: dict[str, Any], run_url: str
) -> str:
    """Compose the review body; the agent never writes to GitHub itself."""
    bot_login = selection.get("bot_login")
    reviewer = bot_login if isinstance(bot_login, str) and bot_login else DEFAULT_BOT
    model = policy.sanitise(selection.get("model"), 100) or "unknown"
    summary = policy.sanitise(check.get("summary"), policy.MAX_SUMMARY_CHARS)
    return (
        f"{OPENINGS[str(check['tier'])]}\n\n{summary}\n\n---\n"
        f"Automated review by `{reviewer}` using model `{model}`; "
        f"see the [run]({run_url}). This is not a human review."
    )


def gate_reasons(check: dict[str, Any], state: dict[str, Any]) -> list[str]:
    """Reasons the live pull request may no longer be approved."""
    reasons: list[str] = []
    if state.get("state") != "OPEN":
        reasons.append(f"pull request is {state.get('state')}")
    if state.get("is_draft"):
        reasons.append("pull request is a draft")
    if state.get("head_sha") != check["head_sha"]:
        reasons.append(f"head moved to {str(state.get('head_sha'))[:12]}")
    if state.get("ci") != "success":
        reasons.append(f"CI is {state.get('ci')}")
    if state.get("changes_requested"):
        reasons.append("a reviewer requested changes")
    requested = cast("list[str]", state.get("review_requests") or [])
    if any(pull_reads.is_copilot(login) for login in requested):
        reasons.append("Copilot review is pending")
    threads = state.get("unresolved_copilot_threads") or 0
    if threads:
        reasons.append(f"{threads} unresolved Copilot review thread(s)")
    # Any standing approval of this head, the bot's own included,
    # makes a second one noise: the selection skipped such pull
    # requests, so one arriving here was approved during the run.
    approved_by = cast("list[str]", state.get("approved_by") or [])
    if approved_by:
        reasons.append(f"already approved by {', '.join(sorted(approved_by))}")
    return reasons


def validated_check(path: Path) -> dict[str, Any]:
    """Load check.json and confirm the fields apply acts on."""
    check = load_json(path, MAX_CHECK_BYTES, "check")
    if check.get("schema") != SCHEMA:
        raise ValueError("check.json has an unknown schema")
    repository = github.require_str(check, "repository", "check.json")
    if not github.REPO_RE.fullmatch(repository):
        raise ValueError("check.json repository is malformed")
    github.require_int(check, "number", "check.json")
    github.require_sha(check, "head_sha", "check.json")
    if check.get("approvable") is True and check.get("tier") not in OPENINGS:
        raise ValueError("check.json is approvable with a non-approvable tier")
    return check


def run_apply(args: argparse.Namespace) -> None:
    """Gate the live pull request, then approve or record why not."""
    check = validated_check(args.check)
    selection = load_json(args.selection, evidence.MAX_EVIDENCE_BYTES, "selection")
    repository, number = str(check["repository"]), int(check["number"])
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "key": check.get("key"),
        "repository": repository,
        "number": number,
        "url": check.get("url"),
        "title": check.get("title"),
        "head_sha": check["head_sha"],
        "verdict": "needs-human",
        "tier": check.get("tier"),
        "reasons": list(cast("list[str]", check.get("reasons") or [])),
        "summary": check.get("summary"),
        "findings": check.get("findings") or [],
        "injection_attempts": check.get("injection_attempts") or [],
        "review_url": None,
        "dry_run": bool(args.dry_run),
        "run_attempt": args.run_attempt,
        "premium_requests": check.get("premium_requests"),
        "agent_seconds": check.get("agent_seconds"),
    }
    reasons = cast("list[str]", result["reasons"])
    if check.get("approvable") is True:
        state = pull_reads.pull_state(repository, number)
        reasons.extend(gate_reasons(check, state))
        if reasons:
            result["verdict"] = "needs-human"
        elif args.dry_run:
            result["verdict"] = "would-approve"
        else:
            reply = github.api_write(
                "POST",
                f"repos/{repository}/pulls/{number}/reviews",
                {
                    "commit_id": check["head_sha"],
                    "event": "APPROVE",
                    "body": approval_body(check, selection, args.run_url),
                },
            )
            url = reply.get("html_url")
            result["review_url"] = url if isinstance(url, str) else None
            result["verdict"] = "approved"
    elif not reasons:
        reasons.append("check judged the change not approvable")
    write_json(args.output, result)
    review_url = result["review_url"] or "-"
    print(f"apply: {result['verdict']} {repository}#{number} review={review_url}")


def build_parser() -> argparse.ArgumentParser:
    """Describe the check and apply commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="offline verdict check and policy")
    check.add_argument("--selection", type=Path, required=True)
    check.add_argument("--key", required=True)
    check.add_argument("--session-dir", type=Path, required=True)
    check.add_argument("--approve-tiers", required=True)
    check.add_argument("--failure", default=None)
    check.add_argument("--output", type=Path, required=True)
    check.add_argument("--summary", type=Path, required=True)
    check.set_defaults(handler=run_check)

    apply = commands.add_parser("apply", help="gate live state and approve")
    apply.add_argument("--check", type=Path, required=True)
    apply.add_argument("--selection", type=Path, required=True)
    apply.add_argument("--output", type=Path, required=True)
    apply.add_argument("--run-url", required=True)
    apply.add_argument("--run-attempt", type=int, required=True)
    apply.add_argument("--dry-run", action="store_true")
    apply.set_defaults(handler=run_apply)
    return parser


def main(argv: list[str] | None = None) -> None:
    """Dispatch a command, keeping operational failures on the error path."""
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = cast("Callable[[argparse.Namespace], None]", args.handler)
    try:
        handler(args)
    except (OSError, ValueError, github.GitHubError) as exc:
        parser.exit(1, f"apply_review: {github.safe_message(exc)}\n")


if __name__ == "__main__":
    main()
