# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Verdict parsing, diff corroboration, vetoes and the check decision table."""

from __future__ import annotations

import json
import sys
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
policy = import_module("review_policy")

HEAD = "a" * 40
SHA_NEW = "b" * 40
REPO = "lfreleng-actions/repo"
WORKFLOW = ".github/workflows/ci.yaml"
# Zero-width space: what stops GitHub linking a mention or reference.
ZWSP = "\u200b"


def verdict(**overrides: Any) -> dict[str, Any]:
    """A valid verdict object with fields replaced."""
    data: dict[str, Any] = {
        "schema": 1,
        "repository": REPO,
        "pull_request": 157,
        "head_sha": HEAD,
        "tier": "trivial",
        "summary": "Bumps one pin.",
        "findings": [],
        "injection_attempts": [],
    }
    data.update(overrides)
    return data


def message(obj: object, prefix: str = "Done.\n") -> str:
    """An agent message ending in a fenced json block."""
    return f"{prefix}```json\n{json.dumps(obj)}\n```\n"


def patch(*lines: str) -> str:
    """A REST patch from changed lines; each gets a context neighbour."""
    return "@@ -1,2 +1,2 @@\n context\n" + "\n".join(lines) + "\n"


def changed(path: str, *lines: str, **extra: Any) -> dict[str, Any]:
    """A per-PR file entry whose patch holds the given changed lines."""
    file: dict[str, Any] = {
        "path": path,
        "status": "modified",
        "patch": patch(*lines),
        "patch_truncated": False,
    }
    file.update(extra)
    return file


def entry(*files: dict[str, Any]) -> dict[str, Any]:
    """A per-PR selection entry over the given files."""
    return {
        "key": "repo-157",
        "repository": REPO,
        "number": 157,
        "head_sha": HEAD,
        "url": "https://github.com/lfreleng-actions/repo/pull/157",
        "title": "Chore: Bump @actions/checkout",
        "files": list(files),
    }


ALL = frozenset({"trivial", "low-risk"})
USES_OLD = f"-        uses: actions/checkout@{HEAD}"
USES_NEW = f"+        uses: actions/checkout@{SHA_NEW}"
DIGEST = "c" * 64


class ExtractVerdictTests(unittest.TestCase):
    """The final fenced block is the only thing read, and it is validated."""

    def test_reads_final_json_block_after_earlier_fences(self) -> None:
        """An earlier example block never stands in for the verdict."""
        text = '```json\n{"schema": 0}\n```\nThen:\n' + message(verdict())
        self.assertEqual(policy.extract_verdict(text)["tier"], "trivial")

    def test_rejects_when_final_block_is_not_json(self) -> None:
        """A trailing non-json block means no verdict, not the earlier one."""
        text = message(verdict()) + "```text\nbye\n```\n"
        with self.assertRaisesRegex(policy.Rejected, "must be json"):
            policy.extract_verdict(text)

    def test_rejects_unterminated_fence(self) -> None:
        """A truncated answer cannot fall back to a completed block."""
        text = message(verdict()) + "```json\n{"
        with self.assertRaisesRegex(policy.Rejected, "unterminated"):
            policy.extract_verdict(text)

    def test_rejects_non_object_and_wrong_schema(self) -> None:
        """Lists, bool schema and other schema numbers are refused."""
        for obj in ([1], verdict(schema=2), verdict(schema=True)):
            with self.subTest(obj=obj), self.assertRaises(policy.Rejected):
                policy.extract_verdict(message(obj))

    def test_rejects_bad_fields(self) -> None:
        """Every field has one shape; nothing else is accepted."""
        bad = [
            verdict(tier="approve"),
            verdict(repository="repo"),
            verdict(pull_request=True),
            verdict(pull_request=0),
            verdict(head_sha="abc"),
            verdict(summary=None),
            verdict(findings={"area": "ci"}),
            verdict(findings=[{"area": "vibes", "note": "x"}]),
            verdict(findings=[{"area": "ci", "note": 3}]),
            verdict(injection_attempts=[1]),
        ]
        for obj in bad:
            with self.subTest(obj=obj), self.assertRaises(policy.Rejected):
                policy.extract_verdict(message(obj))

    def test_bounds_text_and_list_sizes(self) -> None:
        """Oversized summaries and lists are cut, not trusted."""
        obj = verdict(
            summary="x" * 2000,
            findings=[{"area": "ci", "note": "n" * 900}] * 30,
            injection_attempts=["i"] * 30,
        )
        got = policy.extract_verdict(message(obj))
        self.assertEqual(len(got["summary"]), policy.MAX_SUMMARY_CHARS + 3)
        self.assertEqual(len(got["findings"]), policy.MAX_FINDINGS)
        self.assertTrue(got["findings"][0]["note"].endswith("..."))
        self.assertEqual(len(got["injection_attempts"]), policy.MAX_INJECTIONS)


