# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""GitHub reads behind the pull request selection, against patched transport."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
github = import_module("bot_github")
reads = import_module("pull_reads")
changes = import_module("pull_changes")

HEAD = "a" * 40
BASE = "b" * 40
OLD_HEAD = "c" * 40
NOW = datetime(2026, 11, 12, 2, 0, tzinfo=UTC)
PRE_COMMIT = "pre-commit.ci - pr"


def actor(login: str | None, kind: str = "User") -> dict[str, Any] | None:
    """A GraphQL actor, or null for a deleted account."""
    return None if login is None else {"__typename": kind, "login": login}


def review(
    login: str | None,
    state: str,
    commit: str | None = HEAD,
    kind: str = "User",
) -> dict[str, Any]:
    """One review node as the GraphQL query returns it."""
    return {
        "author": actor(login, kind),
        "state": state,
        "submittedAt": "2026-02-01T00:00:00Z",
        "commit": {"oid": commit} if commit else None,
    }


def thread(login: str | None, *, resolved: bool = False) -> dict[str, Any]:
    """One review thread whose first comment is by ``login``."""
    return {
        "isResolved": resolved,
        "comments": {"nodes": [{"author": actor(login, "Bot")}]},
    }


def rollup(state: str | None, *contexts: dict[str, Any]) -> dict[str, Any] | None:
    """A status check rollup, or null when the head has none."""
    if state is None:
        return None
    return {"state": state, "contexts": {"nodes": list(contexts)}}


def check_run(name: str, conclusion: str | None = "SUCCESS") -> dict[str, Any]:
    """One CheckRun context."""
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": "COMPLETED" if conclusion else "IN_PROGRESS",
        "conclusion": conclusion,
    }


