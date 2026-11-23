# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Review notices: who is owed one, and the one comment that tells them."""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from collections.abc import Generator
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
notice = import_module("review_notice")
text = import_module("notice_text")
rules = import_module("selection_rules")
recovery = import_module("review_recovery")
handback = import_module("handback")
comment_body = import_module("comment_body")

ORG = "org"
REPO = "org/repo"
HEAD = "a" * 40
OTHER = "b" * 40
APP = "lf-releng-antagonistic-review-bot"
MONKEY = "lf-releng-code-monkey-bot"
WORKFLOW = "lfreleng-actions/github-code-review-bot"
RUN = 42
RUN_URL = f"https://github.com/{WORKFLOW}/actions/runs/{RUN}"
TITLE = "Fix: Title words <!-- antagonistic-notice -->"
BODY = "Body words that must stay out"
# The one endpoint a notice may write to, spelt independently of the module.
WRITE_RE = re.compile(r"repos/[^/]+/[^/]+/issues/[0-9]+/comments")
# What the listing job's token cannot read: it holds no checks or statuses grant.
UNGRANTED_RE = re.compile(r"statusCheckRollup|CheckRun|StatusContext")
AUTHOR_STATE = notice.pull_reads.pull_author_state


def live(number: int = 7, **overrides: Any) -> dict[str, Any]:
    """What pull_state returns for an open Code Monkey pull request at HEAD."""
    state: dict[str, Any] = {
        "repository": REPO,
        "repo_name": "repo",
        "number": number,
        "state": "OPEN",
        "head_sha": HEAD,
        "author": {"login": MONKEY, "is_bot": True},
        "title": TITLE,
        "body": BODY,
    }
    state.update(overrides)
    return state


def review(
    head: str = HEAD, *, login: str = f"{APP}[bot]", kind: str = "Bot"
) -> dict[str, Any]:
    """One submitted review as the REST listing returns it."""
    return {
        "user": {"login": login, "type": kind},
        "state": "COMMENTED",
        "commit_id": head,
        "submitted_at": "2026-11-22T00:00:00Z",
        "body": "Findings.",
    }


def comment(
    reason: str = "no_review",
    head: str = HEAD,
    run: int = RUN,
    *,
    login: str = f"{APP}[bot]",
    kind: str = "Bot",
) -> dict[str, Any]:
    """An issue comment carrying a notice as this module writes it."""
    body = text.notice_body(reason, head, f"{APP}[bot]", RUN_URL, run, WORKFLOW)
    return {"user": {"login": login, "type": kind}, "body": body}


