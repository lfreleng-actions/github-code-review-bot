# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""GitHub reads behind the pull request selection, against patched transport."""

from __future__ import annotations

import json
import sys
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
github = import_module("review_github")
reads = import_module("pull_reads")
changes = import_module("pull_changes")

HEAD = "a" * 40
BASE = "b" * 40
OLD_HEAD = "c" * 40


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
        "baseRefOid": BASE,
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


def pages(*entries: dict[str, Any]) -> str:
    """The slurped single page ``api_list`` reads."""
    return json.dumps([list(entries)])


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
            pull_node(baseRefOid=None),
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


class PullFilesTest(ReadsCase):
    """``pull_files`` bounds patches, counts bytes and flags oversize changes."""

    def test_shape_and_bounds(self) -> None:
        """Patches are bounded per file; a missing patch and a rename are kept."""
        big = "x" * (reads.MAX_PATCH_BYTES + 5)
        entries = pages(
            rest_file("README.md"),
            rest_file("big.txt", big),
            rest_file("image.png", None, status="added", additions=0, deletions=0),
            rest_file("new.py", status="renamed", previous_filename="old.py"),
        )
        with patch.object(github, "run_gh", return_value=entries) as gh:
            files, diff_bytes, too_large = changes.pull_files("org/alpha", 7)
        self.assertEqual(
            gh.call_args.args[0],
            [
                "api",
                "repos/org/alpha/pulls/7/files?per_page=100",
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
        entries = pages(*(rest_file(f"f{i}.txt") for i in range(reads.MAX_FILES + 1)))
        with patch.object(github, "run_gh", return_value=entries):
            files, _, too_large = changes.pull_files("org/alpha", 7)
        self.assertTrue(too_large)
        self.assertEqual(len(files), reads.MAX_FILES)

    def test_too_many_patch_bytes(self) -> None:
        """Patch bytes beyond ``MAX_DIFF_BYTES`` are too large even across small files."""
        chunk = "y" * (reads.MAX_DIFF_BYTES // 2 + 1)
        entries = pages(rest_file("a.txt", chunk), rest_file("b.txt", chunk))
        with patch.object(github, "run_gh", return_value=entries):
            files, diff_bytes, too_large = changes.pull_files("org/alpha", 7)
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
        ):
            with (
                self.subTest(entry=entry),
                patch.object(github, "run_gh", return_value=pages(entry)),
                self.assertRaises(github.GitHubError),
            ):
                changes.pull_files("org/alpha", 7)


class PullCommitsTest(ReadsCase):
    """``pull_commits`` reads SHAs, bounded messages, verification and authors."""

    def test_shape(self) -> None:
        """Each commit carries its SHA, message, verified flag and author login."""
        long_message = "m" * (reads.MAX_MESSAGE_BYTES + 1)
        entries = pages(
            {
                "sha": HEAD,
                "commit": {"message": "Fix: Thing", "verification": {"verified": True}},
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
        )
        with patch.object(github, "run_gh", return_value=entries) as gh:
            commits = changes.pull_commits("org/alpha", 7)
        self.assertEqual(
            gh.call_args.args[0][1], "repos/org/alpha/pulls/7/commits?per_page=100"
        )
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
        ):
            with (
                self.subTest(entry=entry),
                patch.object(github, "run_gh", return_value=pages(entry)),
                self.assertRaises(github.GitHubError),
            ):
                changes.pull_commits("org/alpha", 7)


if __name__ == "__main__":
    unittest.main()