def status(name: str, state: str, minutes_ago: int) -> dict[str, Any]:
    """One StatusContext created ``minutes_ago`` before ``NOW``."""
    created = NOW - timedelta(minutes=minutes_ago)
    return {
        "__typename": "StatusContext",
        "context": name,
        "state": state,
        "createdAt": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def pull_node(**overrides: Any) -> dict[str, Any]:
    """A healthy pull request node that no skip rule would catch."""
    node: dict[str, Any] = {
        "number": 7,
        "url": "https://github.com/org/alpha/pull/7",
        "title": "Fix: Correct the thing",
        "body": "Body",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-02T00:00:00Z",
        "authorAssociation": "MEMBER",
        "author": actor("alice"),
        "headRefOid": HEAD,
        "headRefName": "fix/thing",
        "baseRefName": "main",
        "baseRef": {"target": {"oid": BASE}},
        "isCrossRepository": True,
        "headRepository": {"nameWithOwner": "alice/alpha"},
        "labels": {"nodes": [{"name": "bug"}]},
        "commits": {
            "totalCount": 2,
            "nodes": [
                {
                    "commit": {
                        "statusCheckRollup": rollup("SUCCESS", check_run("Testing"))
                    }
                }
            ],
        },
        "reviews": {"nodes": []},
        "reviewRequests": {"nodes": []},
        "reviewThreads": {"nodes": []},
    }
    node.update(overrides)
    return node


def reply(
    node: dict[str, Any] | None,
    *,
    repository: str | None = "org/alpha",
    archived: bool = False,
) -> str:
    """The gh stdout for the pull request query."""
    repo: dict[str, Any] | None = None
    if repository is not None:
        repo = {
            "nameWithOwner": repository,
            "isArchived": archived,
            "pullRequest": node,
        }
    return json.dumps({"data": {"repository": repo}})


def compared(files: list[Any] | None = None, commits: list[Any] | None = None) -> str:
    """One slurped comparison page; by default one commit and no files."""
    page = {
        "files": files or [],
        "commits": commits
        if commits is not None
        else [{"sha": HEAD, "commit": {"message": "x"}, "author": None}],
    }
    return json.dumps([page])


def read_comparison() -> tuple[list[dict[str, Any]], int, bool, list[dict[str, Any]]]:
    """Read the fixed comparison the tests serve."""
    return changes.pull_comparison("org/alpha", BASE, HEAD)


class ReadsCase(unittest.TestCase):
    """Base class forbidding any real ``gh`` invocation."""

    def setUp(self) -> None:
        """Fail fast when a test reaches the subprocess layer."""
        guard = patch.object(
            github.subprocess,
            "run",
            side_effect=AssertionError("unexpected subprocess"),
        )
        guard.start()
        self.addCleanup(guard.stop)

    def state_of(self, node: dict[str, Any] | None, **kwargs: Any) -> dict[str, Any]:
        """Run ``pull_state`` against one canned reply."""
        with patch.object(github, "run_gh", return_value=reply(node, **kwargs)):
            return reads.pull_state("org/alpha", 7)


class IsBotAuthorTest(unittest.TestCase):
    """``is_bot_author`` treats anything that is not a plain human as a bot."""

    def test_humans(self) -> None:
        """A User with a plain login, from search or GraphQL, is human."""
        self.assertFalse(reads.is_bot_author({"login": "alice", "is_bot": False}))
        self.assertFalse(reads.is_bot_author({"__typename": "User", "login": "bob"}))

    def test_bots(self) -> None:
        """The flag, either type marker, a [bot] suffix or no author at all."""
        for author in (
            None,
            {"login": "x", "is_bot": True},
            {"login": "x", "type": "Bot"},
            {"__typename": "Bot", "login": "x"},
            {"login": "dependabot[bot]"},
            {"is_bot": False},
            {"login": 5},
        ):
            with self.subTest(author=author):
                self.assertTrue(reads.is_bot_author(author))


class SmallHelpersTest(unittest.TestCase):
    """``truncate_utf8``, ``bot_login`` and ``is_copilot``."""

    def test_truncate_counts_bytes(self) -> None:
        """The bound is in encoded bytes and never splits a character."""
        self.assertEqual(reads.truncate_utf8("ab", 5), "ab")
        self.assertEqual(reads.truncate_utf8("é" * 3, 3), "é")

    def test_bot_login(self) -> None:
        """A slug gains the [bot] suffix; no slug means no login."""
        self.assertEqual(reads.bot_login("my-app"), "my-app[bot]")
        self.assertIsNone(reads.bot_login(""))

    def test_is_copilot(self) -> None:
        """Every known Copilot login matches regardless of case; others do not."""
        for login in reads.COPILOT_LOGINS:
            self.assertTrue(reads.is_copilot(login.upper()))
        self.assertFalse(reads.is_copilot("alice"))
        self.assertFalse(reads.is_copilot(None))


class SearchOpenPullsTest(ReadsCase):
    """``search_open_pulls`` scopes the search and refuses truncation."""

    def test_arguments(self) -> None:
        """The search asks for open, non-draft pull requests of the owner."""
        with patch.object(github, "run_gh", return_value="[]") as gh:
            self.assertEqual(reads.search_open_pulls("org", ["a", "B"]), [])
        args = gh.call_args.args[0]
        self.assertEqual(args[:2], ["search", "prs"])
        self.assertEqual(args[args.index("--owner") + 1], "org")
        self.assertEqual(args[args.index("--state") + 1], "open")
        self.assertIn("--draft=false", args)
        self.assertEqual(args[args.index("--limit") + 1], str(reads.SEARCH_LIMIT))
        self.assertEqual(args[args.index("--json") + 1], reads.SEARCH_FIELDS)
        self.assertEqual(
            [args[i + 1] for i, a in enumerate(args) if a == "--repo"],
            ["org/a", "org/B"],
        )

    def test_owner_scan_has_no_repo_arguments(self) -> None:
        """Without named repositories the whole owner is searched."""
        with patch.object(github, "run_gh", return_value='[{"number": 1}]') as gh:
            self.assertEqual(reads.search_open_pulls("org", []), [{"number": 1}])
        self.assertNotIn("--repo", gh.call_args.args[0])

    def test_ceiling_raises(self) -> None:
        """A result set at the limit may be truncated and is refused."""
        payload = json.dumps([{"number": i} for i in range(reads.SEARCH_LIMIT)])
        with (
            patch.object(github, "run_gh", return_value=payload),
            self.assertRaises(reads.SelectionError),
        ):
            reads.search_open_pulls("org", [])

    def test_malformed_payloads_raise(self) -> None:
        """A non-array reply or non-object entries are an API failure."""
        for payload in ('{"a": 1}', "[1, 2]"):
            with (
                self.subTest(payload=payload),
                patch.object(github, "run_gh", return_value=payload),
                self.assertRaises(github.GitHubError),
            ):
                reads.search_open_pulls("org", [])


class PullStateTest(ReadsCase):
    """``pull_state`` normalises one GraphQL reply into the selection's view."""

    def test_query_targets_the_pull_request_as_a_read(self) -> None:
        """One read-only GraphQL call carries the owner, name and number."""
        with patch.object(github, "run_gh", return_value=reply(pull_node())) as gh:
            reads.pull_state("org/alpha", 7)
        gh.assert_called_once()
        args = gh.call_args.args[0]
        self.assertEqual(args[:2], ["api", "graphql"])
        self.assertIs(gh.call_args.kwargs.get("read"), True)
        payload = json.loads(gh.call_args.kwargs["input"])
        self.assertEqual(
            payload["variables"], {"owner": "org", "name": "alpha", "number": 7}
        )
        self.assertIn("isCrossRepository", payload["query"])

    @unittest.expectedFailure
    def test_extracts_fields(self) -> None:
        """Every field is typed and the derived flags are computed."""
        state = self.state_of(pull_node())
        self.assertEqual(
            state,
            {
                "repository": "org/alpha",
                "repo_name": "alpha",
                "repository_archived": False,
                "number": 7,
                "url": "https://github.com/org/alpha/pull/7",
                "title": "Fix: Correct the thing",
                "body": "Body",
                "state": "OPEN",
                "is_draft": False,
                "author": {"login": "alice", "is_bot": False},
                "author_association": "MEMBER",
                "head_sha": HEAD,
                "head_repository": "alice/alpha",
                "head_ref": "fix/thing",
                "base_ref": "main",
                "base_sha": BASE,
                "is_fork": True,
                "labels": ["bug"],
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-02T00:00:00Z",
                "mergeable": "MERGEABLE",
                "ci": "success",
                "checks": [
                    {"name": "Testing", "status": "COMPLETED", "conclusion": "SUCCESS"}
                ],
                "stale_statuses": [],
                "reviews": [],
                "review_requests": [],
                "unresolved_copilot_threads": 0,
                "changes_requested": False,
                "approved_by": [],
                "commits_count": 2,
            },
        )

    def test_ci_mapping(self) -> None:
        """Rollup states collapse onto success, pending, failure or none."""
        expected = {
            "SUCCESS": "success",
            "PENDING": "pending",
            "EXPECTED": "pending",
            "FAILURE": "failure",
            "ERROR": "failure",
            None: "none",
        }
        for rollup_state, ci in expected.items():
            with self.subTest(rollup=rollup_state):
                node = pull_node(
                    commits={
                        "totalCount": 1,
                        "nodes": [
                            {"commit": {"statusCheckRollup": rollup(rollup_state)}}
                        ],
                    }
                )
                state = self.state_of(node)
                self.assertEqual(state["ci"], ci)
                self.assertEqual(state["checks"], [])

    def test_status_contexts_become_completed_checks(self) -> None:
        """A commit status maps onto a completed check; an in-progress run keeps null."""
        node = pull_node(
            commits={
                "totalCount": 1,
                "nodes": [
                    {
                        "commit": {
                            "statusCheckRollup": rollup(
                                "PENDING",
                                {
                                    "__typename": "StatusContext",
                                    "context": "ci/x",
                                    "state": "SUCCESS",
                                },
                                check_run("Lint", None),
                            )
                        }
                    }
                ],
            }
        )
        self.assertEqual(
            self.state_of(node)["checks"],
            [
                {"name": "ci/x", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "Lint", "status": "IN_PROGRESS", "conclusion": None},
            ],
        )

    def test_unknown_rollup_state_raises(self) -> None:
        """A rollup state outside the documented set is an API fault."""
        node = pull_node(
            commits={
                "totalCount": 1,
                "nodes": [{"commit": {"statusCheckRollup": rollup("ODD")}}],
            }
        )
        with self.assertRaises(github.GitHubError):
            self.state_of(node)

    def test_approval_counts_only_on_the_current_head(self) -> None:
        """An approval of an earlier head is recorded but does not approve."""
        node = pull_node(
            reviews={
                "nodes": [
                    review("old", "APPROVED", OLD_HEAD),
                    review("fresh", "APPROVED"),
                    review("nocommit", "APPROVED", None),
                ]
            }
        )
        state = self.state_of(node)
        self.assertEqual(state["approved_by"], ["fresh"])
        self.assertFalse(state["changes_requested"])
        self.assertEqual(
            state["reviews"][0],
            {
                "author": "old",
                "is_bot": False,
                "state": "APPROVED",
                "commit": OLD_HEAD,
                "submitted_at": "2026-02-01T00:00:00Z",
            },
        )
        self.assertIsNone(state["reviews"][2]["commit"])

    def test_latest_review_per_reviewer_wins(self) -> None:
        """A later verdict replaces an earlier one; pending and dismissed are inert."""
        node = pull_node(
            reviews={
                "nodes": [
                    review("alice", "APPROVED"),
                    review("alice", "CHANGES_REQUESTED"),
                    review("bob", "CHANGES_REQUESTED"),
                    review("bob", "COMMENTED"),
                    review("carol", "DISMISSED"),
                    review("dave", "APPROVED"),
                    review("dave", "PENDING"),
                ]
            }
        )
        state = self.state_of(node)
        self.assertTrue(state["changes_requested"])
        self.assertEqual(state["approved_by"], ["dave"])

    def test_dismissed_changes_do_not_block(self) -> None:
        """A dismissal rewrites the review's state, and that state is inert."""
        node = pull_node(reviews={"nodes": [review("carol", "DISMISSED")]})
        state = self.state_of(node)
        self.assertFalse(state["changes_requested"])
        self.assertEqual(state["reviews"][0]["state"], "DISMISSED")

    def test_deleted_reviewer_and_bot_reviewer(self) -> None:
        """A deleted reviewer has no login; a Bot reviewer is flagged."""
        node = pull_node(
            reviews={
                "nodes": [
                    review(None, "COMMENTED"),
                    review("copilot-pull-request-reviewer", "COMMENTED", kind="Bot"),
                ]
            }
        )
        reviews = self.state_of(node)["reviews"]
        self.assertEqual(
            [(r["author"], r["is_bot"]) for r in reviews],
            [(None, True), ("copilot-pull-request-reviewer", True)],
        )

    def test_unknown_review_state_raises(self) -> None:
        """A review state outside the documented set is an API fault."""
        node = pull_node(reviews={"nodes": [review("alice", "SHRUGGED")]})
        with self.assertRaises(github.GitHubError):
            self.state_of(node)

    def test_review_requests_and_copilot_threads(self) -> None:
        """Requested users, bots and teams are listed; only Copilot's open threads count."""
        node = pull_node(
            reviewRequests={
                "nodes": [
                    {"requestedReviewer": {"__typename": "User", "login": "alice"}},
                    {"requestedReviewer": {"__typename": "Bot", "login": "Copilot"}},
                    {"requestedReviewer": {"__typename": "Team", "slug": "releng"}},
                    {"requestedReviewer": None},
                ]
            },
            reviewThreads={
                "nodes": [
                    thread("copilot-pull-request-reviewer"),
                    thread("COPILOT-PULL-REQUEST-REVIEWER[bot]"),
                    thread("copilot", resolved=True),
                    thread("alice"),
                    thread(None),
                    {"isResolved": False, "comments": {"nodes": []}},
                ]
            },
        )
        state = self.state_of(node)
        self.assertEqual(state["review_requests"], ["alice", "Copilot", "releng"])
        self.assertEqual(state["unresolved_copilot_threads"], 2)

    def test_deleted_author_is_not_human(self) -> None:
        """A null author normalises to no login and the bot flag."""
        state = self.state_of(pull_node(author=None))
        self.assertEqual(state["author"], {"login": None, "is_bot": True})

    def test_missing_head_repository(self) -> None:
        """A deleted fork leaves the head repository null."""
        state = self.state_of(pull_node(headRepository=None))
        self.assertIsNone(state["head_repository"])

    def test_archived_repository_is_reported(self) -> None:
        """The repository's archived flag rides along for the skip rule."""
        self.assertTrue(
            self.state_of(pull_node(), archived=True)["repository_archived"]
        )

    def test_body_bounded_and_null_body(self) -> None:
        """The body is cut at ``MAX_BODY_BYTES``; a null body becomes empty."""
        long_body = "b" * (reads.MAX_BODY_BYTES + 10)
        self.assertEqual(
            len(self.state_of(pull_node(body=long_body))["body"]), reads.MAX_BODY_BYTES
        )
        self.assertEqual(self.state_of(pull_node(body=None))["body"], "")

    def test_absent_targets_raise(self) -> None:
        """No repository, no pull request or a mismatched reply is an error."""
        cases: list[tuple[dict[str, Any] | None, dict[str, Any]]] = [
            (pull_node(), {"repository": None}),
            (None, {}),
            (pull_node(number=8), {}),
            (pull_node(), {"repository": "org/beta"}),
        ]
        for node, kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(github.GitHubError):
                self.state_of(node, **kwargs)

    def test_invalid_fields_raise(self) -> None:
        """Each untrusted field is validated rather than passed through."""
        bad_nodes = [
            pull_node(url="http://evil.example/pull/7"),
            pull_node(state="WEIRD"),
            pull_node(mergeable="MAYBE"),
            pull_node(isDraft="no"),
            pull_node(isCrossRepository=None),
            pull_node(headRefOid="short"),
            pull_node(baseRef={"target": None}),
            pull_node(headRepository={"nameWithOwner": "bad name"}),
            pull_node(body=["list"]),
            pull_node(author={"__typename": "User"}),
            pull_node(labels={"nodes": [{"name": ""}]}),
            pull_node(commits={"totalCount": 1, "nodes": []}),
            pull_node(
                commits={
                    "totalCount": 1,
                    "nodes": [
                        {
                            "commit": {
                                "statusCheckRollup": rollup(
                                    "SUCCESS", {"__typename": "Odd"}
                                )
                            }
                        }
                    ],
                }
            ),
            pull_node(
                reviewThreads={
                    "nodes": [{"isResolved": "yes", "comments": {"nodes": []}}]
                }
            ),
        ]
        for index, node in enumerate(bad_nodes):
            with self.subTest(index=index), self.assertRaises(github.GitHubError):
                self.state_of(node)


class StaleStatusTest(ReadsCase):
    """A listed commit status waiting past the bound does not hold CI pending."""

    def read(
        self,
        rollup_state: str,
        *contexts: dict[str, Any],
        minutes: int = 8,
        listed: tuple[str, ...] = (PRE_COMMIT,),
        total: int | None = None,
    ) -> dict[str, Any]:
        """Run ``pull_state`` on one rollup under a stale status policy."""
        node = pull_node(
            commits={
                "totalCount": 1,
                "nodes": [
                    {
                        "commit": {
                            "statusCheckRollup": {
                                "state": rollup_state,
                                "contexts": {
                                    "totalCount": len(contexts)
                                    if total is None
                                    else total,
                                    "nodes": list(contexts),
                                },
                            }
                        }
                    }
                ],
            }
        )
        policy = import_module("pull_checks").StalePolicy(
            contexts=frozenset(listed), minutes=minutes, now=NOW
        )
        with patch.object(github, "run_gh", return_value=reply(node)):
            return reads.pull_state("org/alpha", 7, policy)

    @unittest.expectedFailure
    def test_query_reads_when_a_status_was_created(self) -> None:
        """The state query asks for each commit status's creation time."""
        self.assertIn("StatusContext { context state createdAt }", reads.PULL_QUERY)
        self.assertIn("contexts(first: 100) { totalCount", reads.PULL_QUERY)

    @unittest.expectedFailure
    def test_an_old_listed_status_leaves_ci_to_the_other_contexts(self) -> None:
        """Past the bound the listed status is stale and recorded as such."""
        state = self.read(
            "PENDING", check_run("Testing"), status(PRE_COMMIT, "PENDING", 30)
        )
        self.assertEqual(state["ci"], "success")
        self.assertEqual(state["stale_statuses"], [PRE_COMMIT])
        self.assertEqual(
            state["checks"][1],
            {"name": PRE_COMMIT, "status": "COMPLETED", "conclusion": "PENDING"},
        )

    @unittest.expectedFailure
    def test_an_expected_status_goes_stale_too(self) -> None:
        """``EXPECTED`` waits like ``PENDING`` does."""
        state = self.read(
            "EXPECTED", check_run("Testing"), status(PRE_COMMIT, "EXPECTED", 30)
        )
        self.assertEqual(state["ci"], "success")

    @unittest.expectedFailure
    def test_the_other_contexts_still_decide(self) -> None:
        """Another pending or failing context keeps its effect."""
        for other, ci in (
            (check_run("Lint", None), "pending"),
            (check_run("Lint", "FAILURE"), "failure"),
            (status("ci/x", "PENDING", 30), "pending"),
            (status("ci/x", "ERROR", 30), "failure"),
            (check_run("Lint", "NEUTRAL"), "success"),
        ):
            with self.subTest(other=other):
                state = self.read("PENDING", other, status(PRE_COMMIT, "PENDING", 30))
                self.assertEqual(state["ci"], ci)
                self.assertEqual(state["stale_statuses"], [PRE_COMMIT])

    @unittest.expectedFailure
    def test_a_lone_stale_status_leaves_no_ci(self) -> None:
        """With nothing else reporting, CI is absent rather than green."""
        state = self.read("PENDING", status(PRE_COMMIT, "PENDING", 30))
        self.assertEqual(state["ci"], "none")

    @unittest.expectedFailure
    def test_a_young_listed_status_keeps_ci_pending(self) -> None:
        """Within the bound the status counts like any other."""
        for minutes_ago in (2, 8):
            with self.subTest(minutes_ago=minutes_ago):
                state = self.read(
                    "PENDING",
                    check_run("Testing"),
                    status(PRE_COMMIT, "PENDING", minutes_ago),
                )
                self.assertEqual(state["ci"], "pending")
                self.assertEqual(state["stale_statuses"], [])

    @unittest.expectedFailure
    def test_an_unlisted_status_keeps_ci_pending_whatever_its_age(self) -> None:
        """Only a listed context can go stale."""
        state = self.read(
            "PENDING", check_run("Testing"), status("ci/other", "PENDING", 600)
        )
        self.assertEqual(state["ci"], "pending")
        self.assertEqual(state["stale_statuses"], [])

    @unittest.expectedFailure
    def test_a_check_run_never_goes_stale(self) -> None:
        """A check run named like a listed status still holds CI pending."""
        state = self.read("PENDING", check_run("Testing"), check_run(PRE_COMMIT, None))
        self.assertEqual(state["ci"], "pending")
        self.assertEqual(state["stale_statuses"], [])

    @unittest.expectedFailure
    def test_a_failing_listed_status_fails(self) -> None:
        """A listed status that reported failure fails the head, however old."""
        state = self.read(
            "FAILURE", check_run("Testing"), status(PRE_COMMIT, "FAILURE", 600)
        )
        self.assertEqual(state["ci"], "failure")
        self.assertEqual(state["stale_statuses"], [])

    @unittest.expectedFailure
    def test_zero_minutes_disables_the_rule(self) -> None:
        """A bound of 0 leaves the rollup as GitHub reports it."""
        state = self.read(
            "PENDING",
            check_run("Testing"),
            status(PRE_COMMIT, "PENDING", 600),
            minutes=0,
        )
        self.assertEqual(state["ci"], "pending")
        self.assertEqual(state["stale_statuses"], [])

    @unittest.expectedFailure
    def test_a_partial_context_list_keeps_ci_pending(self) -> None:
        """Contexts beyond the first page might be pending, so nothing lifts."""
        state = self.read(
            "PENDING",
            check_run("Testing"),
            status(PRE_COMMIT, "PENDING", 30),
            total=150,
        )
        self.assertEqual(state["ci"], "pending")
        self.assertEqual(state["stale_statuses"], [])

    @unittest.expectedFailure
    def test_without_a_policy_every_context_counts(self) -> None:
        """The default read discounts nothing."""
        node = pull_node(
            commits={
                "totalCount": 1,
                "nodes": [
                    {
                        "commit": {
                            "statusCheckRollup": rollup(
                                "PENDING",
                                check_run("Testing"),
                                status(PRE_COMMIT, "PENDING", 600),
                            )
                        }
                    }
                ],
            }
        )
        state = self.state_of(node)
        self.assertEqual(state["ci"], "pending")
        self.assertEqual(state["stale_statuses"], [])

    @unittest.expectedFailure
    def test_an_unreadable_creation_time_raises(self) -> None:
        """A listed waiting status without a zoned timestamp is an API fault."""
        for created in ("yesterday", "2026-11-12T00:00:00", None):
            broken = status(PRE_COMMIT, "PENDING", 30)
            broken["createdAt"] = created
            with self.subTest(created=created), self.assertRaises(github.GitHubError):
                self.read("PENDING", check_run("Testing"), broken)


class PullStateBaseTest(ReadsCase):
    """``pull_state`` takes the base SHA from the base branch's tip."""

    def test_base_sha_is_the_base_branch_tip(self) -> None:
        """A stale recorded base SHA is ignored in favour of the branch tip.

        GitHub's ``baseRefOid`` can lag the base branch. Compared from
        it, a head that merged newer base content and then removed some
        of it would show no removal at all.
        """
        state = self.state_of(pull_node(baseRefOid=OLD_HEAD))
        self.assertEqual(state["base_sha"], BASE)

    def test_a_missing_or_malformed_base_branch_raises(self) -> None:
        """A deleted base branch or a malformed tip is an error, not a match."""
        for base in (None, "main", {"target": None}, {"target": {"oid": "short"}}):
            with self.subTest(base=base), self.assertRaises(github.GitHubError):
                self.state_of(pull_node(baseRef=base))


class PullIdentityTest(ReadsCase):
    """``pull_identity`` reads the two ends of the comparison and nothing else."""

    def identity_of(self, node: dict[str, Any] | None, **kwargs: Any) -> dict[str, str]:
        """Run ``pull_identity`` against one canned reply."""
        with patch.object(github, "run_gh", return_value=reply(node, **kwargs)) as gh:
            identity = reads.pull_identity("org/alpha", 7)
        gh.assert_called_once()
        self.assertIs(gh.call_args.kwargs.get("read"), True)
        return identity

    def test_extracts_the_comparison(self) -> None:
        """The head SHA, base ref and base SHA come back under the state's keys."""
        self.assertEqual(
            self.identity_of(pull_node(baseRefOid=OLD_HEAD)),
            {"head_sha": HEAD, "base_ref": "main", "base_sha": BASE},
        )

    def test_absent_or_invalid_fields_raise(self) -> None:
        """A missing target or a malformed end is an error, not a match."""
        cases: list[tuple[dict[str, Any] | None, dict[str, Any]]] = [
            (pull_node(), {"repository": None}),
            (None, {}),
            (pull_node(headRefOid="short"), {}),
            (pull_node(baseRefName=""), {}),
            (pull_node(baseRef=None), {}),
            (pull_node(baseRef={"target": {"oid": "short"}}), {}),
        ]
        for node, kwargs in cases:
            with self.subTest(node=node, kwargs=kwargs):
                with self.assertRaises(github.GitHubError):
                    self.identity_of(node, **kwargs)


def rest_file(
    path: str, patch_text: str | None = "@@ -1 +1 @@\n-a\n+b", **over: Any
) -> dict[str, Any]:
    """One entry of the REST files listing."""
    entry: dict[str, Any] = {
        "filename": path,
        "status": "modified",
        "additions": 1,
        "deletions": 1,
        "patch": patch_text,
    }
    entry.update(over)
    return entry


class PullComparisonFilesTest(ReadsCase):
    """``pull_comparison`` bounds patches, counts bytes and flags oversize changes."""

    def test_shape_and_bounds(self) -> None:
        """Patches are bounded per file; a missing patch and a rename are kept."""
        big = "x" * (reads.MAX_PATCH_BYTES + 5)
        entries = compared(
            [
                rest_file("README.md"),
                rest_file("big.txt", big),
                rest_file("image.png", None, status="added", additions=0, deletions=0),
                rest_file("new.py", status="renamed", previous_filename="old.py"),
            ]
        )
        with patch.object(github, "run_gh", return_value=entries) as gh:
            files, diff_bytes, too_large, _ = read_comparison()
        self.assertEqual(
            gh.call_args.args[0],
            [
                "api",
                f"repos/org/alpha/compare/{BASE}...{HEAD}?per_page=100",
                "--paginate",
                "--slurp",
            ],
        )
        self.assertFalse(too_large)
        self.assertEqual(diff_bytes, 2 * len("@@ -1 +1 @@\n-a\n+b") + len(big))
        self.assertEqual(
            files[0],
            {
                "path": "README.md",
                "status": "modified",
                "additions": 1,
                "deletions": 1,
                "previous_path": None,
                "patch": "@@ -1 +1 @@\n-a\n+b",
                "patch_truncated": False,
            },
        )
        self.assertEqual(len(files[1]["patch"]), reads.MAX_PATCH_BYTES)
        self.assertTrue(files[1]["patch_truncated"])
        self.assertIsNone(files[2]["patch"])
        self.assertFalse(files[2]["patch_truncated"])
        self.assertEqual(files[3]["previous_path"], "old.py")

    def test_too_many_files(self) -> None:
        """More than ``MAX_FILES`` files is too large; the read stops there."""
        entries = compared([rest_file(f"f{i}.txt") for i in range(reads.MAX_FILES + 1)])
        with patch.object(github, "run_gh", return_value=entries):
            files, _, too_large, _ = read_comparison()
        self.assertTrue(too_large)
        self.assertEqual(len(files), reads.MAX_FILES)

    def test_too_many_patch_bytes(self) -> None:
        """Patch bytes beyond ``MAX_DIFF_BYTES`` are too large even across small files."""
        chunk = "y" * (reads.MAX_DIFF_BYTES // 2 + 1)
        entries = compared([rest_file("a.txt", chunk), rest_file("b.txt", chunk)])
        with patch.object(github, "run_gh", return_value=entries):
            files, diff_bytes, too_large, _ = read_comparison()
        self.assertTrue(too_large)
        self.assertEqual(len(files), 1)
        self.assertGreater(diff_bytes, reads.MAX_DIFF_BYTES)

    def test_invalid_entries_raise(self) -> None:
        """Counts, names and patches must have the documented types."""
        for entry in (
            rest_file("a", additions=-1),
            rest_file("a", deletions=True),
            rest_file("a", patch=["x"]),
            rest_file("a", filename=None),
            rest_file("a", status=""),
            rest_file("a", previous_filename=3),
            "README.md",
        ):
            with (
                self.subTest(entry=entry),
                patch.object(github, "run_gh", return_value=compared([entry])),
                self.assertRaises(github.GitHubError),
            ):
                read_comparison()

    def test_pages_and_invalid_pages(self) -> None:
        """Commits span every page, files the first; a malformed page is refused."""
        commit = {"sha": HEAD, "commit": {"message": "x"}, "author": None}
        later = {**commit, "sha": OLD_HEAD}
        two = json.dumps(
            [
                {"files": [rest_file("a.txt")], "commits": [commit]},
                {"commits": [later]},
            ]
        )
        with patch.object(github, "run_gh", return_value=two):
            files, _, too_large, commits = read_comparison()
        self.assertEqual([f["path"] for f in files], ["a.txt"])
        self.assertFalse(too_large)
        self.assertEqual([c["sha"] for c in commits], [HEAD, OLD_HEAD])
        for raw in (
            "{}",
            "[]",
            "[[]]",
            json.dumps([{"commits": [commit]}]),
            json.dumps([{"files": "x", "commits": [commit]}]),
            json.dumps([{"files": []}]),
            json.dumps([{"files": [], "commits": [commit]}, {"files": None}]),
        ):
            with (
                self.subTest(raw=raw),
                patch.object(github, "run_gh", return_value=raw),
                self.assertRaises(github.GitHubError),
            ):
                read_comparison()


class PullComparisonCommitsTest(ReadsCase):
    """``pull_comparison`` reads SHAs, bounded messages, verification and authors."""

    def test_shape(self) -> None:
        """Each commit carries its SHA, message, verified flag and author login."""
        long_message = "m" * (reads.MAX_MESSAGE_BYTES + 1)
        entries = compared(
            commits=[
                {
                    "sha": HEAD,
                    "commit": {
                        "message": "Fix: Thing",
                        "verification": {"verified": True},
                    },
                    "author": {"login": "alice"},
                },
                {
                    "sha": BASE,
                    "commit": {
                        "message": long_message,
                        "verification": {"verified": False},
                    },
                    "author": None,
                },
                {"sha": OLD_HEAD, "commit": {"message": "x"}, "author": {"login": 3}},
            ]
        )
        with patch.object(github, "run_gh", return_value=entries):
            *_, commits = read_comparison()
        self.assertEqual(
            commits[0],
            {"sha": HEAD, "message": "Fix: Thing", "verified": True, "author": "alice"},
        )
        self.assertEqual(len(commits[1]["message"]), reads.MAX_MESSAGE_BYTES)
        self.assertFalse(commits[1]["verified"])
        self.assertIsNone(commits[1]["author"])
        self.assertFalse(commits[2]["verified"])
        self.assertIsNone(commits[2]["author"])

    def test_invalid_entries_raise(self) -> None:
        """A bad SHA, a missing commit object or a non-string message is refused."""
        for entry in (
            {"sha": "short", "commit": {"message": "x"}},
            {"sha": HEAD, "commit": None},
            {"sha": HEAD, "commit": {"message": None}},
            "commit",
        ):
            with (
                self.subTest(entry=entry),
                patch.object(github, "run_gh", return_value=compared(commits=[entry])),
                self.assertRaises(github.GitHubError),
            ):
                read_comparison()


class PullReviewsTest(ReadsCase):
    """``pull_reviews`` keeps Copilot's review bodies alone, bounded."""

    def read(self, entries: list[Any]) -> list[dict[str, Any]]:
        """Run ``pull_reviews`` against one page of REST reviews."""
        with patch.object(github, "run_gh", return_value=json.dumps([entries])):
            return changes.pull_reviews("org/alpha", 7)

    def test_copilot_body_kept_others_dropped(self) -> None:
        """Copilot's body and submission time survive; a human's body does not."""
        copilot = {
            "user": {"login": "copilot-pull-request-reviewer[bot]", "type": "Bot"},
            "state": "COMMENTED",
            "commit_id": HEAD,
            "submitted_at": "2026-02-01T00:00:00Z",
            "body": "### 🟢 Approval recommended\n",
        }
        human = {**copilot, "user": {"login": "alice", "type": "User"}}
        lookalike = {**copilot, "user": {"login": "copilot", "type": "User"}}
        other_bot = {**copilot, "user": {"login": "monkey[bot]", "type": "Bot"}}
        agent = {**copilot, "user": {"login": "Copilot[bot]", "type": "Bot"}}
        reviews = self.read([copilot, human, lookalike, other_bot, agent])
        self.assertEqual(
            reviews[0],
            {
                "author": "copilot-pull-request-reviewer[bot]",
                "is_bot": True,
                "state": "COMMENTED",
                "commit": HEAD,
                "submitted_at": "2026-02-01T00:00:00Z",
                "body": "### 🟢 Approval recommended\n",
            },
        )
        self.assertEqual([r["body"] for r in reviews[1:4]], [None, None, None])
        self.assertEqual(reviews[4]["body"], copilot["body"])

    def test_oversized_body_is_dropped(self) -> None:
        """A body past the classifier's bound is never stored, so it reads unclean."""
        limit = changes.MAX_REVIEW_BODY_BYTES
        entry = {
            "user": {"login": "copilot-pull-request-reviewer[bot]", "type": "Bot"},
            "state": "COMMENTED",
            "commit_id": HEAD,
            "submitted_at": None,
            "body": "x" * limit,
        }
        (fits,) = self.read([entry])
        self.assertEqual(fits["body"], "x" * limit)
        (over,) = self.read([{**entry, "body": "x" * (limit + 1)}])
        self.assertIsNone(over["body"])
        self.assertIsNone(over["submitted_at"])


if __name__ == "__main__":
    unittest.main()