class GitHubDouble:
    """Every GitHub call a notice makes: the reads, and the one write it asserts."""

    def __init__(self) -> None:
        """Start with no pull requests, reviews or comments."""
        self.states: dict[int, dict[str, Any]] = {}
        self.reviews: dict[int, list[dict[str, Any]]] = {}
        self.comments: list[dict[str, Any]] = []
        self.unreadable: set[str] = set()
        self.writes: list[dict[str, Any]] = []
        self.write_status: int | None = None
        self.lands_anyway = False
        self.late: list[dict[str, Any]] = []
        self.read: list[int] = []

    def fail(self, what: str) -> None:
        """Raise the 502 an unreadable resource answers with."""
        if what in self.unreadable:
            raise notice.github.GitHubError("HTTP 502: ::read failed", 502)

    def pull_state(self, repository: str, number: int) -> dict[str, Any]:
        """Answer ``pull_reads.pull_state``."""
        self.read.append(number)
        self.fail(f"state {number}")
        return self.states[number]

    def api_list(self, endpoint: str) -> list[dict[str, Any]]:
        """Answer the review and issue comment listings."""
        number = int(re.findall(r"/(?:pulls|issues)/([0-9]+)/", endpoint)[0])
        if "/issues/" in endpoint:
            self.fail(f"comments {number}")
            return list(self.comments)
        self.fail(f"reviews {number}")
        return self.reviews.get(number, [])

    def api_write(
        self, method: str, endpoint: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Record a write, which must be an issue comment, as App or failure."""
        assert method == "POST", method
        assert WRITE_RE.fullmatch(endpoint), endpoint
        assert set(payload) == {"body"}, payload
        self.writes.append({"endpoint": endpoint, **payload})
        posted = {"user": {"login": f"{APP}[bot]", "type": "Bot"}, **payload}
        if self.write_status is not None:
            if self.lands_anyway:
                self.comments.append(posted)
            self.comments.extend(self.late)
            raise notice.github.GitHubError("gh: Bad Gateway", self.write_status)
        self.comments.append(posted)
        return posted

    @contextlib.contextmanager
    def installed(self) -> Generator[None]:
        """Patch the GitHub calls for the duration."""
        with (
            patch.object(notice.github, "api_list", side_effect=self.api_list),
            patch.object(notice.github, "api_write", side_effect=self.api_write),
            patch.object(notice.pull_reads, "pull_state", side_effect=self.pull_state),
            patch.object(
                notice.pull_reads, "pull_author_state", side_effect=self.pull_state
            ),
        ):
            yield


def run(argv: list[str]) -> tuple[int, str, str]:
    """Run the CLI; return its exit code, stdout and stderr."""
    out, err = io.StringIO(), io.StringIO()
    code: Any = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            notice.main(argv)
        except SystemExit as exc:
            code = exc.code
    return int(code or 0), out.getvalue(), err.getvalue()


def listed(out: str, pages: int | None = None) -> list[dict[str, Any]]:
    """The matrix entries ``list`` printed, checking its count and page count."""
    count, page_line, matrix = out.splitlines()
    include = json.loads(matrix.removeprefix("matrix="))["include"]
    assert count == f"count={len(include)}", count
    assert re.fullmatch(r"pages=[0-9]+", page_line), page_line
    assert pages is None or page_line == f"pages={pages}", page_line
    return include


class Case(unittest.TestCase):
    """A temp tree holding the bot author list, and a GitHub double."""

    def setUp(self) -> None:
        """Lay out the tree and install the double."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.authors = self.root / "authors.txt"
        self.authors.write_text(f"{MONKEY}\ncopilot\n", encoding="utf-8")
        self.gh = GitHubDouble()
        context = self.gh.installed()
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)


class ClassificationTest(unittest.TestCase):
    """Every skip reason is notifying or silent, never both, never neither."""

    def test_every_skip_reason_is_classified_once(self) -> None:
        """A rule added later fails here until someone classifies it."""
        self.assertEqual(text.NOTIFYING | text.SILENT, frozenset(rules.SKIP_REASONS))
        self.assertFalse(text.NOTIFYING & text.SILENT)

    @unittest.expectedFailure
    def test_an_open_reviewer_thread_notifies_without_asking_again(self) -> None:
        """``reviewer_feedback`` is a rule skip: a sentence, no ask-again."""
        self.assertIn("reviewer_feedback", text.NOTIFYING)
        self.assertEqual(
            text.SENTENCES["reviewer_feedback"],
            (
                "A maintainer's or this bot's review threads on this pull"
                " request are unresolved.",
                False,
            ),
        )


