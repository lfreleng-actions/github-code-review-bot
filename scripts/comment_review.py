# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Turn an antagonistic session into a check, then a check into a COMMENT review.

The antagonistic mode's applier. ``check`` runs offline against the
session artefact and writes the typed ``check.json`` the workflow
gates its write token on. ``comment`` re-reads the pull request live,
applies the gates, and is the only place the mode writes: one review
whose event is always ``COMMENT``, carrying the agent's findings
inline. It never approves and never requests changes, so the human
review the organisation requires stays required. A gate refusal is a
typed ``refused`` result and exit 0; a GitHub failure is exit 1.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import bot_evidence as evidence
import bot_github as github
import pull_changes
import pull_reads
import review_comments as comments
import review_files as files
import review_policy as policy
from selection_rules import author_skip, reviewed_by

SCHEMA = 1
# The only review event this mode sends. GitHub also accepts APPROVE
# and REQUEST_CHANGES; neither appears in this module.
EVENT = "COMMENT"
SOURCE = "comment_review"
UNPROCESSABLE = 422
DEFAULT_BOT = "the antagonistic review bot"
OPENING = "🤖 Antagonistic review by agent"
DISCLAIMER = (
    "This is not a human review and never an approval: a maintainer must "
    "still review and approve this pull request."
)


