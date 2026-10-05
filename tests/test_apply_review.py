# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The check and apply commands, against patched GitHub reads and writes."""

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
apply_review = import_module("apply_review")
policy = import_module("review_policy")

HEAD = "a" * 40
MOVED = "b" * 40
REPO = "lfreleng-actions/repo"
BOT = "lf-releng-issues-triage-bot[bot]"
RUN_URL = "https://github.com/lfreleng-actions/bot/actions/runs/1"


def entry() -> dict[str, Any]:
    """A per-PR selection entry with one documentation change."""
    return {
        "key": "repo-157",
        "repository": REPO,
        "number": 157,
        "head_sha": HEAD,
        "url": "https://github.com/lfreleng-actions/repo/pull/157",
        "title": "Docs: Fix a typo",
        "files": [
            {
                "path": "README.md",
                "status": "modified",
                "patch": "@@ -1 +1 @@\n-teh\n+the\n",
                "patch_truncated": False,
            }
        ],
    }


def selection(**overrides: Any) -> dict[str, Any]:
    """A selection.json object holding the entry."""
    data: dict[str, Any] = {
        "schema": 1,
        "dry_run": False,
        "model": "claude-opus-5.5",
        "approve_tiers": ["low-risk", "trivial"],
        "bot_login": BOT,
        "pull_requests": [entry()],
    }
    data.update(overrides)
    return data


def verdict_text(**overrides: Any) -> str:
    """An agent message whose final block is a valid trivial verdict."""
    data: dict[str, Any] = {
        "schema": 1,
        "repository": REPO,
        "pull_request": 157,
        "head_sha": HEAD,
        "tier": "trivial",
        "summary": "Fixes a typo. Thanks @someone!",
        "findings": [],
        "injection_attempts": [],
    }
    data.update(overrides)
    return f"All done.\n\n```json\n{json.dumps(data)}\n```\n"


def live_state(**overrides: Any) -> dict[str, Any]:
    """What pull_state returns for a pull request still fit to approve."""
    state: dict[str, Any] = {
        "state": "OPEN",
        "is_draft": False,
        "head_sha": HEAD,
        "ci": "success",
        "changes_requested": False,
        "approved_by": [],
        "review_requests": [],
        "unresolved_copilot_threads": 0,
    }
    state.update(overrides)
    return state