class ListFromSelectionTest(Case):
    """``list --selection``: the selected heads left unreviewed, and named skips."""

    def selection(self, *entries: dict[str, Any], **overrides: Any) -> Path:
        """Write a live antagonistic selection and return its path."""
        data: dict[str, Any] = {
            "schema": 1,
            "mode": "antagonistic",
            "dry_run": False,
            "bot_login": f"{APP}[bot]",
            "explicit_pull_requests": [],
            "skipped": [],
            "pull_requests": list(entries),
        }
        data.update(overrides)
        path = self.root / "selection.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def matrix(self, path: Path) -> list[dict[str, Any]]:
        """Run ``list`` on a selection and return the matrix entries."""
        code, out, _ = run(
            [
                "list",
                "--org",
                ORG,
                "--bot-authors-file",
                str(self.authors),
                "--selection",
                str(path),
            ]
        )
        self.assertEqual(code, 0)
        return listed(out)

    @staticmethod
    def entry(number: int, head: str = HEAD, repo: str = REPO) -> dict[str, Any]:
        """A selected pull request."""
        return {"repository": repo, "number": number, "head_sha": head}

    @staticmethod
    def skip(number: int, reason: str, head: str | None = HEAD) -> dict[str, Any]:
        """A skipped row of a named run."""
        return {
            "repository": REPO,
            "number": number,
            "head_sha": head,
            "reason": reason,
        }

    def test_an_entry_without_this_apps_review_is_owed_no_review(self) -> None:
        """A review of an old head, or another bot's, leaves the head owed."""
        self.gh.reviews = {
            1: [review(OTHER), review(login="copilot-pull-request-reviewer[bot]")],
        }
        include = self.matrix(self.selection(self.entry(1)))
        self.assertEqual(
            include,
            [
                {
                    "key": "repo-1",
                    "repository": REPO,
                    "repo_name": "repo",
                    "number": 1,
                    "head_sha": HEAD,
                    "reason": "no_review",
                }
            ],
        )

    def test_an_entry_this_app_reviewed_is_left_out(self) -> None:
        """The review landed, so nothing is owed."""
        self.gh.reviews = {1: [review()]}
        self.assertEqual(self.matrix(self.selection(self.entry(1))), [])

    def test_an_unreadable_history_keeps_the_entry(self) -> None:
        """The selection vouches for it; post reads again."""
        self.gh.unreadable.add("reviews 1")
        include = self.matrix(self.selection(self.entry(1)))
        self.assertEqual([item["reason"] for item in include], ["no_review"])

    def test_a_named_runs_skips_give_their_reason_and_head(self) -> None:
        """Every notifying reason is owed; every silent one is not."""
        reasons = sorted(text.NOTIFYING) + sorted(text.SILENT)
        skipped = [self.skip(n, r) for n, r in enumerate(reasons, start=1)]
        path = self.selection(
            explicit_pull_requests=[f"{REPO}#{n}" for n in range(1, len(reasons) + 1)],
            skipped=skipped,
        )
        include = self.matrix(path)
        self.assertEqual({item["reason"] for item in include}, set(text.NOTIFYING))
        self.assertTrue(all(item["head_sha"] == HEAD for item in include))
        self.assertEqual(
            [item["number"] for item in include],
            list(range(1, len(text.NOTIFYING) + 1)),
        )

    def test_a_scans_skips_give_nothing(self) -> None:
        """Nobody asked for a scanned pull request."""
        path = self.selection(skipped=[self.skip(1, "cap"), self.skip(2, "draft")])
        self.assertEqual(self.matrix(path), [])

    def test_a_dry_run_gives_nothing(self) -> None:
        """A dry run posts nothing anywhere."""
        path = self.selection(
            self.entry(1),
            dry_run=True,
            explicit_pull_requests=[f"{REPO}#2"],
            skipped=[self.skip(2, "cap")],
        )
        self.assertEqual(self.matrix(path), [])

    def test_rows_outside_the_org_or_without_a_head_give_nothing(self) -> None:
        """Every candidate belongs to the org and names a head."""
        path = self.selection(
            self.entry(1, repo="other/repo"),
            explicit_pull_requests=[f"{REPO}#2"],
            skipped=[self.skip(2, "cap", head=None)],
        )
        self.assertEqual(self.matrix(path), [])

    def test_entries_sort_by_repository_and_number(self) -> None:
        """The matrix order is stable."""
        path = self.selection(
            self.entry(9, repo="org/zeta"),
            self.entry(3),
            explicit_pull_requests=[f"{REPO}#2"],
            skipped=[self.skip(2, "cap")],
        )
        keys = [item["key"] for item in self.matrix(path)]
        self.assertEqual(keys, ["repo-2", "repo-3", "zeta-9"])

    def page(self, path: Path, page: int, pages: int) -> list[dict[str, Any]]:
        """Run ``list --page`` on a selection and return that page's entries."""
        code, out, _ = run(
            [
                "list",
                "--org",
                ORG,
                "--bot-authors-file",
                str(self.authors),
                "--selection",
                str(path),
                "--page",
                str(page),
            ]
        )
        self.assertEqual(code, 0)
        return listed(out, pages=pages)

    def test_every_owed_entry_lands_on_some_page(self) -> None:
        """GitHub refuses a matrix over 256 jobs; the rest go on later pages."""
        total = notice.MATRIX_LIMIT + 2
        path = self.selection(
            explicit_pull_requests=[f"{REPO}#{n}" for n in range(1, total + 1)],
            skipped=[self.skip(n, "draft") for n in range(1, total + 1)],
        )
        first, second, past = (self.page(path, n, pages=2) for n in (0, 1, 2))
        self.assertEqual(len(first), notice.MATRIX_LIMIT)
        self.assertEqual(
            [item["number"] for item in first + second], list(range(1, total + 1))
        )
        self.assertEqual(past, [])

    def test_a_review_that_lands_shifts_no_entry_off_its_page(self) -> None:
        """Pages split the selection before the review read, so none is lost."""
        total = notice.MATRIX_LIMIT + 1
        path = self.selection(*(self.entry(n) for n in range(1, total + 1)))
        self.gh.reviews = {1: [review()]}
        first, second = (self.page(path, n, pages=2) for n in (0, 1))
        self.assertEqual(len(first), notice.MATRIX_LIMIT - 1)
        self.assertEqual([item["number"] for item in second], [total])

    def test_an_approve_selection_is_an_error(self) -> None:
        """Only an antagonistic run owes notices."""
        path = self.selection(self.entry(1), mode="approve")
        code, out, _ = run(
            [
                "list",
                "--org",
                ORG,
                "--bot-authors-file",
                str(self.authors),
                "--selection",
                str(path),
            ]
        )
        self.assertEqual((code, out), (1, ""))