def findings_of(check: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """One of the check's finding lists: ``comments`` inline, or ``unplaced``."""
    return cast("list[dict[str, Any]]", check.get(key) or [])


def finding_count(check: dict[str, Any]) -> int:
    """Every finding the review carries, inline or in its body."""
    return len(findings_of(check, "comments")) + len(findings_of(check, "unplaced"))


def summary_markdown(check: dict[str, Any]) -> str:
    """A short Markdown summary of the check for the job summary.

    Names every finding left out, and why, since the posted review
    sends readers here for them.
    """
    dropped = cast("list[str]", check["dropped"])
    lines = [
        f"## Antagonistic review check: [{check['repository']}#{check['number']}]"
        f"({check['url']})",
        "",
        f"- Title: {check['title']}",
        f"- Commentable: {'yes' if check['commentable'] else 'no'}",
        f"- Inline comments: {len(findings_of(check, 'comments'))}",
        f"- In the body, no line to sit on: {len(findings_of(check, 'unplaced'))}",
        f"- Malformed, left out: {len(dropped)}",
        f"- Beyond the cap of {comments.MAX_COMMENTS}, not read: {check['capped']}",
    ]
    lines.extend(
        f"  - `{policy.sanitise(c['path'], 120)}` line {c['line']}: "
        f"{policy.sanitise(c.get('reason'), policy.MAX_REASON_CHARS)}"
        for c in findings_of(check, "unplaced")
    )
    lines.extend(f"  - left out: {reason}" for reason in dropped)
    reasons = cast("list[str]", check.get("reasons") or [])
    if reasons:
        lines.append(f"- Reasons: {'; '.join(reasons)}")
    return "\n".join(lines) + "\n"


def run_check(args: argparse.Namespace) -> None:
    """Offline check: verdict, target and the anchor of every comment."""
    selection = files.load_json(
        args.selection, evidence.MAX_EVIDENCE_BYTES, "selection"
    )
    files.require_mode(selection, comments.MODE, "selection")
    entry = files.find_entry(selection, args.key)
    verdict: dict[str, Any] | None = None
    failure: str | None = str(args.failure) if args.failure else None
    if failure is None:
        verdict, failure = files.read_verdict(
            args.session_dir, comments.extract_verdict
        )
    if failure is not None:
        files.note(
            SOURCE, f"no usable verdict: {github.safe_message(ValueError(failure))}"
        )
    check = comments.check(
        entry,
        verdict,
        failure=failure,
        usage=files.read_usage(args.session_dir, SOURCE),
    )
    files.write_json(args.output, check)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(summary_markdown(check), encoding="utf-8")
    print(f"commentable={'true' if check['commentable'] else 'false'}")
    print(f"comments={finding_count(check)}")


def validated_check(path: Path) -> dict[str, Any]:
    """Load check.json and confirm the fields the comment command acts on."""
    check = files.load_json(path, files.MAX_CHECK_BYTES, "check")
    if check.get("schema") != SCHEMA:
        raise ValueError("check.json has an unknown schema")
    files.require_mode(check, comments.MODE, "check.json")
    repository = github.require_str(check, "repository", "check.json")
    if not github.REPO_RE.fullmatch(repository):
        raise ValueError("check.json repository is malformed")
    github.require_int(check, "number", "check.json")
    github.require_sha(check, "head_sha", "check.json")
    capped = check.get("capped")
    if type(capped) is not int or capped < 0:
        raise ValueError("check.json capped is not a count")
    for key in ("comments", "unplaced"):
        raw = check.get(key)
        if not isinstance(raw, list):
            raise ValueError(f"check.json {key} is not a list")
        for item in cast("list[Any]", raw):
            if not isinstance(item, dict):
                raise ValueError("check.json carries a malformed comment")
            comment = cast("dict[str, Any]", item)
            github.require_str(comment, "path", "check.json comment")
            github.require_int(comment, "line", "check.json comment")
            start = comment.get("start_line")
            if start is not None and (
                type(start) is not int or not 0 < start < int(comment["line"])
            ):
                raise ValueError("check.json comment has an invalid start_line")
            github.require_str(comment, "body", "check.json comment")
            if comment.get("severity") not in comments.SEVERITIES:
                raise ValueError("check.json comment has an unknown severity")
    return check


def reviewer_of(selection: dict[str, Any]) -> str:
    """The App login from the selection, or a generic name in a secretless run."""
    bot_login = selection.get("bot_login")
    return bot_login if isinstance(bot_login, str) and bot_login else DEFAULT_BOT


def inline(comment: dict[str, Any], org: str) -> dict[str, Any]:
    """One review comment as the reviews API takes it, on the new file's side.

    The severity sits on a line of its own, so a body that opens with a
    code fence still opens it at column 0, where the sanitiser assumed
    it would be.
    """
    payload: dict[str, Any] = {
        "path": comment["path"],
        "line": comment["line"],
        "side": "RIGHT",
        "body": f"**{comment['severity']}**:\n\n{contained(comment['body'], org)}",
    }
    start = comment.get("start_line")
    if type(start) is int:
        payload["start_line"] = start
        payload["start_side"] = "RIGHT"
    return payload


def contained(text: str, org: str) -> str:
    """Untrusted Markdown with no live link outside the organisation, code aside."""
    return comments.outside_code(text, lambda prose: policy.contain_links(prose, org))


def review_body(
    check: dict[str, Any],
    selection: dict[str, Any],
    run_url: str,
    folded: list[dict[str, Any]],
) -> str:
    """Compose the review body; the agent never writes to GitHub itself.

    ``folded`` carries the findings that have no line to sit on, which
    appear in the body under their file and line instead. A note says
    how many proposed findings were malformed, so a short review never
    passes for a clean one.
    """
    count = finding_count(check)
    model = policy.sanitise(selection.get("model"), 100) or "unknown"
    org = str(check["repository"]).partition("/")[0]
    summary = policy.contain_links(
        policy.sanitise(check.get("summary"), policy.MAX_SUMMARY_CHARS), org
    )
    # With no line-level finding the summary carries the verdict, and
    # may name a whole-change blocker, so the opening claims nothing.
    if count:
        opening = f"{OPENING}: {count} finding(s) for the human reviewer."
    elif summary:
        opening = (
            f"{OPENING}: no line-level findings; the summary below is the verdict."
        )
    else:
        opening = f"{OPENING}: no findings on this head."
    parts = [opening, summary]
    if folded:
        parts.append(
            "\n\n".join(
                f"- `{policy.sanitise(c['path'], 200)}` line {c['line']}, "
                f"**{c['severity']}**:\n\n{contained(c['body'], org)}"
                for c in folded
            )
        )
    dropped = len(cast("list[Any]", check.get("dropped") or []))
    if dropped:
        parts.append(
            f"_{dropped} further proposed finding(s) were malformed and are not "
            "shown; the run's check summary names them._"
        )
    capped = check.get("capped")
    if type(capped) is int and capped > 0:
        parts.append(
            f"_The agent proposed {capped} finding(s) beyond the cap of "
            f"{comments.MAX_COMMENTS}; they were not read._"
        )
    footer = (
        f"---\nAutomated antagonistic review by `{reviewer_of(selection)}` using "
        f"model `{model}`; see the [run]({run_url}). {DISCLAIMER}"
    )
    return "\n\n".join([*(part for part in parts if part), footer])


def gate_reasons(
    check: dict[str, Any],
    state: dict[str, Any],
    history: list[dict[str, Any]],
    selection: dict[str, Any],
) -> list[str]:
    """Reasons the live pull request may no longer take this review.

    No CI or approval gate: nothing here approves, so neither can be
    relied on. The author must still be a listed bot, and the bot must
    not have reviewed this head already; ``history`` is every review,
    not the state query's last 100, so an old one is not missed.
    """
    reasons: list[str] = []
    if state.get("state") != "OPEN":
        reasons.append(f"pull request is {state.get('state')}")
    if state.get("is_draft"):
        reasons.append("pull request is a draft")
    if state.get("head_sha") != check["head_sha"]:
        reasons.append(f"head moved to {str(state.get('head_sha'))[:12]}")
    authors = [
        str(login)
        for login in cast("list[Any]", selection.get("bot_authors") or [])
        if isinstance(login, str)
    ]
    author = cast("dict[str, Any] | None", state.get("author"))
    if author_skip(author, mode=comments.MODE, bot_authors=authors) is not None:
        reasons.append("the author is no longer a listed bot")
    bot_login = selection.get("bot_login")
    if (
        isinstance(bot_login, str)
        and bot_login
        and reviewed_by(history, bot_login, check["head_sha"])
    ):
        reasons.append("this head already carries an antagonistic review")
    return reasons


def post(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Send one review, refusing any event but COMMENT before it leaves.

    The one place this module writes. The check runs at the call, so
    a payload built some other way still cannot approve or request
    changes through this applier.
    """
    if payload.get("event") != EVENT:
        raise ValueError("the antagonistic applier posts COMMENT reviews alone")
    return github.api_write("POST", endpoint, payload)


def submit(
    check: dict[str, Any], selection: dict[str, Any], run_url: str
) -> tuple[dict[str, Any], bool]:
    """Post the one COMMENT review; return the reply and whether it went inline.

    GitHub refuses the whole review with 422 when it cannot place one
    comment, and creates nothing; the comments then travel in the
    body, so a stale anchor costs its placement and not the review.
    """
    repository, number = str(check["repository"]), int(check["number"])
    endpoint = f"repos/{repository}/pulls/{number}/reviews"
    found = findings_of(check, "comments")
    unplaced = findings_of(check, "unplaced")
    payload: dict[str, Any] = {
        "commit_id": check["head_sha"],
        "event": EVENT,
        "body": review_body(check, selection, run_url, folded=unplaced),
    }
    if found:
        org = repository.partition("/")[0]
        payload["comments"] = [inline(comment, org) for comment in found]
    try:
        return post(endpoint, payload), bool(found)
    except github.GitHubError as exc:
        if exc.status != UNPROCESSABLE or not found:
            raise
        files.note(
            SOURCE,
            "GitHub could not place the inline comments; folding them into the body",
        )
    payload = {
        "commit_id": check["head_sha"],
        "event": EVENT,
        "body": review_body(check, selection, run_url, folded=[*found, *unplaced]),
    }
    return post(endpoint, payload), False


def run_comment(args: argparse.Namespace) -> None:
    """Gate the live pull request, then comment or record why not."""
    check = validated_check(args.check)
    selection = files.load_json(
        args.selection, evidence.MAX_EVIDENCE_BYTES, "selection"
    )
    files.require_mode(selection, comments.MODE, "selection")
    repository, number = str(check["repository"]), int(check["number"])
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "key": check.get("key"),
        "repository": repository,
        "number": number,
        "url": check.get("url"),
        "title": check.get("title"),
        "head_sha": check["head_sha"],
        "verdict": "refused",
        "tier": None,
        "reasons": list(cast("list[str]", check.get("reasons") or [])),
        "summary": check.get("summary"),
        "findings": [],
        "comment_count": finding_count(check),
        "dropped_count": len(cast("list[Any]", check.get("dropped") or [])),
        "capped_count": check["capped"],
        "inline": None,
        "injection_attempts": check.get("injection_attempts") or [],
        "review_url": None,
        "dry_run": bool(args.dry_run),
        "run_attempt": args.run_attempt,
        "premium_requests": check.get("premium_requests"),
        "agent_seconds": check.get("agent_seconds"),
    }
    reasons = cast("list[str]", result["reasons"])
    if check.get("commentable") is True:
        state = pull_reads.pull_state(repository, number)
        history = pull_changes.pull_reviews(repository, number)
        reasons.extend(gate_reasons(check, state, history, selection))
        if reasons:
            result["verdict"] = "refused"
        elif args.dry_run:
            result["verdict"] = "would-comment"
        else:
            reply, placed = submit(check, selection, args.run_url)
            url = reply.get("html_url")
            result["review_url"] = url if isinstance(url, str) else None
            result["inline"] = placed
            result["verdict"] = "commented"
    elif not reasons:
        reasons.append("check found no usable verdict")
    files.write_json(args.output, result)
    review_url = result["review_url"] or "-"
    print(f"comment: {result['verdict']} {repository}#{number} review={review_url}")


def build_parser() -> argparse.ArgumentParser:
    """Describe the check and comment commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="offline verdict and anchor check")
    check.add_argument("--selection", type=Path, required=True)
    check.add_argument("--key", required=True)
    check.add_argument("--session-dir", type=Path, required=True)
    check.add_argument("--failure", default=None)
    check.add_argument("--output", type=Path, required=True)
    check.add_argument("--summary", type=Path, required=True)
    check.set_defaults(handler=run_check)

    comment = commands.add_parser("comment", help="gate live state and comment")
    comment.add_argument("--check", type=Path, required=True)
    comment.add_argument("--selection", type=Path, required=True)
    comment.add_argument("--output", type=Path, required=True)
    comment.add_argument("--run-url", required=True)
    comment.add_argument("--run-attempt", type=int, required=True)
    comment.add_argument("--dry-run", action="store_true")
    comment.set_defaults(handler=run_comment)
    return parser


def main(argv: list[str] | None = None) -> None:
    """Dispatch a command, keeping operational failures on the error path."""
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = cast("Callable[[argparse.Namespace], None]", args.handler)
    try:
        handler(args)
    except (OSError, ValueError, github.GitHubError) as exc:
        parser.exit(1, f"comment_review: {github.safe_message(exc)}\n")


if __name__ == "__main__":
    main()
