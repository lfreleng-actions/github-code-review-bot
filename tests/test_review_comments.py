# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The antagonistic verdict: extraction, sanitising and anchoring to the diff."""

from __future__ import annotations

import json
import sys
import time
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
comments = import_module("review_comments")

HEAD = "a" * 40
REPO = "lfreleng-actions/repo"
ZWSP = "\u200b"
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


class CommentTextTest(unittest.TestCase):
    """``comment_text`` keeps lines but defuses mentions, references and suggestions."""

    def test_mentions_and_references_are_defused(self) -> None:
        """Neither can notify anyone or leave a cross-reference, URLs included."""
        self.assertEqual(
            comments.comment_text(
                "cc @team, see #12, org/repo#3 and\n"
                "[the issue](https://github.com/org/repo/issues/9)"
            ),
            f"cc @{ZWSP}team, see #{ZWSP}12, org/repo#{ZWSP}3 and\n"
            f"[the issue](https://github.com/org/repo/issues/{ZWSP}9)",
        )

    def test_entity_spellings_stay_literal(self) -> None:
        """GitHub decodes entities before linking, so they are escaped too."""
        self.assertEqual(
            comments.comment_text("&#64;team &commat;team"),
            f"&amp;#{ZWSP}64;team &amp;commat;team",
        )

    def test_suggestion_fences_become_plain_code(self) -> None:
        """A one-click commit offer turns into a code block a human must apply,
        at the line start, in a blockquote or in a list item alike.
        """
        text = (
            "Use:\n```suggestion\nretry(read)\n```\n~~~ Suggestion\nx\n~~~\n"
            "> Quoted:\n> ```suggestion\n> y\n> ```\n"
            "- In a list:\n  ```suggestion\n  z\n  ```\n"
            "1. Numbered:\n   ~~~~suggestion\n   w\n   ~~~~"
        )
        rendered = comments.comment_text(text)
        self.assertNotIn("suggestion", rendered.lower())
        self.assertIn("```text\nretry(read)\n```", rendered)
        self.assertIn("~~~text\nx", rendered)
        # Fences in containers become text altogether (see below).
        self.assertIn(f"> {ZWSP}```text\n> y", rendered)
        self.assertIn(f"  {ZWSP}```text\n  z", rendered)
        self.assertIn(f"   {ZWSP}~~~~text\n   w", rendered)

    def test_a_suggestion_fence_quoted_in_code_stays_as_written(self) -> None:
        """Only a line that opens a fence is rewritten. Inside a fenced block
        the same text is code, and a paste must give it back intact.
        """
        code = 'marker = "```suggestion"\n```suggestion\n~~~ suggestion\n'
        rendered = comments.comment_text(f"Quote it:\n\n````python\n{code}````")
        self.assertIn(f"````python\n{code}````", rendered)
        self.assertEqual(
            comments.comment_text("````suggestion\n```suggestion\n````"),
            "````text\n```suggestion\n````",
        )
        self.assertEqual(
            comments.comment_text("say ```suggestion inline"),
            "say ```suggestion inline",
        )

    def test_fenced_code_stays_as_written(self) -> None:
        """GitHub links nothing in a fenced block, so its code pastes cleanly;
        the prose around it is still disarmed.
        """
        code = "@dataclass\nclass A: ...  # see #25, a@b.com\nif a<b: pass\n"
        rendered = comments.comment_text(f"Use @x:\n\n```python\n{code}```\n\nOr @y.")
        self.assertIn(f"```python\n{code}```", rendered)
        self.assertTrue(rendered.startswith(f"Use @{ZWSP}x:"))
        self.assertTrue(rendered.endswith(f"Or @{ZWSP}y."))

    def test_raw_html_in_prose_stays_text(self) -> None:
        """No beacon or hidden block survives outside code."""
        self.assertEqual(
            comments.comment_text('<img src="https://e.com/b"> <details>x</details>'),
            f'<{ZWSP}img src="https://e.com/b"> <{ZWSP}details>x<{ZWSP}/details>',
        )

    def test_fences_github_might_pair_differently_are_disarmed(self) -> None:
        """Indented, quoted, listed or unclosed fences become text, so GitHub
        sees only the fences the scanner paired, and prose stays prose.
        """
        cases = {
            "  ```\n@x\n```": f"{ZWSP}```\n@{ZWSP}x\n{ZWSP}```",
            "> ```\n> @x\n> ```": f"> {ZWSP}```\n> @{ZWSP}x\n> {ZWSP}```",
            "- a\n  ~~~\n@x": f"- a\n  {ZWSP}~~~\n@{ZWSP}x",
            "```js `x`\n@x": f"{ZWSP}```js `x`\n@{ZWSP}x",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(comments.comment_text(raw), expected)

    def test_an_unclosed_fence_is_closed(self) -> None:
        """Cut mid-block, a fence never swallows the review after it."""
        cut = comments.comment_text("```\n" + "x\n" * 1500 + "```")
        self.assertTrue(cut.endswith("x...\n```"))
        self.assertLessEqual(len(cut), comments.MAX_COMMENT_CHARS + 7)
        tilde = comments.comment_text("~~~~\n" + "y\n" * 1500 + "~~~~")
        self.assertTrue(tilde.endswith("y...\n~~~~"))

    def test_a_fence_closes_on_a_run_at_least_as_long(self) -> None:
        """A shorter run inside a longer fence is code, not its closer; an
        opener nothing closes stays open while the scan pairs those after it.
        """
        cases: dict[tuple[str, ...], tuple[set[int], str | None]] = {
            ("````", "```", "```", "````"): ({0, 1, 2, 3}, None),
            ("````", "```", "```"): ({1, 2}, "````"),
            ("~~~", "```", "   ~~~~ "): ({0, 1, 2}, None),
            ("```", "    ```", "x"): (set(), "```"),
        }
        for lines, expected in cases.items():
            with self.subTest(lines=lines):
                self.assertEqual(comments.fences(list(lines)), expected)

    def test_hostile_bodies_cost_one_pass(self) -> None:
        """A body of fences nothing closes, or one long run of fence
        characters, is read in linear time: each took tens of seconds when
        every opener searched the rest of the body for its closer and the
        suggestion pattern backtracked through each run.
        """
        for body in ("```x\n" * 20_000, "`" * 40_000, "~" * 40_000 + "x"):
            with self.subTest(body=body[:8]):
                started = time.perf_counter()
                comments.comment_text(body)
                self.assertLess(time.perf_counter() - started, 2.0)

    def test_controls_fold_lines_survive_and_length_is_bounded(self) -> None:
        """CRLF becomes LF, other controls become spaces, long text is cut."""
        self.assertEqual(comments.comment_text("a\r\nb\x07c\td"), "a\nb c\td")
        long = comments.comment_text("x" * 5000)
        self.assertEqual(len(long), comments.MAX_COMMENT_CHARS + 3)
        self.assertTrue(long.endswith("..."))


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
