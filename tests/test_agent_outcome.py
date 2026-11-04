# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Reading every review leg's session outcome from one listing of the run's jobs."""

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
outcome = import_module("agent_outcome")
github = import_module("bot_github")

KEY = "repo-15"


def job(name: str, *steps: tuple[str, Any]) -> dict[str, Any]:
    """A jobs API entry with named steps and their conclusions."""
    return {
        "name": name,
        "steps": [{"name": step, "conclusion": result} for step, result in steps],
    }


def leg(
    conclusion: Any, name: str = f"Review ({KEY})", upload: Any = "success"
) -> dict[str, Any]:
    """A review leg whose agent step ended with ``conclusion``."""
    return job(
        name,
        ("Prepare packet and prompt", "success"),
        (outcome.AGENT_STEP, conclusion),
        (outcome.UPLOAD_STEP, upload),
    )


def selection(*keys: Any) -> dict[str, Any]:
    """A selection naming ``keys``."""
    return {"schema": 1, "pull_requests": [{"key": key} for key in keys]}


def run_main(
    pages: Any = None,
    error: Exception | None = None,
    chosen: Any = None,
) -> tuple[str, str, int]:
    """Run the CLI against patched gh output; return stdout, stderr and calls."""
    out, err = io.StringIO(), io.StringIO()
    side = error if error is not None else None
    value = json.dumps(pages) if error is None else None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "selection.json"
        path.write_text(
            json.dumps(selection(KEY, "repo-1") if chosen is None else chosen)
        )
        with (
            patch.object(github, "run_gh", side_effect=side, return_value=value) as gh,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            outcome.main(
                ["--repository", "org/bot", "--run-id", "9", "--selection", str(path)]
            )
    for call in gh.call_args_list:
        assert call.args[0][1] == (
            "repos/org/bot/actions/runs/9/jobs?filter=latest&per_page=100"
        )
    return out.getvalue(), err.getvalue(), gh.call_count


def printed(out: str) -> dict[str, str]:
    """The outcomes map from the CLI's one output line."""
    name, _, value = out.rstrip("\n").partition("=")
    assert name == "outcomes" and "\n" not in value
    return json.loads(value)


class ConclusionTest(unittest.TestCase):
    """The leg is found by name and only a success reads as success."""

    def test_the_leg_matches_alone_or_under_a_caller_prefix(self) -> None:
        """A reusable workflow's jobs carry the caller's job name first."""
        self.assertTrue(outcome.is_leg(f"Review ({KEY})", KEY))
        self.assertTrue(outcome.is_leg(f"code-review / Review ({KEY})", KEY))
        self.assertFalse(outcome.is_leg("Review (repo-1)", KEY))
        self.assertFalse(outcome.is_leg(f"Apply ({KEY})", KEY))
        self.assertFalse(outcome.is_leg(None, KEY))

    def test_a_successful_agent_step_is_success(self) -> None:
        """Other legs and other jobs do not matter."""
        jobs = [leg("failure", "Review (repo-1)"), leg("success"), job("Report")]
        self.assertEqual(outcome.conclusion(jobs, KEY), "success")

    def test_any_other_ending_is_reported_as_it_is(self) -> None:
        """A failure or a timeout leaves the step failed; a cancel says so."""
        for result in ("failure", "cancelled", "skipped"):
            with self.subTest(result):
                self.assertEqual(outcome.conclusion([leg(result)], KEY), result)

    def test_a_success_counts_only_after_its_upload_succeeded(self) -> None:
        """A failed upload can leave an earlier attempt's artifact in place."""
        for upload in ("failure", "cancelled", "skipped", None):
            with self.subTest(upload=upload):
                jobs = [leg("success", upload=upload)]
                self.assertEqual(outcome.conclusion(jobs, KEY), "unknown")
        missing = job(f"Review ({KEY})", (outcome.AGENT_STEP, "success"))
        self.assertEqual(outcome.conclusion([missing], KEY), "unknown")
        self.assertEqual(
            outcome.conclusion([leg("failure", upload="success")], KEY), "failure"
        )

    def test_doubt_is_unknown(self) -> None:
        """No leg, no agent step, no conclusion or an odd one is not success."""
        cases: dict[str, list[dict[str, Any]]] = {
            "no leg": [job("Report")],
            "no agent step": [job(f"Review ({KEY})", ("Other", "success"))],
            "no conclusion": [leg(None)],
            "odd conclusion": [leg("success\nfoo=bar")],
            "two agent steps": [
                job(
                    f"Review ({KEY})",
                    (outcome.AGENT_STEP, "success"),
                    (outcome.AGENT_STEP, "success"),
                )
            ],
        }
        for name, jobs in cases.items():
            with self.subTest(name):
                self.assertEqual(outcome.conclusion(jobs, KEY), "unknown")

    def test_every_matching_leg_must_succeed(self) -> None:
        """Two callers in one run naming the same key must agree."""
        jobs = [
            leg("success", f"a / Review ({KEY})"),
            leg("failure", f"b / Review ({KEY})"),
        ]
        self.assertEqual(outcome.conclusion(jobs, KEY), "failure")


class MainTest(unittest.TestCase):
    """The CLI lists the jobs once, prints one map and never fails the step."""

    def test_one_listing_answers_every_selected_key(self) -> None:
        """The legs can sit on any page; keys outside the selection are left out."""
        pages = [
            {"jobs": [job("Select"), leg("failure", "Review (repo-1)")]},
            {"jobs": [leg("success"), leg("success", "Review (repo-2)")]},
        ]
        out, err, calls = run_main(pages)
        self.assertEqual(printed(out), {KEY: "success", "repo-1": "failure"})
        self.assertEqual(err, "")
        self.assertEqual(calls, 1)

    def test_a_github_failure_reads_as_unknown(self) -> None:
        """An unreachable API costs a retry of each head, not its verdict."""
        out, err, _ = run_main(error=github.GitHubError("HTTP 502"))
        self.assertEqual(printed(out), {KEY: "unknown", "repo-1": "unknown"})
        self.assertIn("treating as unknown", err)

    def test_an_unexpected_reply_reads_as_unknown(self) -> None:
        """A reply that is not pages of jobs is not a success."""
        replies: list[Any] = [{"jobs": []}, [{"total_count": 1}], [[leg("success")]]]
        for pages in replies:
            with self.subTest(pages=pages):
                out, _, _ = run_main(pages)
                self.assertEqual(printed(out), {KEY: "unknown", "repo-1": "unknown"})

    def test_a_malformed_selection_prints_an_empty_map(self) -> None:
        """Each applier then reads its key as unknown; nothing is listed."""
        malformed: list[Any] = [
            [KEY],
            {"pull_requests": {}},
            selection("a b"),
            selection(7),
        ]
        for chosen in malformed:
            with self.subTest(chosen=chosen):
                out, err, calls = run_main([{"jobs": [leg("success")]}], chosen=chosen)
                self.assertEqual(printed(out), {})
                self.assertIn("treating as unknown", err)
                self.assertEqual(calls, 0)


if __name__ == "__main__":
    unittest.main()
