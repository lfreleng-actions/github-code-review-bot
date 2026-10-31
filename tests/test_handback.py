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
) -> dict[str, Any]:
    """One review as the REST listing returns it."""
    text = "Findings.\n\n---\nfooter"
    if marker:
        text += "\n\n" + body.handback_marker(1, head, run)
    return {"user": {"login": login, "type": kind}, "commit_id": head, "body": text}


class GitHubDouble:
    """Serve each pull request's reviews from a dict, recording the reads."""

    def __init__(self, reviews: dict[int, list[dict[str, Any]]]) -> None:
        """Remember what each pull request's listing returns."""
        self.reviews = reviews
        self.reads: list[str] = []

    def __call__(self, endpoint: str) -> list[dict[str, Any]]:
        """Answer ``api_list`` for ``repos/org/repo/pulls/N/reviews``."""
        self.reads.append(endpoint)
        number = int(endpoint.split("/pulls/")[1].split("/")[0])
        return self.reviews.get(number, [])


class HandbacksTest(unittest.TestCase):
    """``handbacks`` keeps a nominee the selection and GitHub both confirm."""

    def run_handbacks(
        self,
        report: dict[str, Any],
        chosen: dict[str, Any],
        reviews: dict[int, list[dict[str, Any]]],
    ) -> tuple[list[str], GitHubDouble]:
        """Run with the GitHub double; return the list and the reads it made."""
        double = GitHubDouble(reviews)
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


class MainTest(unittest.TestCase):
    """The CLI prints exactly two output lines."""

    def run_list(
        self, report: object, chosen: object, org: str = "org", run: str = str(RUN)
    ) -> str:
        """Run ``list`` over files in a temp tree; return stdout."""
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            selection_path = Path(tmp) / "selection.json"
            selection_path.write_text(json.dumps(chosen), encoding="utf-8")
            out = io.StringIO()
            with (
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(io.StringIO()),
                patch.object(
                    handback.github,
                    "api_list",
                    side_effect=GitHubDouble({1: [review()]}),
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
