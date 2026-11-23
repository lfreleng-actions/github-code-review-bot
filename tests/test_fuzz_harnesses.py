# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Replay the fuzz harnesses' seeds, and show each oracle can fail.

The harnesses under ``fuzz/`` run under Atheris in the organisation's
central fuzzing job. Here they run without it: every seed goes through
each harness's check, and each oracle is handed a deliberately wrong
result to show it would reject one.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "fuzz"), str(ROOT / "scripts")]
verdict_harness = import_module("fuzz_verdict")
comment_harness = import_module("fuzz_comment")
Breach = import_module("github_markdown").Breach

ZWSP = "\u200b"
ORG = "lfreleng-actions"


class SeedReplayTest(unittest.TestCase):
    """Every seed holds every contract its harness checks."""

    def test_verdict_seeds(self) -> None:
        """The verdict harness accepts each of its seeds."""
        for index, seed in enumerate(verdict_harness.SEEDS):
            with self.subTest(seed=index):
                verdict_harness.check(seed)

    def test_comment_seeds(self) -> None:
        """The comment harness accepts each of its seeds."""
        for index, seed in enumerate(comment_harness.SEEDS):
            with self.subTest(seed=index):
                comment_harness.check(seed)

    def test_a_seed_verdict_is_accepted(self) -> None:
        """The first verdict seed reaches the accepted-verdict oracle."""
        text = verdict_harness.SEEDS[0].decode()
        verdict, failure = verdict_harness.files.parse_verdict(
            text, verdict_harness.policy.extract_verdict
        )
        self.assertIsNone(failure)
        verdict_harness.verify_verdict(text, verdict)


