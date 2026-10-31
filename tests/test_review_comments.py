# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The antagonistic verdict: extraction, sanitising and anchoring to the diff."""

from __future__ import annotations

import json
import sys
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
comments = import_module("review_comments")

HEAD = "a" * 40
REPO = "lfreleng-actions/repo"
# New-file lines 10-13 in the first hunk (11 added), 40-41 in the second.
PATCH = (
    "@@ -10,3 +10,4 @@ def fetch():\n"
    " context ten\n"
    "+added eleven\n"
    " context twelve\n"
    " context thirteen\n"
    "@@ -38,3 +40,2 @@ def store():\n"
    " context forty\n"
    "-removed\n"
    " context forty-one\n"
    "\\ No newline at end of file"
)


def entry(**overrides: Any) -> dict[str, Any]:
    """A selection entry changing one script and deleting one file."""
    data: dict[str, Any] = {
        "key": "repo-157",
        "repository": REPO,
        "number": 157,
        "head_sha": HEAD,
        "url": "https://github.com/lfreleng-actions/repo/pull/157",
        "title": "Feat: Retry the fetch",
        "files": [
            {"path": "scripts/fetch.py", "patch": PATCH, "patch_truncated": False},
            {"path": "assets/logo.png", "patch": None, "patch_truncated": False},
        ],
    }
    data.update(overrides)
    return data


def comment(**overrides: Any) -> dict[str, Any]:
    """A raw agent comment on an added line."""
    data: dict[str, Any] = {
        "path": "scripts/fetch.py",
        "line": 11,
        "severity": "major",
        "body": "Retries the write too.",
    }
    data.update(overrides)
    return data


def verdict_text(**overrides: Any) -> str:
    """An agent message whose final block is a valid antagonistic verdict."""
    data: dict[str, Any] = {
        "schema": 1,
        "repository": REPO,
        "pull_request": 157,
        "head_sha": HEAD,
        "summary": "The retry repeats writes.",
        "comments": [comment()],
        "injection_attempts": [],
    }
    data.update(overrides)
    return f"Findings follow.\n\n```json\n{json.dumps(data)}\n```\n"


class RightLinesTest(unittest.TestCase):
    """``right_lines`` maps new-file lines the patch shows to their hunk."""

    def test_added_and_context_lines_counted_removed_skipped(self) -> None:
        """Both hunks' right-side lines are present; removed lines take no number."""
        self.assertEqual(
            comments.right_lines(PATCH),
            {10: 0, 11: 0, 12: 0, 13: 0, 40: 1, 41: 1},
        )

    def test_new_and_empty_patches(self) -> None:
        """A new file starts at line one; text without a hunk shows nothing."""
        self.assertEqual(comments.right_lines("@@ -0,0 +1,2 @@\n+a\n+b"), {1: 0, 2: 0})
        self.assertEqual(comments.right_lines("Binary files differ"), {})

    def test_blank_context_count_free_header_and_no_newline_marker(self) -> None:
        """A blank context line keeps its space, a header may omit counts, and
        the no-newline marker mid-hunk takes no line number.
        """
        patch = "@@ -1 +1 @@\n-a\n+b\n\\ No newline at end of file\n \n+c"
        self.assertEqual(comments.right_lines(patch), {1: 0, 2: 0, 3: 0})


