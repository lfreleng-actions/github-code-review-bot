# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The comment Markdown pass: fences paired, prose disarmed, code kept as written."""

from __future__ import annotations

import sys
import time
import unittest
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
markdown = import_module("comment_markdown")

ZWSP = "\u200b"


class CommentTextTest(unittest.TestCase):
    """``comment_text`` keeps lines but defuses mentions, references and suggestions."""

    def test_mentions_and_references_are_defused(self) -> None:
        """Neither can notify anyone or leave a cross-reference, URLs included."""
        self.assertEqual(
            markdown.comment_text(
                "cc @team, see #12, org/repo#3 and\n"
                "[the issue](https://github.com/org/repo/issues/9)"
            ),
            f"cc @{ZWSP}team, see #{ZWSP}12, org/repo#{ZWSP}3 and\n"
            f"[the issue](https://github.com/org/repo/issues/{ZWSP}9)",
        )

    def test_entity_spellings_stay_literal(self) -> None:
        """GitHub decodes entities before linking, so they are escaped too."""
        self.assertEqual(
            markdown.comment_text("&#64;team &commat;team"),
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
        rendered = markdown.comment_text(text)
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
        rendered = markdown.comment_text(f"Quote it:\n\n````python\n{code}````")
        self.assertIn(f"````python\n{code}````", rendered)
        self.assertEqual(
            markdown.comment_text("````suggestion\n```suggestion\n````"),
            "````text\n```suggestion\n````",
        )
        self.assertEqual(
            markdown.comment_text("say ```suggestion inline"),
            "say ```suggestion inline",
        )

    def test_fenced_code_stays_as_written(self) -> None:
        """GitHub links nothing in a fenced block, so its code pastes cleanly;
        the prose around it is still disarmed.
        """
        code = "@dataclass\nclass A: ...  # see #25, a@b.com\nif a<b: pass\n"
        rendered = markdown.comment_text(f"Use @x:\n\n```python\n{code}```\n\nOr @y.")
        self.assertIn(f"```python\n{code}```", rendered)
        self.assertTrue(rendered.startswith(f"Use @{ZWSP}x:"))
        self.assertTrue(rendered.endswith(f"Or @{ZWSP}y."))

    def test_raw_html_in_prose_stays_text(self) -> None:
        """No beacon or hidden block survives outside code."""
        self.assertEqual(
            markdown.comment_text('<img src="https://e.com/b"> <details>x</details>'),
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
                self.assertEqual(markdown.comment_text(raw), expected)

    def test_an_unclosed_fence_is_closed(self) -> None:
        """Cut mid-block, a fence never swallows the review after it."""
        cut = markdown.comment_text("```\n" + "x\n" * 1500 + "```")
        self.assertTrue(cut.endswith("x\u2026\n```"))
        self.assertLessEqual(len(cut), markdown.MAX_COMMENT_CHARS)
        tilde = markdown.comment_text("~~~~\n" + "y\n" * 1500 + "~~~~")
        self.assertTrue(tilde.endswith("y\u2026\n~~~~"))

    def test_a_cut_body_and_its_closer_fit_the_limit(self) -> None:
        """The closer a cut needs is budgeted, not added past the limit: a
        run longer than the cap opens no block, so it needs no closer, and
        the longest that does still fits.
        """
        longest = "`" * markdown.MAX_FENCE_RUN
        widest = f"{longest}\n" + "z" * 3000 + f"\n{longest}"
        bodies = (
            "`" * 1999 + "\nx\n" + "`" * 1999,
            "~" * 1999 + "\n" + "y\n" * 1500,
            widest,
            "```\n" + "x\n" * 1500,
        )
        for body in bodies:
            with self.subTest(body=body[:12]):
                cut = markdown.comment_text(body)
                self.assertLessEqual(len(cut), markdown.MAX_COMMENT_CHARS)
                self.assertIsNone(markdown.fences(cut.split("\n"))[1])
        self.assertTrue(markdown.comment_text(widest).endswith(f"z\u2026\n{longest}"))

    def test_an_encoded_suggestion_info_string_stays_literal(self) -> None:
        """GitHub decodes a character reference in an info string before it
        reads the language, so an opener's references are escaped; inside
        the block, where GitHub decodes nothing, code stays as written.
        """
        cases = {
            "```sugges&#116;ion\nx\n```": "```sugges&amp;#116;ion\nx\n```",
            "~~~ SUGGES&#x54;ION\nx\n~~~": "~~~ SUGGES&amp;#x54;ION\nx\n~~~",
            "```py\na = '&#116;'\n```": "```py\na = '&#116;'\n```",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(markdown.comment_text(raw), expected)

    def test_a_fence_closes_on_a_run_at_least_as_long(self) -> None:
        """A shorter run inside a longer fence is code, not its closer; an
        opener nothing closes stays open while the scan pairs those after it.
        """
        cases: dict[tuple[str, ...], tuple[set[int], str | None]] = {
            ("````", "```", "```", "````"): ({0, 1, 2, 3}, None),
            ("````", "```", "```"): ({1, 2}, "````"),
            ("~~~", "```", "   ~~~~ "): ({0, 1, 2}, None),
            ("```", "    ```", "x"): (set(), "```"),
            ("`" * 11, "x", "`" * 11): (set(), None),
            ("`" * 10, "x", "`" * 10): ({0, 1, 2}, None),
        }
        for lines, expected in cases.items():
            with self.subTest(lines=lines):
                self.assertEqual(markdown.fences(list(lines)), expected)

    def test_hostile_bodies_cost_one_pass(self) -> None:
        """A body of fences nothing closes, or one long run of fence
        characters, is read in linear time: each took tens of seconds when
        every opener searched the rest of the body for its closer and the
        suggestion pattern backtracked through each run.
        """
        for body in ("```x\n" * 20_000, "`" * 40_000, "~" * 40_000 + "x"):
            with self.subTest(body=body[:8]):
                started = time.perf_counter()
                markdown.comment_text(body)
                self.assertLess(time.perf_counter() - started, 2.0)

    def test_controls_fold_lines_survive_and_length_is_bounded(self) -> None:
        """CRLF becomes LF, other controls become spaces, long text is cut."""
        self.assertEqual(markdown.comment_text("a\r\nb\x07c\td"), "a\nb c\td")
        long = markdown.comment_text("x" * 5000)
        self.assertLessEqual(len(long), markdown.MAX_COMMENT_CHARS)
        self.assertTrue(long.endswith("x\u2026"))


if __name__ == "__main__":
    unittest.main()