class VerdictOracleTest(unittest.TestCase):
    """The verdict harness's oracles reject wrong results."""

    def accepted(self) -> tuple[str, dict[str, Any]]:
        """A seed message and the verdict extracted from it."""
        text = verdict_harness.verdict_text()
        return text, verdict_harness.policy.extract_verdict(text)

    def test_one_line_rejects_unsafe_text(self) -> None:
        """A live mention, a line break, a marker, a fence, an overrun."""
        for wrong in (
            "cc @team",
            "see &#35;12",
            "GH\\-3",
            "a\nb",
            "::warning::x",
            "##[group]",
            "```",
            "<img src=x>",
            "x" * 700,
        ):
            with self.subTest(wrong=wrong), self.assertRaises(Breach):
                verdict_harness.one_line(wrong, 600, "wrong")
        verdict_harness.one_line(f"cc @{ZWSP}team #{ZWSP}12 <{ZWSP}img", 600, "ok")

    def test_verify_verdict_rejects_a_wrong_verdict(self) -> None:
        """A field not from the final block, or out of bounds, is caught."""
        text, verdict = self.accepted()
        verdict_harness.verify_verdict(text, verdict)
        for key, wrong in (
            ("tier", "needs-human"),
            ("head_sha", "b" * 40),
            ("summary", "cc @team"),
            ("findings", [{"area": "style", "note": "x"}]),
            ("injection_attempts", ["x"] * 21),
        ):
            with self.subTest(key=key), self.assertRaises(Breach):
                verdict_harness.verify_verdict(text, {**verdict, key: wrong})

    def test_verify_verdict_rejects_an_earlier_block(self) -> None:
        """A verdict accepted from anything but the final block is caught."""
        text, verdict = self.accepted()
        for later in ("```text\nx\n```\n", "```json\n"):
            with self.subTest(later=later), self.assertRaises(Breach):
                verdict_harness.verify_verdict(text + later, verdict)

    def test_verify_verdict_rejects_another_schema(self) -> None:
        """A block of another schema, or a verdict claiming one, is caught."""
        _, verdict = self.accepted()
        for source, claimed in ((2, 1), (None, 1), (True, 1), (1, 2)):
            wrong = verdict_harness.verdict_text(schema=source)
            with self.subTest(source=source), self.assertRaises(Breach):
                verdict_harness.verify_verdict(wrong, {**verdict, "schema": claimed})
        verdict_harness.verify_verdict(
            verdict_harness.verdict_text(schema=1.0), verdict
        )

    def test_verify_verdict_rejects_substituted_text(self) -> None:
        """Text blanked, swapped or not the block's own is caught."""
        text = verdict_harness.verdict_text(
            tier="needs-human",
            summary="Needs a human: @team & co.",
            findings=[{"area": "ci", "note": "a"}, {"area": "ci", "note": "b"}],
            injection_attempts=["x", "y"],
        )
        verdict: dict[str, Any] = verdict_harness.policy.extract_verdict(text)
        verdict_harness.verify_verdict(text, verdict)
        notes: list[Any] = verdict["findings"]
        wrongs: tuple[tuple[str, Any], ...] = (
            ("summary", ""),
            ("summary", "Needs a human..."),
            ("findings", [{**notes[0], "note": ""}, notes[1]]),
            ("findings", [notes[0], {**notes[1], "note": "a"}]),
            ("injection_attempts", ["y", "x"]),
        )
        for key, wrong in wrongs:
            with self.subTest(key=key, wrong=wrong), self.assertRaises(Breach):
                verdict_harness.verify_verdict(text, {**verdict, key: wrong})

    def test_verify_text_accepts_a_cut_through_an_escape(self) -> None:
        """A cut that splits an added ``&amp;`` still reads as the source."""
        for kept, limit in (("a &am...", 5), ("a &amp...", 6)):
            self.assertEqual(verdict_harness.policy.sanitise("a &#64; b", limit), kept)
            verdict_harness.verify_text(kept, "a &#64; b", limit, "summary")
        for wrong in ("a &am", "a &am..."):
            with self.subTest(wrong=wrong), self.assertRaises(Breach):
                verdict_harness.verify_text(wrong, "a &#64; b", 600, "summary")

    def test_verify_text_keeps_word_breaks(self) -> None:
        """Text whose spaces moved reads differently, and is caught."""
        verdict_harness.verify_text("a: :b # #[c", "a::b\n ##[c", 600, "summary")
        for kept, source in (
            ("The bug is nowhere.", "The bug is now here."),
            ("Use token x::y for this check.", "Use token x: :y for this check."),
            ("a & b", "a &amp; b"),
        ):
            with self.subTest(kept=kept), self.assertRaises(Breach):
                verdict_harness.verify_text(kept, source, 600, "summary")

    def test_verify_text_rejects_an_emptied_cut(self) -> None:
        """A cut keeping nothing, or of a source too short to cut, is caught."""
        long = "critical security issue " * 30
        verdict_harness.verify_text(long[:600].rstrip() + "...", long, 600, "summary")
        spaced = ZWSP * 1000 + "x"
        verdict_harness.verify_text(
            verdict_harness.policy.sanitise(spaced, 600), spaced, 600, "summary"
        )
        for kept, source in (
            (" " * 597 + "...", "critical security issue"),
            (" " * 597 + "...", long),
            (ZWSP * 597 + "...", long),
            (ZWSP * 600 + "...", "critical security issue" + ZWSP * 1000),
            ("a" * 120 + ZWSP * 480 + "...", "a" * 150),
        ):
            with self.subTest(kept=kept[:1]), self.assertRaises(Breach):
                verdict_harness.verify_text(kept, source, 600, "summary")

    def test_verify_text_rejects_invented_marks(self) -> None:
        """A zero-width space where the source spells out no trigger is caught."""
        for source in ("gh-1", "GH\\-2", "#3", "/issues/4", "@a", "<b", "x@"):
            kept = verdict_harness.policy.sanitise(source, 600)
            verdict_harness.verify_text(kept, source, 600, "summary")
        warning = "h" * 300 + " critical security issue"
        for kept, source in (
            ("h" + ZWSP * 300 + "...", warning),
            (("h" + ZWSP) * 300 + "...", warning),
            ("#" + ZWSP + "a", "#a"),
            ("xg" + ZWSP + "h-1", "xgh-1"),
            ("/issue/" + ZWSP + "1", "/issue/1"),
            ("@" + ZWSP * 599 + "...", "@ critical security issue"),
            ("#" + ZWSP * 2 + "1", "#1"),
        ):
            with self.subTest(kept=kept[:8]), self.assertRaises(Breach):
                verdict_harness.verify_text(kept, source, 600, "summary")

    def test_verify_text_escapes_only_references(self) -> None:
        """An ``amp;`` is due after a reference's ``&``, and nowhere else."""
        source = "&copy; &amp; a & b &#64;x &&lt; &"
        kept = verdict_harness.policy.sanitise(source, 600)
        verdict_harness.verify_text(kept, source, 600, "summary")
        for kept, source in (
            ("&amp;" * 120 + "...", "&" * 120 + "critical security issue"),
            ("a &amp; b", "a & b"),
            ("&copy;", "&copy;"),
            ("&amp;amp;amp;", "&amp;amp;"),
        ):
            with self.subTest(kept=kept[:8]), self.assertRaises(Breach):
                verdict_harness.verify_text(kept, source, 600, "summary")

    @unittest.expectedFailure
    def test_verify_text_breaks_colons_in_pairs(self) -> None:
        """Reported in review: ``sanitise`` breaks non-overlapping ``::``
        pairs, so a space after every colon hides a warning the real
        result keeps.
        """
        warning = " critical security issue"
        for count in (300, 301, 302):
            source = ":" * count + warning
            with self.subTest(count=count):
                kept = verdict_harness.policy.sanitise(source, 600)
                verdict_harness.verify_text(kept, source, 600, "summary")
                wrong = (": " * count + warning)[:600].rstrip() + "..."
                with self.assertRaises(Breach):
                    verdict_harness.verify_text(wrong, source, 600, "summary")
        with self.assertRaises(Breach):
            verdict_harness.verify_text(
                ": " * 299 + "::...", ":" * 301 + warning, 600, "summary"
            )

    @unittest.expectedFailure
    def test_an_uncut_text_fits_the_limit(self) -> None:
        """Reported in review: a whole match over the limit is no result
        ``sanitise`` returns, which cuts at 600 and appends ``...``.
        """
        for count in (599, 600, 601):
            source = "a" * count
            kept = verdict_harness.policy.sanitise(source, 600)
            with self.subTest(count=count):
                verdict_harness.verify_text(kept, source, 600, "summary")
                verdict_harness.one_line(kept, 600, "summary")
        for wrong in ("a" * 601, "a" * 602):
            with self.subTest(wrong=len(wrong)):
                with self.assertRaises(Breach):
                    verdict_harness.verify_text(wrong, wrong, 600, "summary")
                with self.assertRaises(Breach):
                    verdict_harness.one_line(wrong, 600, "summary")

    @unittest.expectedFailure
    def test_verify_text_cuts_only_a_text_too_long(self) -> None:
        """Reported in review: a prefix one short is a cut only where
        ``rstrip`` took a space, the source's or a mark's, from a text
        that runs past the limit. Reported in review: a text that ends on
        its mark at the limit exactly is whole, so needs no cut. Reported
        in review: a zero-width space due before the source's space is
        what the cut keeps, since ``rstrip`` leaves it.
        """
        for source in (
            "a" * 596 + " notx",
            "a" * 599 + " b",
            "a" * 598 + "::x",
            "a" * 598 + "##[x",
            "a" * 599 + "@",
            "a" * 598 + "@ b",
            "a" * 599 + "@ b",
            "a" * 598 + "< b",
        ):
            kept = verdict_harness.policy.sanitise(source, 600)
            with self.subTest(source=source[-4:]):
                self.assertTrue(kept.endswith("..."))
                verdict_harness.verify_text(kept, source, 600, "summary")
        for kept, source in (
            ("a" * 596 + " no...", "a" * 596 + " not"),
            ("a" * 596 + " not...", "a" * 596 + " not"),
            ("a" * 596 + " no...", "a" * 596 + " notx"),
            ("a" * 598 + ":...", "a" * 598 + ":bcd"),
            ("a" * 596 + ": :...", "a" * 596 + ":::x"),
            ("a" * 598 + f"@{ZWSP}...", "a" * 598 + "@"),
            ("a" * 597 + f"@{ZWSP}...", "a" * 597 + "@"),
            ("a" * 598 + "@...", "a" * 598 + "@ b"),
            ("a" * 598 + "<...", "a" * 598 + "< b"),
            ("a" * 597 + "@...", "a" * 597 + "@ b"),
        ):
            with self.subTest(kept=kept[-6:], source=source[-5:]):
                with self.assertRaises(Breach):
                    verdict_harness.verify_text(kept, source, 600, "summary")
        whole = "a" * 598 + "@"
        self.assertEqual(verdict_harness.policy.sanitise(whole, 600), whole + ZWSP)
        verdict_harness.verify_text(whole + ZWSP, whole, 600, "summary")

    def test_verify_verdict_rejects_a_coerced_number(self) -> None:
        """A pull request number that was a boolean or a float is caught."""
        _, verdict = self.accepted()
        for source in (True, 1.0):
            wrong = verdict_harness.verdict_text(pull_request=source)
            with self.subTest(source=source), self.assertRaises(Breach):
                verdict_harness.verify_verdict(wrong, {**verdict, "pull_request": 1})

    def test_emitted_rejects_an_uncontained_approval(self) -> None:
        """An approval body posting its summary uncontained is caught."""
        _, verdict = self.accepted()
        verdict = {**verdict, "summary": "See https://evil.example"}
        verdict_harness.emitted(verdict, None)

        def wrong(check: dict[str, Any], *_: object) -> str:
            return f"Approved.\n\n{check['summary']}\n\n---\nFooter"

        with (
            mock.patch.object(verdict_harness.review, "approval_body", wrong),
            self.assertRaises(Breach),
        ):
            verdict_harness.emitted(verdict, None)

    def test_verify_verdict_rejects_dropped_entries(self) -> None:
        """Findings or injection attempts lost from the final block are caught."""
        text = verdict_harness.verdict_text(
            tier="needs-human",
            findings=[
                {"area": "security", "note": "a"},
                {"area": "ci", "note": "a"},
            ],
            injection_attempts=["x"],
        )
        verdict: dict[str, Any] = verdict_harness.policy.extract_verdict(text)
        verdict_harness.verify_verdict(text, verdict)
        findings: list[Any] = verdict["findings"]
        wrongs: tuple[tuple[str, list[Any]], ...] = (
            ("findings", []),
            ("findings", findings[:1]),
            ("findings", findings[::-1]),
            ("injection_attempts", []),
        )
        for key, wrong in wrongs:
            with self.subTest(key=key, wrong=wrong), self.assertRaises(Breach):
                verdict_harness.verify_verdict(text, {**verdict, key: wrong})

    def test_last_block_reads_a_fence_line_once(self) -> None:
        """A long fence run before a carriage return costs the oracle one pass."""
        text = "`" * 40_000 + "\rx\n" + verdict_harness.verdict_text()
        started = time.perf_counter()
        block = verdict_harness.last_block(text)
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertEqual(block[0] if block else None, "json")

    def test_emitted_rejects_text_no_stream_can_write(self) -> None:
        """A reason holding a lone surrogate fails the strict UTF-8 render."""
        _, verdict = self.accepted()
        wrong = {
            **verdict,
            "tier": "needs-human",
            "findings": [{"area": "other", "note": "\ud800"}],
        }
        with self.assertRaises(UnicodeEncodeError):
            verdict_harness.emitted(wrong, None)
        verdict_harness.emitted(verdict, None)

    def test_verify_posted_rejects_outside_links(self) -> None:
        """Links leaving the organisation, however spelt, are caught."""
        for wrong in (
            "https://evil.example",
            "http://github.com/lfreleng-actions/x",
            f"https://github.com/{ORG}/../evil",
            f"https://github.com/{ORG}/%2e%2e/evil",
            f"https://github.com/{ORG}\\..\\evil",
            f"https://github.com/{ORG}x/repo",
            f"https://github.com/{ORG})other/repo",
            f"https://github.com/{ORG}'?",
            f"https://github.com/{ORG}]",
            "ftp://evil.example/x",
            f"https://github.com@evil.example/{ORG}/",
            "[x](https://evil.example)",
            "[x]: https://evil.example",
            "www.evil.example",
        ):
            with self.subTest(wrong=wrong), self.assertRaises(Breach):
                verdict_harness.verify_posted(wrong)
        # The Kelvin sign lowercases to k, but names no organisation k.
        with self.assertRaises(Breach):
            verdict_harness.verify_posted("https://github.com/\u212a/repo", "k")
        verdict_harness.verify_posted("https://github.com/K/repo", "k")
        verdict_harness.verify_posted(f"https://github.com/{ORG}/repo/blob/x.py).")
        verdict_harness.verify_posted(f"(https://github.com/{ORG}).")

    @unittest.expectedFailure
    def test_verify_contained_keeps_schemes_in_kept_links(self) -> None:
        """Reported in review: a scheme broken inside an organisation link
        changes where it goes. ``live_links`` reads such a scheme as part
        of the link, and a scheme of its own as a link.
        """
        source = f"See https://github.com/{ORG}/repo/search?q=ftp://server x"
        verdict_harness.verify_contained(source)
        verdict_harness.verify_posted(source)
        verdict_harness.verify_links_kept(source, source)
        other = f"See https://github.com/{ORG}/r?q=x://y&z=https://s x"
        for written, wrong in (
            (source, source.replace("ftp:", "ftp:" + ZWSP)),
            (other, other.replace("s://s", f"s:{ZWSP}//s")),
        ):
            with self.subTest(wrong=wrong), self.assertRaises(Breach):
                verdict_harness.verify_links_kept(written, wrong)
        for wrong in (
            f"https://github.com/{ORG}/r ftp://e.com",
            f"https://github.com/someone/?q=https://github.com/{ORG}/r",
            f"ftp://e.com/?q=https://github.com/{ORG}/r",
        ):
            with self.subTest(wrong=wrong), self.assertRaises(Breach):
                verdict_harness.verify_posted(wrong)
        for written in (
            f"`https://github.com/{ORG}/r`_https://evil.example",
            f"xhttps://github.com/{ORG}/r_https://evil.example",
        ):
            with self.subTest(written=written):
                verdict_harness.verify_contained(written)
                with self.assertRaises(Breach):
                    verdict_harness.verify_posted(written)
        verdict_harness.verify_posted(f"_https://github.com/{ORG}/r_https://e.com")
        ticked = f"https://github.com/{ORG}/r?u=ftp://x`y?v=ftp://z"
        verdict_harness.verify_links_kept(
            ticked, ticked.replace("v=ftp:", f"v=ftp:{ZWSP}")
        )
        source = f"https://github.com/{ORG}/r?u=ftp://server"
        for wrong in (
            ticked.replace("u=ftp:", f"u=ftp:{ZWSP}"),
            source.replace("ftp:", f"ftp:{ZWSP}"),
            source.replace("ftp:", f"ftp:{ZWSP}{ZWSP}"),
        ):
            with self.subTest(wrong=wrong), self.assertRaises(Breach):
                verdict_harness.verify_links_kept(wrong.replace(ZWSP, ""), wrong)
        broken = f"ftp:{ZWSP}//e.com https://github.com/{ORG}/r?x=ftp://y"
        verdict_harness.verify_links_kept(broken.replace(ZWSP, ""), broken)
        # Reported in review: a kept link whose own scheme breaks is no
        # link in the result, so the source decides what to keep.
        outer = source.replace("https:", f"https:{ZWSP}")
        for wrong in (outer, outer.replace("ftp:", f"ftp:{ZWSP}")):
            for harness in (verdict_harness, comment_harness):
                with self.subTest(wrong=wrong), self.assertRaises(Breach):
                    harness.verify_links_kept(source, wrong)
        for written in (
            f"https://github.com/{ORG}/../r?u=ftp://server",
            f"https://github.com/{ORG}/r?u=ftp://server/..",
            f"https://github.com/{ORG}?u=ftp://server",
            f"xhttps://github.com/{ORG}/r?u=ftp://server",
        ):
            with self.subTest(written=written):
                verdict_harness.verify_contained(written)
        verdict_harness.verify_links_kept(
            f"https://github.com/{ORG}?u=ftp://s",
            f"https:{ZWSP}//github.com/{ORG}?u=ftp:{ZWSP}//s",
        )
        verdict_harness.verify_links_kept(
            f"https://github.com/{ORG}/www.x](y", f"https://github.com/{ORG}/www.x](y"
        )
        verdict_harness.verify_links_kept(
            f"https://github.com/{ORG}/www.x](y",
            f"https://github.com/{ORG}/www{ZWSP}.x]{ZWSP}(y",
        )
        # Reported in review: a mark between two characters of a scheme
        # breaks it as surely as one after its colon.
        for wrong in (
            source.replace("ftp:", f"f{ZWSP}tp:"),
            source.replace("ftp:", f"ftp{ZWSP}:"),
            source.replace("ftp://", f"ftp:/{ZWSP}/"),
            source.replace("https:", f"ht{ZWSP}tps:"),
        ):
            for harness in (verdict_harness, comment_harness):
                with self.subTest(wrong=wrong), self.assertRaises(Breach):
                    harness.verify_links_kept(source, wrong)
        # Reported in review: containment keeps a link after a dot segment
        # and a root with nothing after it but what GitHub trims.
        for written in (
            f"x/../https://github.com/{ORG}/r?u=ftp://server",
            f"https://github.com/{ORG}",
            f"(https://github.com/{ORG}).",
        ):
            self.assertEqual(
                verdict_harness.policy.contain_links(written, ORG), written
            )
            wrong = written.replace("https:", f"https:{ZWSP}")
            for harness in (verdict_harness, comment_harness):
                with self.subTest(wrong=wrong), self.assertRaises(Breach):
                    harness.verify_links_kept(written, wrong)
        for written in (
            f"https://github.com/{ORG}/r/../x?u=ftp://server",
            f"https://github.com/{ORG}'",
            f"https://github.com/{ORG}`x",
        ):
            broken = verdict_harness.policy.contain_links(written, ORG)
            with self.subTest(written=written):
                self.assertNotEqual(broken, written)
                verdict_harness.verify_links_kept(written, broken)

    def test_verify_defused_rejects_a_change(self) -> None:
        """A second pass that changes the text is caught."""
        with self.assertRaises(Breach):
            verdict_harness.verify_defused("@" + ZWSP + "a", "@" + ZWSP * 2 + "a")