class ExtractVerdictTest(unittest.TestCase):
    """``extract_verdict`` refuses a bad verdict and drops a bad comment."""

    def test_valid_verdict(self) -> None:
        """Identity, summary, comments and injections come through."""
        verdict = comments.extract_verdict(verdict_text())
        self.assertEqual(verdict["repository"], REPO)
        self.assertEqual(verdict["pull_request"], 157)
        self.assertEqual(verdict["comments"], [comment()])
        self.assertEqual(verdict["dropped"], [])

    def test_shape_problems_refuse_the_whole_verdict(self) -> None:
        """Outside the comments, any problem leaves no verdict."""
        for overrides in (
            {"schema": 2},
            {"schema": True},
            {"repository": "nope"},
            {"head_sha": "abc"},
            {"pull_request": 0},
            {"summary": 3},
            {"comments": "none"},
            {"injection_attempts": None},
        ):
            with (
                self.subTest(overrides=overrides),
                self.assertRaises(comments.Rejected),
            ):
                comments.extract_verdict(verdict_text(**overrides))

    def test_summary_finding_is_an_optional_boolean(self) -> None:
        """Absent means no defect in the summary; anything but a bool refuses."""
        self.assertIs(
            comments.extract_verdict(verdict_text())["summary_finding"], False
        )
        self.assertIs(
            comments.extract_verdict(verdict_text(summary_finding=True))[
                "summary_finding"
            ],
            True,
        )
        for bad in ("yes", 1, 0.0, [True]):
            with self.subTest(value=bad), self.assertRaises(comments.Rejected):
                comments.extract_verdict(verdict_text(summary_finding=bad))

    def test_an_approve_mode_verdict_is_refused(self) -> None:
        """A tier verdict carries no comments list, so it is not this mode's."""
        text = verdict_text(tier="trivial")
        data = json.loads(text.split("```json\n")[1].split("\n```")[0])
        del data["comments"]
        with self.assertRaises(comments.Rejected):
            comments.extract_verdict(f"```json\n{json.dumps(data)}\n```")

    def test_malformed_comments_are_dropped_with_reasons(self) -> None:
        """Each bad comment costs itself alone and names why."""
        bad = [
            "text",
            comment(path=""),
            comment(path="a\nb"),
            comment(line=0),
            comment(line=True),
            comment(start_line=0),
            comment(start_line=True),
            comment(severity="critical"),
            comment(body="   "),
        ]
        verdict = comments.extract_verdict(verdict_text(comments=[comment(), *bad]))
        self.assertEqual(len(verdict["comments"]), 1)
        self.assertEqual(len(verdict["dropped"]), len(bad))
        self.assertTrue(verdict["dropped"][0].startswith("comment 2: "))

    def test_a_range_that_does_not_open_above_its_line_degrades(self) -> None:
        """A start at or below the line names that line at most: the comment
        keeps it as a single-line comment rather than dropping the finding.
        """
        for start in (11, 12):
            with self.subTest(start=start):
                verdict = comments.extract_verdict(
                    verdict_text(comments=[comment(start_line=start)])
                )
                (kept,) = verdict["comments"]
                self.assertNotIn("start_line", kept)
                self.assertEqual(kept["line"], 11)
                self.assertEqual(verdict["dropped"], [])

    def test_comments_past_the_cap_are_counted_apart_from_malformed_ones(
        self,
    ) -> None:
        """The first 25 are read; the rest count as capped, never as malformed."""
        many = [comment(line=10 + (n % 4), body=f"n{n}") for n in range(30)]
        verdict = comments.extract_verdict(verdict_text(comments=many))
        self.assertEqual(len(verdict["comments"]), comments.MAX_COMMENTS)
        self.assertEqual(verdict["dropped"], [])
        self.assertEqual(verdict["capped"], 5)
        result = comments.check(entry(), verdict)
        self.assertEqual(result["capped"], 5)
        self.assertEqual(result["dropped"], [])