class ListFromNamesTest(Case):
    """``list --pull-requests``: a failed selection's named pull requests."""

    def names(self, names: str) -> tuple[int, list[dict[str, Any]]]:
        """Run ``list`` on names; return the exit code and the entries."""
        code, out, _ = run(
            [
                "list",
                "--org",
                ORG,
                "--bot-authors-file",
                str(self.authors),
                "--pull-requests",
                names,
            ]
        )
        if code:
            return code, []
        return code, listed(out)

    def test_an_open_pull_request_by_a_listed_bot_is_owed_at_its_live_head(
        self,
    ) -> None:
        """The head is the one just read."""
        self.gh.states[1] = live(1, head_sha=OTHER)
        code, include = self.names("repo#1")
        self.assertEqual(code, 0)
        self.assertEqual(
            [(item["number"], item["head_sha"], item["reason"]) for item in include],
            [(1, OTHER, "selection_failed")],
        )

    def test_nothing_vouches_for_the_rest(self) -> None:
        """A human's, a closed one and an unreadable one are left out."""
        self.gh.states[1] = live(1, author={"login": "someone", "is_bot": False})
        self.gh.states[2] = live(2, state="CLOSED")
        self.gh.states[3] = live(3, author={"login": "evil[bot]", "is_bot": True})
        self.gh.unreadable.add("state 4")
        code, include = self.names("repo#1 repo#2 org/repo#3 repo#4")
        self.assertEqual((code, include), (0, []))

    def test_every_named_pull_request_lands_on_some_page(self) -> None:
        """A page reads only its own names, and the last page holds the rest."""
        total = notice.MATRIX_LIMIT + 1
        for number in range(1, total + 1):
            self.gh.states[number] = live(number)
        names = " ".join(f"repo#{n}" for n in range(total, 0, -1))
        code, out, _ = run(
            [
                "list",
                "--org",
                ORG,
                "--bot-authors-file",
                str(self.authors),
                "--pull-requests",
                names,
                "--page",
                "1",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual([item["number"] for item in listed(out, pages=2)], [total])
        self.assertEqual(self.gh.read, [total])

    def test_the_read_asks_for_nothing_the_job_token_cannot_see(self) -> None:
        """A query naming checks or statuses fails on the listing job's token."""
        queries: list[str] = []

        def graphql(query: str, variables: dict[str, Any], **_: Any) -> Any:
            queries.append(query)
            if UNGRANTED_RE.search(query):
                raise notice.github.GitHubError(
                    "Resource not accessible by integration"
                )
            pull = {
                "number": variables["number"],
                "state": "OPEN",
                "headRefOid": OTHER,
                "author": {"__typename": "Bot", "login": MONKEY},
            }
            return {"repository": {"nameWithOwner": REPO, "pullRequest": pull}}

        with (
            patch.object(notice.pull_reads, "pull_author_state", AUTHOR_STATE),
            patch.object(notice.github, "graphql", side_effect=graphql),
        ):
            code, include = self.names("repo#1")
        self.assertEqual(code, 0)
        self.assertEqual(
            [(item["number"], item["head_sha"]) for item in include], [(1, OTHER)]
        )
        self.assertEqual(len(queries), 1)

    def test_a_negative_page_is_a_usage_error(self) -> None:
        """Pages count from zero."""
        code, out, _ = run(
            [
                "list",
                "--org",
                ORG,
                "--bot-authors-file",
                str(self.authors),
                "--pull-requests",
                "repo#1",
                "--page",
                "-1",
            ]
        )
        self.assertEqual((code, out, self.gh.read), (2, "", []))

    def test_a_malformed_list_is_an_error(self) -> None:
        """A list that does not parse, or names another owner, fails."""
        for names in ("not a pull", "other/repo#1"):
            with self.subTest(names=names):
                self.assertEqual(self.names(names)[0], 1)


class PostTest(Case):
    """``post``: the five conditions, the one write and its recovery."""

    def setUp(self) -> None:
        """An open Code Monkey pull request, at HEAD, owed nothing yet."""
        super().setUp()
        self.gh.states[7] = live()
        self.summary = self.root / "summary.md"

    def post(self, reason: str = "no_review", **overrides: str) -> tuple[int, str]:
        """Run ``post``; return the exit code and stdout."""
        options = {
            "--repository": REPO,
            "--number": "7",
            "--head": HEAD,
            "--reason": reason,
            "--bot-slug": APP,
            "--bot-authors-file": str(self.authors),
            "--run-id": str(RUN),
            "--run-url": RUN_URL,
            "--workflow-repository": WORKFLOW,
            "--summary": str(self.summary),
        }
        options.update(overrides)
        argv = ["post", *[part for pair in options.items() for part in pair]]
        code, out, _ = run(argv)
        return code, out

    def test_posts_one_notice_ending_with_the_marker(self) -> None:
        """All five hold: one comment, recorded on stdout and in the summary."""
        code, out = self.post()
        self.assertEqual(code, 0)
        (write,) = self.gh.writes
        self.assertEqual(write["endpoint"], f"repos/{REPO}/issues/7/comments")
        self.assertTrue(
            write["body"].endswith(
                f"<!-- antagonistic-notice reason=no_review head={HEAD} run={RUN} -->"
            )
        )
        self.assertIn(f"`{HEAD[:12]}`", write["body"])
        self.assertIn(WORKFLOW, write["body"])
        for word in (TITLE, BODY, MONKEY):
            self.assertNotIn(word, write["body"])
        self.assertIn("posted", out)
        self.assertEqual(self.summary.read_text(encoding="utf-8"), out)

    def test_each_condition_stops_the_write_on_its_own(self) -> None:
        """Closed, human, moved, reviewed, already noticed: no write, exit 0."""
        cases: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
            ("closed", {"state": "MERGED"}, {}),
            ("human", {"author": {"login": "someone", "is_bot": False}}, {}),
            ("moved", {"head_sha": OTHER}, {}),
            ("reviewed", {}, {"reviews": {7: [review()]}}),
            ("noticed", {}, {"comments": [comment(run=RUN - 1)]}),
        ]
        for name, state, held in cases:
            with self.subTest(case=name):
                self.gh.states[7] = live(**state)
                self.gh.reviews = held.get("reviews", {})
                self.gh.comments = list(held.get("comments", []))
                code, out = self.post()
                self.assertEqual((code, self.gh.writes), (0, []))
                self.assertIn("no notice", out)

    def test_a_different_head_or_reason_posts_again(self) -> None:
        """A notice is one per head and reason."""
        for earlier in (comment(head=OTHER), comment(reason="cap")):
            with self.subTest(earlier=earlier["body"][-80:]):
                self.gh.comments = [earlier]
                self.gh.writes = []
                self.assertEqual(self.post()[0], 0)
                self.assertEqual(len(self.gh.writes), 1)

    def test_a_marker_by_anyone_else_suppresses_nothing(self) -> None:
        """A human named like the App, or another bot, cannot hold a notice back."""
        for forged in (
            comment(login=APP, kind="User"),
            comment(login=f"{APP}[bot]", kind="User"),
            comment(login="evil[bot]"),
        ):
            with self.subTest(forged=forged["user"]):
                self.gh.comments = [forged]
                self.gh.writes = []
                self.assertEqual(self.post()[0], 0)
                self.assertEqual(len(self.gh.writes), 1)

    def test_a_transient_failure_that_landed_is_posted_and_not_resent(self) -> None:
        """The marker naming this run reads back, so the write stands."""
        self.gh.write_status = 502
        self.gh.lands_anyway = True
        code, out = self.post()
        self.assertEqual((code, len(self.gh.writes)), (0, 1))
        self.assertIn("posted", out)

    def test_a_transient_failure_that_did_not_land_fails(self) -> None:
        """Nothing reads back: exit 1, and still one write."""
        self.gh.write_status = 502
        code, _ = self.post()
        self.assertEqual((code, len(self.gh.writes)), (1, 1))

    def test_another_runs_notice_does_not_stand_for_this_write(self) -> None:
        """Recovery reads back this run's marker, not one a concurrent run posted."""
        self.gh.write_status = 502
        self.gh.late = [comment(run=RUN - 1)]
        code, _ = self.post()
        self.assertEqual((code, len(self.gh.writes)), (1, 1))

    def test_a_github_read_failure_exits_one(self) -> None:
        """A read that fails is a failure, not a reason to skip."""
        self.gh.unreadable.add("comments 7")
        self.assertEqual(self.post(), (1, ""))
        self.assertEqual(self.gh.writes, [])

    def test_usage_errors(self) -> None:
        """An unknown reason, a bad head or a foreign run URL is refused."""
        for option, value in (
            ("--reason", "approved"),
            ("--reason", "closed"),
            ("--head", HEAD[:12]),
            ("--run-url", "https://github.com/other/repo/actions/runs/42"),
            ("--run-url", f"{RUN_URL}/attempts/1"),
        ):
            with self.subTest(option=option, value=value):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(self.post(**{option: value})[0], 2)
        self.assertEqual(self.gh.writes, [])

    def test_the_write_refuses_any_other_endpoint(self) -> None:
        """The one write function posts issue comments alone."""
        for endpoint in (
            f"repos/{REPO}/pulls/7/reviews",
            f"repos/{REPO}/issues/7/labels",
            f"repos/{REPO}/issues/7/comments/../labels",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                notice.post(endpoint, {"body": "x"})
        self.assertEqual(self.gh.writes, [])


class MarkerTest(unittest.TestCase):
    """A notice's marker is mistaken for no other marker, and none for it."""

    def test_no_other_reader_takes_a_notice_for_its_own(self) -> None:
        """Review, hand-back and Code Monkey footer readers ignore it."""
        body = text.notice_body(
            "no_review", HEAD, f"{APP}[bot]", RUN_URL, RUN, WORKFLOW
        )
        self.assertIsNone(recovery.ANTAGONISTIC_RE.search(body))
        self.assertIsNone(handback.MARKER_RE.search(body))
        self.assertIsNone(handback.ANSWERED_RE.search(body))
        self.assertIsNotNone(text.MARKER_RE.search(body))

    def test_a_review_marker_is_no_notice(self) -> None:
        """This bot's review and hand-back markers are not notices."""
        for marker in (
            comment_body.review_marker(1, HEAD, RUN),
            comment_body.handback_marker(1, HEAD, RUN),
        ):
            with self.subTest(marker=marker):
                self.assertIsNone(text.MARKER_RE.search(marker))


if __name__ == "__main__":
    unittest.main()