class CommentOracleTest(unittest.TestCase):
    """The comment harness's oracles reject wrong results."""

    def test_verify_safe_rejects_unsafe_bodies(self) -> None:
        """An overrun, an open fence, a suggestion, a live mention."""
        for wrong in (
            "x" * 2001,
            "```\ncode",
            "  ~~~~\ncode",
            "```suggestion\nx\n```",
            "> ```sugges&#116;ion\n> x",
            "```suggestion:-0+2\nx\n```",
            "cc @team",
            "```\nx\n```\nsee #12",
            "<details>",
        ):
            with self.subTest(wrong=wrong), self.assertRaises(Breach):
                comment_harness.verify_safe(wrong, 2000, "wrong")
        comment_harness.verify_safe("```\n@team #12 <b>\n```", 2000, "ok")
        comment_harness.verify_safe("\\`\\`\\`suggestion", 2000, "escaped")

    def test_verify_code_kept_rejects_changed_code(self) -> None:
        """Code that does not come back as written is caught."""
        source = "Use:\n```\n@retry\n```\n"
        comment_harness.verify_code_kept(source, source.strip(), 2000)
        for wrong in ("Use:\n```\n@\u200bretry\n```", "Use:\n" + ZWSP + "```\n@retry"):
            with self.subTest(wrong=wrong), self.assertRaises(Breach):
                comment_harness.verify_code_kept(source, wrong, 2000)

    def test_verify_code_kept_checks_a_long_uncut_block(self) -> None:
        """Code takes its own size in the bound, so a long block is checked."""
        code = "x = 1\n" * 100
        source = f"Use:\n```\n{code}```\n"
        comment_harness.verify_code_kept(source, source, 2000)
        for wrong in ("Use:", f"Use:\n```\n{code.upper()}```"):
            with self.subTest(wrong=wrong[:8]), self.assertRaises(Breach):
                comment_harness.verify_code_kept(source, wrong, 2000)

    def test_verify_code_kept_counts_each_block(self) -> None:
        """A repeated block dropped, or blocks reordered, is caught."""
        source = "```\na\n```\n```\na\n```\n```\nb\n```\n"
        comment_harness.verify_code_kept(source, source, 2000)
        for wrong in (
            "```\na\n```\n```\nb\n```",
            "```\nb\n```\n```\na\n```\n```\na\n```",
        ):
            with self.subTest(wrong=wrong), self.assertRaises(Breach):
                comment_harness.verify_code_kept(source, wrong, 2000)

    def test_verify_code_kept_checks_beside_long_prose(self) -> None:
        """Prose that cannot grow leaves the code checked, so a drop is caught."""
        source = "a" * 401 + "\n```\nimportant\n```"
        limit = comment_harness.limit_of(b"\x2d")
        self.assertEqual(comment_harness.markdown.comment_text(source, limit), source)
        comment_harness.verify_code_kept(source, source, limit)
        with self.assertRaises(Breach):
            comment_harness.verify_code_kept(source, "a" * 401, limit)

    def test_verify_code_kept_checks_beside_plain_ampersands(self) -> None:
        """An ``&`` that starts no reference cannot grow, so a drop is caught."""
        source = "a & b " * 250 + "\n```\nimportant\n```"
        self.assertEqual(comment_harness.markdown.comment_text(source, 2000), source)
        comment_harness.verify_code_kept(source, source, 2000)
        with self.assertRaises(Breach):
            comment_harness.verify_code_kept(source, "a & b " * 250, 2000)

    @unittest.expectedFailure
    def test_verify_code_kept_checks_beside_escaped_ampersands(self) -> None:
        """Reported in review: ``&amp;`` stays as written, so it is no
        growth, and a drop beside it is caught. A reference that does
        grow still counts.
        """
        block = "\n```\nimportant\n```"
        for prose in ("&amp; " * 250, "&amp; " * 331):
            source = prose + block
            kept = comment_harness.markdown.comment_text(source, 2000)
            with self.subTest(size=len(source)):
                comment_harness.verify_code_kept(source, kept, 2000)
        source = "&amp; " * 250 + block
        self.assertEqual(comment_harness.markdown.comment_text(source, 2000), source)
        with self.assertRaises(Breach):
            comment_harness.verify_code_kept(source, ("&amp; " * 250).strip(), 2000)
        self.assertEqual(comment_harness.growth("&amp; &lt; &#64;"), 9)

    @unittest.expectedFailure
    def test_verify_code_kept_strips_outer_whitespace(self) -> None:
        """Reported in review: ``comment_text`` strips the outer whitespace
        before it fits the body, so only what is left counts against the
        limit, and a drop is caught. Fences are read before the strip, so
        an indented one is still no fence.
        """
        block = "\n```\nimportant\n```"
        for source in (
            " " * 2000 + block,
            block + "\n" * 2000,
            "x" + " " * 2000 + block,
        ):
            kept = comment_harness.markdown.comment_text(source, 2000)
            with self.subTest(source=source[:2]):
                comment_harness.verify_code_kept(source, kept, 2000)
        for source in (" " * 2000 + block, " " * 1983 + block, block + " " * 2000):
            with self.subTest(source=len(source)), self.assertRaises(Breach):
                comment_harness.verify_code_kept(source, "", 2000)
        indented = " " * 2000 + "  ```\nx\n```"
        kept = comment_harness.markdown.comment_text(indented, 2000)
        comment_harness.verify_code_kept(indented, kept, 2000)

    def test_verify_code_kept_rejects_added_blocks(self) -> None:
        """A block inserted or duplicated beside the written one is caught."""
        source = "```\na\n```"
        comment_harness.verify_code_kept(source, source, 2000)
        for wrong in (
            "```\na\n```\n```\na\n```",
            "```\nwrong\n```\n```\na\n```",
            "```\na\n```\n~~~\nwrong\n~~~",
        ):
            with self.subTest(wrong=wrong), self.assertRaises(Breach):
                comment_harness.verify_code_kept(source, wrong, 2000)

    def test_check_reads_unclosed_openers_once(self) -> None:
        """A long run of openers nothing closes costs no quadratic scan."""
        started = time.perf_counter()
        comment_harness.check(b"\xff" + b"~~~x\n" * 20_000)
        self.assertLess(time.perf_counter() - started, 2.0)

    def test_verify_contained_rejects_outside_links(self) -> None:
        """A live link outside the organisation in prose is caught."""
        for wrong in (
            "see https://evil.example",
            "see ftp://evil.example",
            f"see https://github.com/{ORG[:4]}\u2026",
        ):
            with self.subTest(wrong=wrong), self.assertRaises(Breach):
                comment_harness.verify_contained(wrong)
        comment_harness.verify_contained("```\nhttps://evil.example\n```")
        comment_harness.verify_contained(f"see https://github.com/{ORG}/r\u2026")


class DiscoveryTest(unittest.TestCase):
    """Fuzzing is found by the import line, which only harnesses carry."""

    def test_only_harnesses_import_the_fuzzer(self) -> None:
        """The text appears in each harness and in no other tracked Python file.

        Discovery reads tracked files alone, so an ignored build or
        virtual environment directory is no part of it.
        """
        git = shutil.which("git")
        if git is None or not (ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        listed = subprocess.run(
            [git, "ls-files", "-z", "--", "*.py"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8")
        needle = "import " + "atheris"
        carrying = {
            name
            for name in listed.split("\0")
            if name
            and (ROOT / name).is_file()
            and needle in (ROOT / name).read_text(encoding="utf-8")
        }
        self.assertEqual(carrying, {"fuzz/fuzz_verdict.py", "fuzz/fuzz_comment.py"})


if __name__ == "__main__":
    unittest.main()
