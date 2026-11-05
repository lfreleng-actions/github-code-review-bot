# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The hand-back list: results nominate, the selection and GitHub confirm."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
handback = import_module("handback")
body = import_module("comment_body")

HEAD = "a" * 40
OLD = "b" * 40
APP = "lf-releng-antagonistic-review-bot"
MONKEY = "lf-releng-code-monkey-bot"
RUN = 42
OTHER_RUN = 41
REVIEW = 501
ISSUE = 31


def result(number: int, **overrides: Any) -> dict[str, Any]:
    """A posted antagonistic result that claims a hand-back."""
    data: dict[str, Any] = {
        "repository": "org/repo",
        "number": number,
        "verdict": "commented",
        "handback": True,
    }
    data.update(overrides)
    return data


def entry(number: int, **overrides: Any) -> dict[str, Any]:
    """A selected Code Monkey pull request at HEAD."""
    data: dict[str, Any] = {
        "repository": "org/repo",
        "number": number,
        "author": MONKEY,
        "head_sha": HEAD,
    }
    data.update(overrides)
    return data


def selection(*entries: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """The verified selection of an antagonistic run."""
    data: dict[str, Any] = {
        "bot_login": f"{APP}[bot]",
        "handback_authors": [MONKEY],
        "pull_requests": list(entries),
    }
    data.update(overrides)
    return data


def review(
    head: str = HEAD,
    *,
    marker: bool = True,
    login: str = f"{APP}[bot]",
    kind: str = "Bot",
    run: int = RUN,
    review_id: int = REVIEW,
) -> dict[str, Any]:
    """One review as the REST listing returns it."""
    text = "Findings.\n\n---\nfooter"
    if marker:
        text += "\n\n" + body.handback_marker(1, head, run)
    return {
        "id": review_id,
        "user": {"login": login, "type": kind},
        "commit_id": head,
        "body": text,
    }


def pull(
    head: str = HEAD, branch: str = f"code-monkey/issue-{ISSUE}"
) -> dict[str, Any]:
    """A pull request as the REST read returns it."""
    return {"head": {"sha": head, "ref": branch}}


def footer(
    review_id: int = REVIEW,
    *,
    login: str = f"{MONKEY}[bot]",
    kind: str = "Bot",
    trailer: str = "",
) -> dict[str, Any]:
    """A Code Monkey round comment on its issue whose footer names a review."""
    text = (
        "Round summary.\n\n<!-- code-monkey https://run proposed round=3 pr=1"
        f" since=2026-10-07T00:00:00Z antagonistic={review_id} -->{trailer}"
    )
    return {"user": {"login": login, "type": kind}, "body": text}


class GitHubDouble:
    """Serve each pull request's reviews from a dict, recording the reads."""

    def __init__(
        self,
        reviews: dict[int, list[dict[str, Any]]],
        failures: dict[int, int] | None = None,
        *,
        pulls: dict[int, dict[str, Any]] | None = None,
        comments: list[dict[str, Any]] | None = None,
        broken: tuple[str, ...] = (),
    ) -> None:
        """Remember each listing, and the HTTP status of each that fails.

        ``pulls`` and ``comments`` answer the delivery check: each pull
        request defaults to one at HEAD on a Code Monkey issue branch, and
        the issue to no comments. An endpoint ending with an entry of
        ``broken`` fails with HTTP 502.
        """
        self.reviews = reviews
        self.failures = failures or {}
        self.pulls = pulls or {}
        self.comments = comments or []
        self.broken = broken
        self.reads: list[str] = []

    def fail_if_broken(self, endpoint: str) -> None:
        """Raise the 502 a broken endpoint answers with."""
        if endpoint.endswith(self.broken):
            raise handback.github.GitHubError("HTTP 502: ::read failed", 502)

    def __call__(self, endpoint: str) -> list[dict[str, Any]]:
        """Answer ``api_list`` for reviews and the issue's comments."""
        self.reads.append(endpoint)
        self.fail_if_broken(endpoint)
        if "/issues/" in endpoint:
            return self.comments
        number = int(endpoint.split("/pulls/")[1].split("/")[0])
        if number in self.failures:
            status = self.failures[number]
            raise handback.github.GitHubError(f"HTTP {status}: ::read failed", status)
        return self.reviews.get(number, [])

    def read_pull(self, endpoint: str) -> dict[str, Any]:
        """Answer ``api_object`` for ``repos/org/repo/pulls/N``."""
        self.reads.append(endpoint)
        self.fail_if_broken(endpoint)
        return self.pulls.get(int(endpoint.rsplit("/", 1)[1]), pull())


class HandbacksTest(unittest.TestCase):
    """``handbacks`` keeps a nominee the selection and GitHub both confirm."""

    def run_handbacks(
        self,
        report: dict[str, Any],
        chosen: dict[str, Any],
        reviews: dict[int, list[dict[str, Any]]],
        failures: dict[int, int] | None = None,
    ) -> tuple[list[str], GitHubDouble]:
        """Run with the GitHub double; return the list and the reads it made."""
        double = GitHubDouble(reviews, failures)
        with patch.object(handback.github, "api_list", side_effect=double):
            return handback.handbacks(report, chosen, "org", RUN), double

    def test_a_marked_review_of_the_selected_head_confirms(self) -> None:
        """The applier's marker on this App's review of the head is the proof."""
        refs, _ = self.run_handbacks(
            {"results": [result(1)]}, selection(entry(1)), {1: [review()]}
        )
        self.assertEqual(refs, ["org/repo#1"])

    def test_a_forged_result_dispatches_nothing(self) -> None:
        """A result naming a pull request the run never marked is refused."""
        cases: list[tuple[str, dict[str, Any], dict[int, list[dict[str, Any]]]]] = [
            ("not selected", selection(), {1: [review()]}),
            ("no review", selection(entry(1)), {1: []}),
            ("review without marker", selection(entry(1)), {1: [review(marker=False)]}),
            ("marker on an old head", selection(entry(1)), {1: [review(OLD)]}),
            (
                "human named like the App",
                selection(entry(1)),
                {1: [review(kind="User")]},
            ),
            ("another bot", selection(entry(1)), {1: [review(login="evil[bot]")]}),
            (
                "author takes no hand-back",
                selection(entry(1, author="Copilot")),
                {1: [review()]},
            ),
            (
                "selection has another head",
                selection(entry(1, head_sha=OLD)),
                {1: [review()]},
            ),
            ("no App in the run", selection(entry(1), bot_login=None), {1: [review()]}),
            (
                "marker from another run",
                selection(entry(1)),
                {1: [review(run=OTHER_RUN)]},
            ),
        ]
        for name, chosen, reviews in cases:
            with self.subTest(case=name):
                refs, _ = self.run_handbacks({"results": [result(1)]}, chosen, reviews)
                self.assertEqual(refs, [])

    def test_a_marker_quoted_mid_body_does_not_count(self) -> None:
        """The marker must close the review; agent text before the footer cannot."""
        marked = review(marker=False)
        marked["body"] = (
            body.handback_marker(1, HEAD, RUN) + "\n\nagent text\n\n---\nfooter"
        )
        refs, _ = self.run_handbacks(
            {"results": [result(1)]}, selection(entry(1)), {1: [marked]}
        )
        self.assertEqual(refs, [])

    def test_a_run_dispatches_on_its_own_review_alone(self) -> None:
        """Two runs can hold one head. The run whose applier refused it as
        already reviewed finds the other run's marker, not its own, so a
        result it forges for that head dispatches nothing; the run that
        posted still dispatches.
        """
        report = {"results": [result(1)]}
        posted = {1: [review(run=OTHER_RUN)]}
        refs, _ = self.run_handbacks(report, selection(entry(1)), posted)
        self.assertEqual(refs, [])
        double = GitHubDouble(posted)
        with patch.object(handback.github, "api_list", side_effect=double):
            refs = handback.handbacks(report, selection(entry(1)), "org", OTHER_RUN)
        self.assertEqual(refs, ["org/repo#1"])

    def test_unclaimed_or_malformed_results_cost_no_read(self) -> None:
        """Only a posted result claiming a hand-back in the owner is checked."""
        report = {
            "results": [
                "text",
                result(1, verdict="would-comment"),
                result(2, handback=False),
                result(3, repository="elsewhere/repo"),
                result(4, repository="org/repo\nx=1"),
                result(0),
                result(True),
            ]
        }
        refs, double = self.run_handbacks(report, selection(entry(1)), {})
        self.assertEqual(refs, [])
        self.assertEqual(double.reads, [])

    def test_the_whole_list_survives(self) -> None:
        """No cap: every confirmed hand-back is dispatched, in report order."""
        numbers = range(1, 81)
        refs, _ = self.run_handbacks(
            {"results": [result(n) for n in numbers]},
            selection(*(entry(n) for n in numbers)),
            {n: [review()] for n in numbers},
        )
        self.assertEqual(len(refs), 80)

    def test_an_unreadable_candidate_is_skipped(self) -> None:
        """A 404 (a private repository) or a 5xx on one candidate drops that
        candidate with a warning; the others are still read and confirmed.
        """
        numbers = (1, 2, 3)
        for status in (404, 502):
            with (
                self.subTest(status=status),
                contextlib.redirect_stderr(io.StringIO()) as err,
            ):
                refs, double = self.run_handbacks(
                    {"results": [result(n) for n in numbers]},
                    selection(*(entry(n) for n in numbers)),
                    {n: [review()] for n in numbers},
                    {1: status},
                )
            self.assertEqual(refs, ["org/repo#2", "org/repo#3"])
            self.assertEqual(len(double.reads), 3)
            warning = err.getvalue()
            self.assertTrue(warning.startswith("::warning::handback: org/repo#1"))
            self.assertNotIn("::read", warning)


class DeliveriesTest(unittest.TestCase):
    """Each confirmed hand-back is recorded with its review, and dropped from
    the dispatch when it needs no delivery.
    """

    def records(
        self, double: GitHubDouble, numbers: tuple[int, ...] = (1,)
    ) -> list[dict[str, Any]]:
        """Confirm and check the hand-backs of ``numbers`` against the double."""
        with (
            patch.object(handback.github, "api_list", side_effect=double),
            patch.object(handback.github, "api_object", side_effect=double.read_pull),
        ):
            confirmed = handback.handbacks(
                {"results": [result(n) for n in numbers]},
                selection(*(entry(n) for n in numbers)),
                "org",
                RUN,
            )
            return handback.deliveries(confirmed)

    @unittest.expectedFailure
    def test_a_pending_delivery_names_its_review(self) -> None:
        """The posted review's id is the delivery's identity."""
        double = GitHubDouble({1: [review()]})
        self.assertEqual(
            self.records(double),
            [{"pull": "org/repo#1", "review": REVIEW, "state": "pending"}],
        )
        self.assertIn(
            f"repos/org/repo/issues/{ISSUE}/comments?per_page=100", double.reads
        )

    @unittest.expectedFailure
    def test_a_moved_head_is_skipped(self) -> None:
        """A round already ran, or someone pushed: the review is spent."""
        double = GitHubDouble({1: [review()]}, pulls={1: pull(OLD)})
        with contextlib.redirect_stderr(io.StringIO()):
            records = self.records(double)
        self.assertEqual(
            records,
            [
                {
                    "pull": "org/repo#1",
                    "review": REVIEW,
                    "state": "skipped",
                    "reason": f"the head moved from {HEAD[:12]} to {OLD[:12]}",
                }
            ],
        )
        self.assertFalse(any("/issues/" in read for read in double.reads))

    @unittest.expectedFailure
    def test_an_answered_review_is_skipped(self) -> None:
        """Code Monkey's round footer naming this review means it went."""
        double = GitHubDouble({1: [review()]}, comments=[footer(trailer="\n")])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            records = self.records(double)
        self.assertEqual(
            records,
            [
                {
                    "pull": "org/repo#1",
                    "review": REVIEW,
                    "state": "skipped",
                    "reason": f"issue #{ISSUE} already answers review {REVIEW}",
                }
            ],
        )
        self.assertIn("::notice::handback: org/repo#1 skipped", err.getvalue())

    @unittest.expectedFailure
    def test_other_footers_do_not_answer(self) -> None:
        """Another review, another author, or a footer mid-body: still pending."""
        quoted = footer()
        quoted["body"] += "\n\nlater text"
        cases = {
            "another review": footer(REVIEW + 1),
            "a human": footer(kind="User"),
            "another bot": footer(login="evil[bot]"),
            "mid-body": quoted,
        }
        for name, comment in cases.items():
            with self.subTest(case=name):
                double = GitHubDouble({1: [review()]}, comments=[comment])
                self.assertEqual(self.records(double)[0]["state"], "pending")

    @unittest.expectedFailure
    def test_a_branch_without_an_issue_reads_no_comments(self) -> None:
        """No Code Monkey issue to read: the hand-back stays pending."""
        double = GitHubDouble({1: [review()]}, pulls={1: pull(branch="feature")})
        self.assertEqual(self.records(double)[0]["state"], "pending")
        self.assertFalse(any("/issues/" in read for read in double.reads))

    @unittest.expectedFailure
    def test_a_failed_check_keeps_the_pull_request(self) -> None:
        """A duplicate dispatch costs a refused session; a dropped one a revision."""
        for broken in ("/pulls/1", "/comments?per_page=100"):
            with (
                self.subTest(broken=broken),
                contextlib.redirect_stderr(io.StringIO()) as err,
            ):
                double = GitHubDouble({1: [review()]}, broken=(broken,))
                records = self.records(double)
            self.assertEqual(records[0]["state"], "pending")
            warning = err.getvalue()
            self.assertTrue(warning.startswith("::warning::handback: org/repo#1 kept"))
            self.assertNotIn("::read", warning)


class MainTest(unittest.TestCase):
    """The CLI prints exactly two output lines."""

    def run_list(
        self,
        report: object,
        chosen: object,
        org: str = "org",
        run: str = str(RUN),
        failures: dict[int, int] | None = None,
    ) -> str:
        """Run ``list`` over files in a temp tree; return stdout."""
        double = GitHubDouble({1: [review()]}, failures)
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            selection_path = Path(tmp) / "selection.json"
            selection_path.write_text(json.dumps(chosen), encoding="utf-8")
            out = io.StringIO()
            with (
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(io.StringIO()),
                patch.object(handback.github, "api_list", side_effect=double),
                patch.object(
                    handback.github, "api_object", side_effect=double.read_pull
                ),
            ):
                handback.main(
                    [
                        "list",
                        "--report",
                        str(report_path),
                        "--selection",
                        str(selection_path),
                        "--org",
                        org,
                        "--run-id",
                        run,
                    ]
                )
        return out.getvalue()

    def test_outputs(self) -> None:
        """One space-separated list and its count, an empty list included."""
        self.assertEqual(
            self.run_list({"results": [result(1)]}, selection(entry(1))),
            "pulls=org/repo#1\ncount=1\n",
        )
        self.assertEqual(
            self.run_list({"results": []}, selection()), "pulls=\ncount=0\n"
        )

    def test_a_failed_read_lists_nothing_and_succeeds(self) -> None:
        """A 404 or a 5xx on the only candidate gives an empty list, not exit 1."""
        for status in (404, 503):
            with self.subTest(status=status):
                self.assertEqual(
                    self.run_list(
                        {"results": [result(1)]},
                        selection(entry(1)),
                        failures={1: status},
                    ),
                    "pulls=\ncount=0\n",
                )

    @unittest.expectedFailure
    def test_records_carry_every_confirmed_hand_back(self) -> None:
        """Skipped hand-backs are recorded and left out of the dispatch list."""
        self.assertEqual(
            self.run_list({"results": [result(1)]}, selection(entry(1))).splitlines()[
                2
            ],
            f'records=[{{"pull":"org/repo#1","review":{REVIEW},"state":"pending"}}]',
        )
        with patch.object(GitHubDouble, "read_pull", return_value=pull(OLD)):
            out = self.run_list({"results": [result(1)]}, selection(entry(1)))
        pulls, count, records = out.splitlines()
        self.assertEqual((pulls, count), ("pulls=", "count=0"))
        self.assertEqual(
            json.loads(records.removeprefix("records="))[0]["state"], "skipped"
        )

    def test_bad_inputs_fail(self) -> None:
        """Files that are not objects, or an owner or run that is malformed, exit."""
        with self.assertRaises(SystemExit) as caught:
            self.run_list(["not", "an", "object"], selection())
        self.assertEqual(caught.exception.code, 1)
        with self.assertRaises(SystemExit) as caught:
            self.run_list({"results": []}, ["nope"])
        self.assertEqual(caught.exception.code, 1)
        for org, run in (("org\nx", str(RUN)), ("org", "0"), ("org", "x")):
            with (
                self.subTest(org=org, run=run),
                self.assertRaises(SystemExit) as caught,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.run_list({"results": []}, selection(), org=org, run=run)
            self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