class AnchorTest(unittest.TestCase):
    """``anchor`` splits the comments GitHub can place from those it cannot."""

    def test_valid_lines_and_ranges_go_inline(self) -> None:
        """Added lines, context lines and a range within one hunk are placed."""
        inline, unplaced = comments.anchor(
            entry(),
            [comment(), comment(line=13, body="b"), comment(start_line=10, line=12)],
        )
        self.assertEqual(len(inline), 3)
        self.assertEqual(unplaced, [])

    def test_unplaceable_comments_are_kept_with_a_reason(self) -> None:
        """Unknown files, files without a patch, stray lines and split ranges
        are still findings: they come back unplaced, saying why.
        """
        inline, unplaced = comments.anchor(
            entry(),
            [
                comment(path="README.md"),
                comment(path="assets/logo.png", line=1),
                comment(line=39),
                comment(start_line=12, line=40),
            ],
        )
        self.assertEqual(inline, [])
        self.assertEqual(
            [c["reason"] for c in unplaced],
            [
                "not a file this pull request changes",
                "the file has no diff to anchor on",
                "not a line the diff shows",
                "the range leaves its hunk",
            ],
        )
        self.assertEqual(unplaced[0]["body"], "Retries the write too.")

    def test_exact_repeats_collapse(self) -> None:
        """The same comment twice posts once, placed or not."""
        inline, _ = comments.anchor(entry(), [comment(), comment()])
        self.assertEqual(len(inline), 1)
        _, unplaced = comments.anchor(entry(), [comment(line=99), comment(line=99)])
        self.assertEqual(len(unplaced), 1)


class CheckTest(unittest.TestCase):
    """``check`` produces the antagonistic check.json."""

    def test_usable_verdict_is_commentable(self) -> None:
        """Placed, unplaced and malformed findings each land in their own list."""
        verdict = comments.extract_verdict(
            verdict_text(comments=[comment(), comment(line=99), comment(line=0)])
        )
        result = comments.check(entry(), verdict, usage={"totalPremiumRequestCost": 2})
        self.assertEqual(result["mode"], "antagonistic")
        self.assertTrue(result["commentable"])
        self.assertNotIn("approvable", result)
        self.assertEqual(len(result["comments"]), 1)
        self.assertEqual(len(result["unplaced"]), 1)
        self.assertEqual(len(result["dropped"]), 1)
        self.assertEqual(result["premium_requests"], 2.0)

    def test_only_malformed_findings_are_not_commentable(self) -> None:
        """Posting would read as a clean review and block a fresh look."""
        verdict = comments.extract_verdict(
            verdict_text(comments=[comment(line=0), comment(severity="huge")])
        )
        result = comments.check(entry(), verdict)
        self.assertFalse(result["commentable"])
        self.assertEqual(
            result["reasons"],
            ["every proposed finding read was malformed; nothing to post"],
        )

    def test_a_flagged_summary_posts_when_every_comment_is_malformed(self) -> None:
        """A defect the summary flags is a finding of its own: losing every
        line comment to validation does not silence it.
        """
        verdict = comments.extract_verdict(
            verdict_text(comments=[comment(line=0)], summary_finding=True)
        )
        result = comments.check(entry(), verdict)
        self.assertTrue(result["commentable"])
        self.assertEqual(result["reasons"], [])
        self.assertTrue(result["summary_finding"])
        self.assertEqual(len(result["dropped"]), 1)

    def test_only_unplaced_findings_are_still_commentable(self) -> None:
        """A finding with no line to sit on still goes in the review body."""
        verdict = comments.extract_verdict(verdict_text(comments=[comment(line=99)]))
        result = comments.check(entry(), verdict)
        self.assertTrue(result["commentable"])
        self.assertEqual(result["comments"], [])
        self.assertEqual(len(result["unplaced"]), 1)

    def test_no_verdict_or_wrong_target_is_not_commentable(self) -> None:
        """A failure or a verdict about another head posts nothing."""
        failed = comments.check(entry(), None, failure="no session summary")
        self.assertFalse(failed["commentable"])
        self.assertEqual(failed["reasons"], ["no session summary"])
        other = comments.extract_verdict(verdict_text(head_sha="b" * 40))
        moved = comments.check(entry(), other)
        self.assertFalse(moved["commentable"])
        self.assertIn("verdict targets", moved["reasons"][0])

    def test_a_verdict_with_no_findings_is_still_commentable(self) -> None:
        """An empty comment list still posts the summary on the head."""
        verdict = comments.extract_verdict(verdict_text(comments=[]))
        result = comments.check(entry(), verdict)
        self.assertTrue(result["commentable"])
        self.assertEqual(result["comments"], [])


if __name__ == "__main__":
    unittest.main()