def run(argv: list[str]) -> str:
    """Run the CLI and return its stdout."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        apply_review.main(argv)
    return out.getvalue()


class CheckCommandTests(unittest.TestCase):
    """The offline check writes check.json and the workflow outputs."""

    def setUp(self) -> None:
        """Lay out a selection and a session directory in a temp tree."""
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.session = self.root / "session"
        self.session.mkdir()
        (self.root / "selection.json").write_text(json.dumps(selection()))

    def tearDown(self) -> None:
        """Remove the temp tree."""
        self.tmp.cleanup()

    def check_argv(self, *extra: str) -> list[str]:
        """Arguments for the check command against the temp tree."""
        return [
            "check",
            "--selection",
            str(self.root / "selection.json"),
            "--key",
            "repo-157",
            "--session-dir",
            str(self.session),
            "--approve-tiers",
            "trivial,low-risk",
            "--output",
            str(self.root / "out" / "check.json"),
            "--summary",
            str(self.root / "out" / "check-summary.md"),
            *extra,
        ]

    def test_valid_verdict_is_approvable(self) -> None:
        """A corroborated trivial verdict yields approvable outputs and files."""
        (self.session / "session-summary.md").write_text(verdict_text())
        (self.session / "usage.json").write_text(
            json.dumps({"totalPremiumRequestCost": 2, "totalApiDurationMs": 5000})
        )
        out = run(self.check_argv())
        self.assertEqual(out, "approvable=true\ntier=trivial\n")
        check = json.loads((self.root / "out" / "check.json").read_text())
        self.assertEqual(check["agent_tier"], "trivial")
        self.assertEqual(check["summary"], "Fixes a typo. Thanks &#64;someone!")
        self.assertEqual(check["premium_requests"], 2.0)
        self.assertEqual(check["agent_seconds"], 5)
        summary = (self.root / "out" / "check-summary.md").read_text()
        self.assertIn("repo#157", summary)
        self.assertIn("Approvable: yes", summary)

    def test_missing_summary_and_failure_flag_refuse(self) -> None:
        """No session output, or an upstream failure, is a typed refusal."""
        out = run(self.check_argv())
        self.assertEqual(out, "approvable=false\ntier=needs-human\n")
        check = json.loads((self.root / "out" / "check.json").read_text())
        self.assertIsNone(check["agent_tier"])
        self.assertEqual(check["reasons"], ["no session summary"])
        run(self.check_argv("--failure", "artefact rejected"))
        check = json.loads((self.root / "out" / "check.json").read_text())
        self.assertEqual(check["reasons"], ["artefact rejected"])

    def test_malformed_verdict_refuses_with_the_parse_error(self) -> None:
        """A rejected block names why rather than failing the step."""
        (self.session / "session-summary.md").write_text("no block here")
        out = run(self.check_argv())
        self.assertEqual(out, "approvable=false\ntier=needs-human\n")
        check = json.loads((self.root / "out" / "check.json").read_text())
        self.assertEqual(check["reasons"], ["the final fenced block must be json"])

    def test_unknown_key_or_tier_fails_the_step(self) -> None:
        """Operator mistakes are operational failures, exit 1."""
        argv = self.check_argv()
        argv[argv.index("--key") + 1] = "other-1"
        with self.assertRaises(SystemExit) as caught:
            run(argv)
        self.assertEqual(caught.exception.code, 1)
        argv = self.check_argv()
        argv[argv.index("--approve-tiers") + 1] = "everything"
        with self.assertRaises(SystemExit):
            run(argv)


class ApplyCommandTests(unittest.TestCase):
    """The live gates and the single approval write."""

    def setUp(self) -> None:
        """Write an approvable check.json and the selection."""
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "selection.json").write_text(json.dumps(selection()))
        verdict = policy.extract_verdict(verdict_text())
        self.check = policy.check(
            entry(), verdict, approve_tiers=frozenset({"trivial", "low-risk"})
        )
        self.write_check()

    def tearDown(self) -> None:
        """Remove the temp tree."""
        self.tmp.cleanup()

    def write_check(self) -> None:
        """Persist the current check object."""
        (self.root / "check.json").write_text(json.dumps(self.check))

    def apply(self, *extra: str) -> dict[str, Any]:
        """Run apply and return result.json."""
        run(
            [
                "apply",
                "--check",
                str(self.root / "check.json"),
                "--selection",
                str(self.root / "selection.json"),
                "--output",
                str(self.root / "out" / "result.json"),
                "--run-url",
                RUN_URL,
                "--run-attempt",
                "2",
                *extra,
            ]
        )
        return json.loads((self.root / "out" / "result.json").read_text())

    def test_not_approvable_makes_no_network_call(self) -> None:
        """A refused check never reads or writes GitHub."""
        self.check["approvable"] = False
        self.check["tier"] = "needs-human"
        self.check["reasons"] = ["tier low-risk not enabled for approval"]
        self.write_check()
        with (
            patch.object(apply_review.pull_reads, "pull_state") as state,
            patch.object(apply_review.github, "api_write") as write,
        ):
            result = self.apply()
        state.assert_not_called()
        write.assert_not_called()
        self.assertEqual(result["verdict"], "needs-human")
        self.assertEqual(result["reasons"], ["tier low-risk not enabled for approval"])
        self.assertEqual(result["run_attempt"], 2)

    def test_each_failing_gate_refuses(self) -> None:
        """Any live change since selection withholds the approval."""
        cases: list[tuple[dict[str, Any], str]] = [
            ({"state": "CLOSED"}, "pull request is CLOSED"),
            ({"is_draft": True}, "pull request is a draft"),
            ({"head_sha": MOVED}, "head moved"),
            ({"ci": "pending"}, "CI is pending"),
            ({"changes_requested": True}, "requested changes"),
            ({"review_requests": ["copilot-pull-request-reviewer[bot]"]}, "Copilot"),
            ({"unresolved_copilot_threads": 2}, "2 unresolved Copilot"),
            ({"approved_by": [BOT]}, f"already approved by {BOT}"),
            ({"approved_by": ["octocat"]}, "already approved by octocat"),
        ]
        for overrides, expected in cases:
            with (
                self.subTest(overrides=overrides),
                patch.object(
                    apply_review.pull_reads,
                    "pull_state",
                    return_value=live_state(**overrides),
                ),
                patch.object(apply_review.github, "api_write") as write,
            ):
                result = self.apply()
                write.assert_not_called()
                self.assertEqual(result["verdict"], "needs-human")
                self.assertEqual(len(result["reasons"]), 1)
                self.assertIn(expected, result["reasons"][0])

    def test_dry_run_reads_live_state_but_does_not_write(self) -> None:
        """A dry run reports what a live run would have hit, then stops."""
        with (
            patch.object(
                apply_review.pull_reads, "pull_state", return_value=live_state()
            ) as state,
            patch.object(apply_review.github, "api_write") as write,
        ):
            result = self.apply("--dry-run")
        state.assert_called_once_with(REPO, 157)
        write.assert_not_called()
        self.assertEqual(result["verdict"], "would-approve")
        self.assertTrue(result["dry_run"])
        self.assertIsNone(result["review_url"])

    def test_live_run_posts_one_approval(self) -> None:
        """The approval targets the checked head and carries provenance."""
        reply = {"html_url": "https://github.com/x/pull/157#pullrequestreview-1"}
        with (
            patch.object(
                apply_review.pull_reads, "pull_state", return_value=live_state()
            ),
            patch.object(apply_review.github, "api_write", return_value=reply) as write,
        ):
            result = self.apply()
        write.assert_called_once()
        method, endpoint, payload = write.call_args.args
        self.assertEqual(
            (method, endpoint), ("POST", f"repos/{REPO}/pulls/157/reviews")
        )
        self.assertEqual(payload["commit_id"], HEAD)
        self.assertEqual(payload["event"], "APPROVE")
        body = payload["body"]
        self.assertTrue(body.startswith(apply_review.OPENINGS["trivial"] + "\n\n"))
        self.assertIn("\n\nFixes a typo. Thanks &#64;someone!\n\n---\n", body)
        self.assertIn(
            f"Automated review by `{BOT}` using model `claude-opus-5.5`", body
        )
        self.assertIn(f"[run]({RUN_URL}). This is not a human review.", body)
        self.assertEqual(result["verdict"], "approved")
        self.assertEqual(result["review_url"], reply["html_url"])
        self.assertFalse(result["dry_run"])

    def test_missing_review_url_and_secretless_bot_name(self) -> None:
        """An empty reply still counts as approved; no App means a generic name."""
        (self.root / "selection.json").write_text(json.dumps(selection(bot_login=None)))
        with (
            patch.object(
                apply_review.pull_reads, "pull_state", return_value=live_state()
            ),
            patch.object(apply_review.github, "api_write", return_value={}) as write,
        ):
            result = self.apply()
        self.assertEqual(result["verdict"], "approved")
        self.assertIsNone(result["review_url"])
        self.assertIn("by `the code review bot`", write.call_args.args[2]["body"])

    def test_github_failure_exits_one(self) -> None:
        """An unreachable GitHub is operational, not a policy outcome."""
        with (
            patch.object(
                apply_review.pull_reads,
                "pull_state",
                side_effect=apply_review.github.GitHubError("gh: boom (HTTP 502)"),
            ),
            self.assertRaises(SystemExit) as caught,
        ):
            self.apply()
        self.assertEqual(caught.exception.code, 1)
        self.assertFalse((self.root / "out" / "result.json").exists())

    def test_check_approvable_with_bad_tier_is_refused(self) -> None:
        """A tampered or inconsistent check.json cannot drive an approval."""
        self.check["tier"] = "needs-human"
        self.write_check()
        with (
            patch.object(apply_review.github, "api_write") as write,
            self.assertRaises(SystemExit),
        ):
            self.apply()
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