class TextTests(unittest.TestCase):
    """Tier parsing and the one-line sanitiser."""

    def test_parse_approve_tiers(self) -> None:
        """Known tokens, none and empty parse; anything else is an error."""
        self.assertEqual(policy.parse_approve_tiers(" trivial , low-risk"), ALL)
        self.assertEqual(policy.parse_approve_tiers("low-risk"), {"low-risk"})
        self.assertEqual(policy.parse_approve_tiers("none"), frozenset())
        self.assertEqual(policy.parse_approve_tiers(""), frozenset())
        for text in ("needs-human", "trivial,none", "all"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                policy.parse_approve_tiers(text)

    def test_sanitise_neutralises_mentions_markers_and_controls(self) -> None:
        """Mentions, workflow commands and control characters are defused."""
        got = policy.sanitise("hi @octocat\x00::set\n##[cmd]  x", 100)
        self.assertEqual(got, f"hi @{ZWSP}octocat : :set # #[cmd] x")

    def test_sanitise_truncates_and_handles_non_strings(self) -> None:
        """Long text is cut with a marker; non-text becomes empty or digits."""
        self.assertEqual(policy.sanitise("abcdef", 3), "abc...")
        self.assertEqual(policy.sanitise(None, 10), "")
        self.assertEqual(policy.sanitise({"a": 1}, 10), "")
        self.assertEqual(policy.sanitise(42, 10), "42")


class DefuseTests(unittest.TestCase):
    """``defuse`` stops every form GitHub links as a mention or a reference.

    Each input below was rendered through GitHub's Markdown API: the
    raw form links, and the defused form renders as plain text.
    GitHub decodes character references first, so ``&#64;`` and
    ``&commat;`` mention as surely as ``@`` does.
    """

    def test_mentions_including_entity_spellings(self) -> None:
        """A zero-width space follows every @; spelt-out entities stay literal."""
        cases = {
            "@octocat": f"@{ZWSP}octocat",
            "@org/team": f"@{ZWSP}org/team",
            "&#64;octocat": f"&amp;#{ZWSP}64;octocat",
            "&commat;octocat": "&amp;commat;octocat",
            "&#x40;octocat": "&amp;#x40;octocat",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(policy.defuse(raw), expected)

    def test_issue_and_pull_request_references(self) -> None:
        """Short references, GH- references and GitHub URLs all stop linking,
        Markdown-escaped too: rendering drops the backslash before linking.
        """
        cases = {
            "#12": f"#{ZWSP}12",
            "org/repo#12": f"org/repo#{ZWSP}12",
            "GH-12 and gh-3": f"GH{ZWSP}-12 and gh{ZWSP}-3",
            "org/repo/pull/7": f"org/repo/pull/{ZWSP}7",
            "https://github.com/org/repo/issues/12": (
                f"https://github.com/org/repo/issues/{ZWSP}12"
            ),
            "[x](https://github.com/org/repo/pull/7/files)": (
                f"[x](https://github.com/org/repo/pull/{ZWSP}7/files)"
            ),
            "<https://github.com/org/repo/discussions/3>": (
                f"<https://github.com/org/repo/discussions/{ZWSP}3>"
            ),
            "https://github.com/org/repo/pulls/7": (
                f"https://github.com/org/repo/pulls/{ZWSP}7"
            ),
            "GH\\-12": f"GH{ZWSP}\\-12",
            "\\#12": f"\\#{ZWSP}12",
            "\\@octocat": f"\\@{ZWSP}octocat",
            "[x](https://github.com/org/repo/issues\\/12)": (
                f"[x](https://github.com/org/repo/issues\\/{ZWSP}12)"
            ),
            "org/repo/pull\\/7": f"org/repo/pull\\/{ZWSP}7",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(policy.defuse(raw), expected)

    def test_ordinary_text_is_untouched(self) -> None:
        """Headings, anchors, shell operators and plain ampersands read as written."""
        for text in ("## Heading", "see #readme", "a && b", "R&D", "&amp; kept"):
            with self.subTest(text=text):
                self.assertEqual(policy.defuse(text), text)

    def test_defusing_twice_changes_nothing(self) -> None:
        """Text sanitised twice on its way to GitHub reads the same as once."""
        text = "&#64;a @b #1 GH-2 o/r/issues/3 https://github.com/o/r/pull/4"
        once = policy.defuse(text)
        self.assertEqual(policy.defuse(once), once)


class DisarmTests(unittest.TestCase):
    """Raw HTML and links leave the agent's text inert outside the organisation.

    Each output below was rendered through GitHub's Markdown API: the
    disarmed forms render as plain text, the organisation link as a link.
    """

    def test_raw_html_and_angle_autolinks_stay_text(self) -> None:
        """A zero-width space after ``<`` stops tags and ``<scheme:...>`` links."""
        self.assertEqual(
            policy.disarm_html('<img src="x"> <details>a</details> <https://e.com>'),
            f'<{ZWSP}img src="x"> <{ZWSP}details>a<{ZWSP}/details> <{ZWSP}https://e.com>',
        )

    def test_links_outside_the_organisation_become_text(self) -> None:
        """Link and image syntax, definitions and off-site bare URLs all break."""
        cases = {
            "[y](https://e.com/y)": f"[y]{ZWSP}(https:{ZWSP}//e.com/y)",
            "![i](https://e.com/i.png)": f"![i]{ZWSP}(https:{ZWSP}//e.com/i.png)",
            "[ref]: https://e.com/r": f"[ref]{ZWSP}: https:{ZWSP}//e.com/r",
            "see http://e.com and HTTPS://E.COM": (
                f"see http:{ZWSP}//e.com and HTTPS:{ZWSP}//E.COM"
            ),
            "www.e.com/z": f"www{ZWSP}.e.com/z",
            "https://github.com/someone/repo": f"https:{ZWSP}//github.com/someone/repo",
            "https://github.com/org-evil/x": f"https:{ZWSP}//github.com/org-evil/x",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(policy.contain_links(raw, "org"), expected)

    def test_link_definitions_break_in_any_container(self) -> None:
        """A definition in a quote, a list or over two lines feeds a reference
        image as surely as one at the top level, so every ``]:`` breaks.
        """
        cases = {
            "> [b]: //e.com/p.png": f"> [b]{ZWSP}: //e.com/p.png",
            "- a\n\n    [b]: https://e.com/q": f"- a\n\n    [b]{ZWSP}: https:{ZWSP}//e.com/q",
            "[b\nx]: https://e.com/r": f"[b\nx]{ZWSP}: https:{ZWSP}//e.com/r",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(policy.contain_links(raw, "org"), expected)

    def test_lookalike_hosts_are_not_the_allow_listed_one(self) -> None:
        """Case folds over ASCII alone: a dotless i is not GitHub."""
        lookalike = "https://g\u0131thub.com/org/repo"
        self.assertEqual(
            policy.contain_links(lookalike, "org"),
            f"https:{ZWSP}//g\u0131thub.com/org/repo",
        )
        self.assertEqual(
            policy.contain_links("https://GITHUB.COM/ORG/x", "org"),
            "https://GITHUB.COM/ORG/x",
        )

    def test_organisation_links_stay_live(self) -> None:
        """A bare URL under github.com/<org>/ is the one link that survives."""
        for text in (
            "https://github.com/org/repo/blob/main/README.md",
            "(https://github.com/org/repo)",
            "https://github.com/ORG/repo",
            "https://github.com/org",
        ):
            with self.subTest(text=text):
                self.assertEqual(policy.contain_links(text, "org"), text)

    def test_containment_twice_changes_nothing(self) -> None:
        """Text contained on its way through two renderers reads the same."""
        text = "<b> [x](https://e.com) [r]: www.e.com https://github.com/org/r"
        once = policy.contain_links(policy.disarm_html(text), "org")
        self.assertEqual(policy.contain_links(policy.disarm_html(once), "org"), once)

    def test_sanitise_disarms_raw_html(self) -> None:
        """The one-line sanitiser leaves no tag live in a summary."""
        self.assertEqual(policy.sanitise('<img src="x">', 100), f'<{ZWSP}img src="x">')

    def test_dot_segments_take_a_link_out_of_the_organisation(self) -> None:
        """A browser resolves ``.`` and ``..``, percent-encoded or after a
        backslash too, so a URL holding one is no longer under the
        organisation. GitHub links up to the next ASCII space or ``<``, a
        no-break space included, so the whole of that run counts; and it
        trims trailing punctuation and an unmatched ``)`` first, so a dot
        run counts unless a letter or digit follows it.
        """
        cases = {
            "https://github.com/org/../outside/repo": (
                f"https:{ZWSP}//github.com/org/../outside/repo"
            ),
            "https://github.com/org/%2e%2E/outside": (
                f"https:{ZWSP}//github.com/org/%2e%2E/outside"
            ),
            "https://github.com/org/x/.%2e/y": f"https:{ZWSP}//github.com/org/x/.%2e/y",
            "https://github.com/org/./x": f"https:{ZWSP}//github.com/org/./x",
            "https://github.com/org/x\\..\\..\\y": (
                f"https:{ZWSP}//github.com/org/x\\..\\..\\y"
            ),
            "https://github.com/org/x\u00a0/../../y": (
                f"https:{ZWSP}//github.com/org/x\u00a0/../../y"
            ),
            "https://github.com/org/x?https://github.com/org/../y": (
                f"https:{ZWSP}//github.com/org/x?https:{ZWSP}//github.com/org/../y"
            ),
            "https://github.com/org/../x https://github.com/org/y": (
                f"https:{ZWSP}//github.com/org/../x https://github.com/org/y"
            ),
            "(https://github.com/org/%2e%2e)": f"(https:{ZWSP}//github.com/org/%2e%2e)",
            "https://github.com/org/%2e%2e.": f"https:{ZWSP}//github.com/org/%2e%2e.",
            "https://github.com/org/..!": f"https:{ZWSP}//github.com/org/..!",
            "https://github.com/org/x/...": f"https:{ZWSP}//github.com/org/x/...",
            "https://github.com/org/x/..y": "https://github.com/org/x/..y",
            "https://github.com/org/r/blob/main/.github/x": (
                "https://github.com/org/r/blob/main/.github/x"
            ),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(policy.contain_links(raw, "org"), expected)

    def test_a_link_after_any_character_is_still_found(self) -> None:
        """GitHub links a URL or ``www.`` after an underscore, a digit or
        punctuation, where a word boundary misses it.
        """
        cases = {
            "_https://e.com/x_": f"_https:{ZWSP}//e.com/x_",
            "1https://e.com": f"1https:{ZWSP}//e.com",
            "x_www.e.com": f"x_www{ZWSP}.e.com",
            "_https://github.com/org/r_": "_https://github.com/org/r_",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(policy.contain_links(raw, "org"), expected)

    def test_plain_http_is_not_the_allow_listed_scheme(self) -> None:
        """Only ``https://github.com/<org>/`` stays a link: the scheme counts
        as much as the host and the organisation.
        """
        cases = {
            "http://github.com/org/repo": f"http:{ZWSP}//github.com/org/repo",
            "HTTP://GITHUB.COM/ORG/x": f"HTTP:{ZWSP}//GITHUB.COM/ORG/x",
            "HTTPS://github.com/org/x": "HTTPS://github.com/org/x",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(policy.contain_links(raw, "org"), expected)

    def test_sanitise_cannot_open_a_code_fence(self) -> None:
        """A summary stands as a paragraph of its own; opening with a fence,
        it would turn the rest of the body into code up to the next fence.
        """
        cases = {
            "```": f"{ZWSP}```",
            "  ~~~python x": f"{ZWSP}~~~python x",
            "````` @a": f"{ZWSP}````` @{ZWSP}a",
            "a ``` b": "a ``` b",
            "``x``": "``x``",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(policy.sanitise(raw, 100), expected)
        once = policy.sanitise("```", 100)
        self.assertEqual(policy.sanitise(once, 100), once)


class ClassifyTrivialTests(unittest.TestCase):
    """Each trivial class, with a change that passes and one that does not.

    A case is (path, changed lines, expected); ``None`` lines mean the
    patch was absent, as GitHub omits it for binary or huge files.
    """

    CASES: tuple[tuple[str, tuple[str, ...] | None, bool], ...] = (
        ("README.md", None, True),
        ("docs/x.png", None, True),
        ("docs/hooks/build.py", ("+import os",), False),
        ("config/allow_list.txt", ("+evil.example.com:443",), False),
        ("LICENSE", ("+text",), True),
        ("CODEOWNERS", ("+* @team",), False),
        ("uv.lock", ("+anything",), True),
        ("requirements.txt", ("-a==1", "+a==2 \\", "+  --hash=sha256:x"), True),
        ("requirements.txt", ("+-e git+https://x",), False),
        ("pyproject.toml", ('-  "requests>=2.0",', '+  "requests>=2.1",'), True),
        ("pyproject.toml", ('+  "requests>=2.1",', "+line-length = 100"), False),
        ("package.json", ('-  "pad": "^1.0.0",', '+  "pad": "^1.3.0",'), True),
        ("package.json", ('+  "test": "jest"',), False),
        (".pre-commit-config.yaml", ("-  rev: v1.0", "+  rev: v1.1  # frozen"), True),
        (".pre-commit-config.yaml", ("+  - id: new-hook",), False),
        (
            "go.mod",
            ("-\tgolang.org/x/text v0.3.0", "+\tgolang.org/x/text v0.3.8"),
            True,
        ),
        ("Cargo.toml", ('-serde = "1.0.1"', '+serde = { version = "1.0.2" }'), True),
        (WORKFLOW, (USES_OLD, USES_NEW), True),
        ("action.yaml", (USES_OLD, USES_NEW), True),
        (WORKFLOW, (USES_OLD, USES_NEW, "+        run: make"), False),
        (WORKFLOW, (USES_OLD, "+        uses: actions/checkout@v4"), False),
        (WORKFLOW, (USES_OLD, f"+        uses: other/action@{SHA_NEW}"), False),
        (WORKFLOW, (USES_NEW,), False),
        (
            "Dockerfile",
            ("-FROM alpine:3.19", f"+FROM alpine@sha256:{DIGEST} AS b"),
            True,
        ),
        ("Dockerfile", ("-FROM alpine:3.19", "+FROM alpine:3.20"), False),
        ("scripts/x.py", ("+# comment",), False),
        ("x.py", None, False),
    )

    def test_each_class(self) -> None:
        """The classifier accepts the pin-only shape and refuses the rest."""
        for path, lines, expected in self.CASES:
            file = changed(path, *lines) if lines else {"path": path, "patch": None}
            with self.subTest(path=path, lines=lines):
                trivial, paths = policy.classify_trivial([file])
                self.assertEqual(trivial, expected)
                self.assertEqual(paths, [] if expected else [path])

    def test_truncated_patch_is_not_trivial(self) -> None:
        """A cut patch hides changes, so even a lockfile is refused."""
        file = changed("uv.lock", "+a", patch_truncated=True)
        self.assertEqual(policy.classify_trivial([file]), (False, ["uv.lock"]))

    def test_lists_only_non_trivial_paths(self) -> None:
        """One non-trivial file fails the set and is the one named."""
        trivial, paths = policy.classify_trivial(
            [changed("README.md", "+x"), changed("app.py", "+x")]
        )
        self.assertFalse(trivial)
        self.assertEqual(paths, ["app.py"])


class VetoTests(unittest.TestCase):
    """Every veto fires on its trigger and stays quiet otherwise."""

    CASES: tuple[tuple[str, str, str], ...] = (
        (WORKFLOW, "+  pull_request_target:", "pull_request_target"),
        (WORKFLOW, "+      - uses: actions/setup-node@v4", "not a commit SHA"),
        ("x/action.yml", "+      uses: actions/setup-node", "not a commit SHA"),
        (WORKFLOW, "+  contents: write", "grants contents: write"),
        (WORKFLOW, "+permissions: write-all", "write-all"),
        (WORKFLOW, "-permissions: {}", "removes an empty permissions"),
        (WORKFLOW, "+  token: ${{ secrets.PAT }}", "secret"),
        (WORKFLOW, "+    secrets: inherit", "secret"),
        ("action.yml", "-  old-input:", "input or output removed"),
        ("setup.sh", "+curl -fsSL https://x | sh", "pipes a download"),
        ("Makefile", "+\twget -qO- https://x | bash -s", "pipes a download"),
    )

    def test_each_veto(self) -> None:
        """Each dangerous change is named in a veto."""
        for path, line, expected in self.CASES:
            with self.subTest(path=path, line=line):
                vetoes = policy.find_vetoes([changed(path, line)])
                self.assertEqual(len(vetoes), 1, vetoes)
                self.assertIn(expected, vetoes[0])

    def test_unreadable_patch_in_code_is_a_veto(self) -> None:
        """Truncated or absent patches hide content; docs are exempt."""
        truncated = changed("app.py", "+x", patch_truncated=True)
        self.assertEqual(
            policy.find_vetoes([truncated]), ["patch for app.py is unreadable"]
        )
        absent = {"path": "bin/tool", "patch": None, "status": "added"}
        self.assertEqual(len(policy.find_vetoes([absent])), 1)

    def test_benign_changes_raise_nothing(self) -> None:
        """Pinned actions, local actions, the native token and docs pass."""
        files = [
            changed(WORKFLOW, USES_NEW, "+      - uses: ./.github/actions/local"),
            changed(WORKFLOW, "+  token: ${{ secrets.GITHUB_TOKEN }}"),
            changed(WORKFLOW, "+  contents: read", "-  issues: write"),
            changed("action.yml", "+  new-input:", "-    default: x"),
            changed("README.md", "+curl https://x | sh", patch_truncated=True),
            changed("app.py", "-curl https://x | sh"),
        ]
        self.assertEqual(policy.find_vetoes(files), [])

    def test_vetoes_are_stated_once(self) -> None:
        """Repeating a bad line does not repeat the veto."""
        file = changed(WORKFLOW, "+  contents: write", "+  contents: write")
        self.assertEqual(len(policy.find_vetoes([file])), 1)


class CheckTests(unittest.TestCase):
    """The decision table that produces check.json."""

    def test_no_verdict_is_needs_human_with_the_failure(self) -> None:
        """A failed session becomes a typed refusal naming the cause."""
        got = policy.check(entry(), None, approve_tiers=ALL, failure="x\n::y")
        self.assertIsNone(got["agent_tier"])
        self.assertEqual(got["tier"], "needs-human")
        self.assertFalse(got["approvable"])
        self.assertEqual(got["reasons"], ["x : :y"])
        got = policy.check(entry(), None, approve_tiers=ALL)
        self.assertEqual(got["reasons"], ["no verdict"])

    def test_target_mismatch_is_refused(self) -> None:
        """A verdict about another head never approves this one."""
        for bad in (verdict(head_sha=SHA_NEW), verdict(pull_request=2)):
            got = policy.check(entry(), bad, approve_tiers=ALL)
            self.assertEqual(got["tier"], "needs-human")
            self.assertIn("verdict targets", got["reasons"][0])

    def test_trivial_claim_needs_corroboration(self) -> None:
        """The agent saying trivial is not enough; the diff must agree."""
        got = policy.check(
            entry(changed("app.py", "+x"), changed("README.md", "+y")),
            verdict(),
            approve_tiers=ALL,
        )
        self.assertEqual(got["tier"], "needs-human")
        self.assertEqual(got["reasons"], ["trivial claim not corroborated: app.py"])
        self.assertEqual(
            got["classifier"], {"trivial": False, "non_trivial_paths": ["app.py"]}
        )
        ok = policy.check(
            entry(changed("README.md", "+y")), verdict(), approve_tiers=ALL
        )
        self.assertTrue(ok["approvable"])
        self.assertEqual(ok["reasons"], [])

    def test_vetoes_demote_low_risk(self) -> None:
        """A veto overrides the agent's low-risk tier."""
        got = policy.check(
            entry(changed(WORKFLOW, "+  contents: write")),
            verdict(tier="low-risk"),
            approve_tiers=ALL,
        )
        self.assertEqual(got["tier"], "needs-human")
        self.assertEqual(got["agent_tier"], "low-risk")
        self.assertEqual(got["reasons"], got["vetoes"])
        self.assertIn("contents: write", got["reasons"][0])

    def test_tier_not_enabled_keeps_tier_but_not_approvable(self) -> None:
        """A disabled tier is reported as-is with the reason it stays unapproved."""
        got = policy.check(
            entry(changed(WORKFLOW, USES_OLD, USES_NEW)),
            verdict(tier="low-risk"),
            approve_tiers=frozenset({"trivial"}),
        )
        self.assertEqual(got["tier"], "low-risk")
        self.assertFalse(got["approvable"])
        self.assertEqual(got["reasons"], ["tier low-risk not enabled for approval"])

    def test_needs_human_passes_through_with_findings(self) -> None:
        """The agent's own refusal keeps its tier and surfaces its notes."""
        findings = [{"area": "security", "note": "touches auth"}]
        got = policy.check(
            entry(), verdict(tier="needs-human", findings=findings), approve_tiers=ALL
        )
        self.assertEqual(got["tier"], "needs-human")
        self.assertEqual(got["reasons"], ["touches auth"])
        self.assertEqual(got["findings"], findings)
        bare = policy.check(entry(), verdict(tier="needs-human"), approve_tiers=ALL)
        self.assertEqual(bare["reasons"], [policy.HUMAN_REQUESTED])

    def test_copies_entry_fields_and_usage(self) -> None:
        """check.json carries the PR identity, a safe title and the spend."""
        usage = {"totalPremiumRequestCost": 1.5, "totalApiDurationMs": 42999}
        got = policy.check(entry(), verdict(), approve_tiers=ALL, usage=usage)
        self.assertEqual(got["key"], "repo-157")
        self.assertEqual(got["title"], f"Chore: Bump @{ZWSP}actions/checkout")
        self.assertEqual(got["premium_requests"], 1.5)
        self.assertEqual(got["agent_seconds"], 42)

    def test_usage_rejects_non_finite_bool_and_negative(self) -> None:
        """Bad spend figures become null rather than poisoning the report."""
        for cost in (float("inf"), float("nan"), True, -1, "1.5", None):
            usage = {"totalPremiumRequestCost": cost, "totalApiDurationMs": cost}
            got = policy.check(entry(), verdict(), approve_tiers=ALL, usage=usage)
            with self.subTest(cost=cost):
                self.assertIsNone(got["premium_requests"])
                self.assertIsNone(got["agent_seconds"])
        got = policy.check(entry(), verdict(), approve_tiers=ALL)
        self.assertIsNone(got["premium_requests"])


if __name__ == "__main__":
    unittest.main()
