# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Selection policy: parsing, skip rules, ordering, capping and outputs."""

from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
github = import_module("bot_github")
ledger = import_module("ledger")
reads = import_module("pull_reads")
changes = import_module("pull_changes")
select = import_module("select_pulls")
outputs = import_module("selection_outputs")

HEAD = "a" * 40
BASE = "b" * 40
OTHER = "c" * 40
PATCH = "@@ -1 +1 @@\n-a\n+b"
REST_PATH_RE = re.compile(
    r"repos/([^/]+/[^/]+)/pulls/(\d+)/(files|commits)\?per_page=100"
)


def rollup(state: str | None) -> dict[str, Any] | None:
    """A status check rollup with one check, or null when there is none."""
    if state is None:
        return None
    return {
        "state": state,
        "contexts": {
            "nodes": [
                {
                    "__typename": "CheckRun",
                    "name": "Testing",
                    "status": "COMPLETED",
                    "conclusion": state,
                }
            ]
        },
    }


def review(login: str, state: str, commit: str = HEAD) -> dict[str, Any]:
    """One review node."""
    return {
        "author": {"__typename": "User", "login": login},
        "state": state,
        "submittedAt": "2026-02-01T00:00:00Z",
        "commit": {"oid": commit},
    }


def pull_node(repo: str, number: int, **overrides: Any) -> dict[str, Any]:
    """A GraphQL pull request node no skip rule would catch."""
    node: dict[str, Any] = {
        "number": number,
        "url": f"https://github.com/org/{repo}/pull/{number}",
        "title": f"Fix: Thing {number}",
        "body": "Body",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": f"2026-01-{number:02d}T00:00:00Z",
        "authorAssociation": "MEMBER",
        "author": {"__typename": "User", "login": "alice"},
        "headRefOid": HEAD,
        "headRefName": "fix/thing",
        "baseRefName": "main",
        "baseRefOid": BASE,
        "isCrossRepository": False,
        "headRepository": {"nameWithOwner": f"org/{repo}"},
        "labels": {"nodes": []},
        "commits": {
            "totalCount": 1,
            "nodes": [{"commit": {"statusCheckRollup": rollup("SUCCESS")}}],
        },
        "reviews": {"nodes": []},
        "reviewRequests": {"nodes": []},
        "reviewThreads": {"nodes": []},
    }
    node.update(overrides)
    return node


def search_pull(repo: str, number: int, **overrides: Any) -> dict[str, Any]:
    """One entry as ``gh search prs --json`` produces it."""
    entry: dict[str, Any] = {
        "repository": {"name": repo, "nameWithOwner": f"org/{repo}"},
        "number": number,
        "title": f"Fix: Thing {number}",
        "url": f"https://github.com/org/{repo}/pull/{number}",
        "author": {"login": "alice", "is_bot": False, "type": "User"},
        "isDraft": False,
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": f"2026-01-{number:02d}T00:00:00Z",
        "labels": [],
    }
    entry.update(overrides)
    return entry


def rest_file(
    path: str = "README.md", patch_text: str | None = PATCH
) -> dict[str, Any]:
    """One entry of the REST files listing."""
    return {
        "filename": path,
        "status": "modified",
        "additions": 1,
        "deletions": 1,
        "patch": patch_text,
    }


def rest_commit() -> dict[str, Any]:
    """One entry of the REST commits listing."""
    return {
        "sha": HEAD,
        "commit": {"message": "Fix: Thing", "verification": {"verified": True}},
        "author": {"login": "alice"},
    }


def ledger_entry(repo: str, number: int, head: str, *, dry_run: bool) -> dict[str, Any]:
    """One prior assessment as the ledger records it."""
    return {
        "repository": f"org/{repo}",
        "number": number,
        "head_sha": head,
        "verdict": "needs-human",
        "tier": "needs-human",
        "dry_run": dry_run,
        "run_id": 1,
        "assessed_at": "2026-03-01T00:00:00Z",
    }


class FakeGitHub:
    """A ``run_gh`` double serving search, GraphQL and REST reads from canned data."""

    def __init__(self) -> None:
        """Start with nothing to serve."""
        self.search: list[dict[str, Any]] = []
        self.pulls: dict[tuple[str, int], dict[str, Any]] = {}
        self.archived: set[str] = set()
        self.files: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.commits: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.calls: list[list[str]] = []

    def add(self, repo: str, number: int, **overrides: Any) -> None:
        """Register a pull request for search and for the GraphQL read."""
        self.search.append(search_pull(repo, number))
        self.pulls[(f"org/{repo}", number)] = pull_node(repo, number, **overrides)

    def __call__(
        self, args: list[str], *, input: str | None = None, read: bool | None = None
    ) -> str:
        """Answer one gh invocation."""
        self.calls.append(list(args))
        if args[:2] == ["search", "prs"]:
            return json.dumps(self.search)
        if args[:2] == ["api", "graphql"]:
            variables = json.loads(input or "{}")["variables"]
            repo = f"{variables['owner']}/{variables['name']}"
            return json.dumps(
                {
                    "data": {
                        "repository": {
                            "nameWithOwner": repo,
                            "isArchived": repo in self.archived,
                            "pullRequest": self.pulls.get((repo, variables["number"])),
                        }
                    }
                }
            )
        found = REST_PATH_RE.fullmatch(args[1]) if args[:1] == ["api"] else None
        if found is None:
            raise AssertionError(f"unexpected gh call {args}")
        key = (found.group(1), int(found.group(2)))
        if found.group(3) == "files":
            return json.dumps([self.files.get(key, [rest_file()])])
        return json.dumps([self.commits.get(key, [rest_commit()])])


class SelectionCase(unittest.TestCase):
    """Runs ``main`` against a ``FakeGitHub`` with no real subprocess."""

    def setUp(self) -> None:
        """Forbid subprocess use and install the gh double."""
        guard = patch.object(
            github.subprocess,
            "run",
            side_effect=AssertionError("unexpected subprocess"),
        )
        guard.start()
        self.addCleanup(guard.stop)
        self.stderr = io.StringIO()
        self.install(FakeGitHub())

    def install(self, fake: FakeGitHub) -> None:
        """Serve gh calls from ``fake`` until the test ends."""
        self.gh = fake
        transport = patch.object(github, "run_gh", side_effect=fake)
        transport.start()
        self.addCleanup(transport.stop)

    def run_main(
        self,
        *extra: str,
        entries: list[dict[str, Any]] | None = None,
        ledger_text: str | None = None,
    ) -> tuple[dict[str, Any], Path, str]:
        """Run the selection into a fresh directory; return it, the directory and stderr.

        The directory lives until the test ends so the caller can read
        every output file. On failure ``self.stderr`` holds the message.
        """
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        root = Path(holder.name)
        prior = root / "ledger.json"
        prior.write_text(
            ledger_text
            if ledger_text is not None
            else json.dumps({"schema": 1, "entries": entries or []}),
            encoding="utf-8",
        )
        out = root / "out"
        self.stderr = io.StringIO()
        with redirect_stderr(self.stderr):
            select.main(
                [
                    "--org",
                    "org",
                    "--output-dir",
                    str(out),
                    "--model",
                    "claude-opus-5.5",
                    "--approve-tiers",
                    "trivial,low-risk",
                    "--ledger",
                    str(prior),
                    *extra,
                ]
            )
        selection = json.loads((out / "selection.json").read_text(encoding="utf-8"))
        return selection, out, self.stderr.getvalue()

    def assert_only_skip(self, selection: dict[str, Any], reason: str) -> None:
        """The single candidate was skipped for exactly ``reason``."""
        self.assertEqual(selection["pull_requests"], [])
        self.assertEqual(selection["skip_counts"][reason], 1)
        self.assertEqual(sum(selection["skip_counts"].values()), 1)
        (row,) = selection["skipped"]
        self.assertEqual(row["reason"], reason)
        self.assertEqual(row["repository"], "org/alpha")
        self.assertEqual(row["number"], 1)
        self.assertEqual(row["url"], "https://github.com/org/alpha/pull/1")


class ParsePullRequestsTest(unittest.TestCase):
    """``parse_pull_requests`` accepts five spellings and refuses the rest."""

    def test_accepted_forms(self) -> None:
        """Each documented spelling resolves to the same pair."""
        for text in (
            "repo/pull/5",
            "org/repo/pull/5",
            "https://github.com/org/repo/pull/5",
            "https://github.com/org/repo/pull/5/",
            "repo#5",
            "org/repo#5",
            "ORG/Repo#5",
        ):
            with self.subTest(text=text):
                self.assertEqual(select.parse_pull_requests(text, "org"), [("repo", 5)])

    def test_separators_and_dedupe(self) -> None:
        """Commas and whitespace separate; repeats collapse keeping first order."""
        text = "alpha#1, Alpha/pull/1 org/beta#2\nhttps://github.com/org/alpha/pull/1"
        self.assertEqual(
            select.parse_pull_requests(text, "org"), [("alpha", 1), ("beta", 2)]
        )
        self.assertEqual(select.parse_pull_requests(" \n", "org"), [])

    def test_foreign_owner_rejected(self) -> None:
        """A pull request outside the organisation is an operator error."""
        for text in ("other/repo#1", "https://github.com/other/repo/pull/1"):
            with self.subTest(text=text), self.assertRaises(reads.SelectionError):
                select.parse_pull_requests(text, "org")

    def test_garbage_rejected(self) -> None:
        """Anything that is not one of the five forms is refused."""
        for text in (
            "repo",
            "repo#",
            "repo#0",
            "repo#1x",
            "repo/pulls/3",
            "..#1",
            "http://github.com/org/repo/pull/1",
            "https://github.com/repo/pull/1",
            "org/repo/issues/1",
        ):
            with self.subTest(text=text), self.assertRaises(reads.SelectionError):
                select.parse_pull_requests(text, "org")


class ParseRepositoriesTest(unittest.TestCase):
    """``parse_repositories`` splits, folds, dedups and rejects."""

    def test_mixed_separators_fold_and_dedupe(self) -> None:
        """Commas and whitespace both separate; names fold and repeats collapse."""
        self.assertEqual(select.parse_repositories("A, b c,,B"), ["a", "b", "c"])
        self.assertEqual(select.parse_repositories("  \n "), [])

    def test_rejects_bad_names(self) -> None:
        """Traversal, slashes and punctuation are refused."""
        for bad in ("..", ".", "a/b", "x y!", "a,b/c"):
            with self.subTest(bad=bad), self.assertRaises(reads.SelectionError):
                select.parse_repositories(bad)


class ParseMaxPullRequestsTest(unittest.TestCase):
    """``parse_max_pull_requests`` accepts non-negative integers only."""

    def test_values(self) -> None:
        """Zero lifts the cap; a padded positive integer parses."""
        self.assertEqual(select.parse_max_pull_requests("0"), 0)
        self.assertEqual(select.parse_max_pull_requests(" 10 "), 10)

    def test_rejects_invalid(self) -> None:
        """Negatives, words, decimals and empty strings are refused."""
        for bad in ("-1", "ten", "", "1.5"):
            with self.subTest(bad=bad), self.assertRaises(reads.SelectionError):
                select.parse_max_pull_requests(bad)


class ParseApproveTiersTest(unittest.TestCase):
    """``parse_approve_tiers`` yields a sorted list or refuses."""

    def test_accepted(self) -> None:
        """Both tiers, one tier, none and empty all parse; the result is sorted."""
        cases = {
            "trivial,low-risk": ["low-risk", "trivial"],
            " low-risk , trivial ": ["low-risk", "trivial"],
            "trivial": ["trivial"],
            "low-risk": ["low-risk"],
            "none": [],
            "": [],
            "trivial trivial": ["trivial"],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(select.parse_approve_tiers(text), expected)

    def test_rejected(self) -> None:
        """An unknown tier, or none beside a tier, is refused."""
        for bad in ("needs-human", "Trivial", "none,trivial"):
            with self.subTest(bad=bad), self.assertRaises(reads.SelectionError):
                select.parse_approve_tiers(bad)


class LoadExclusionsTest(unittest.TestCase):
    """``load_exclusions`` prefers the override and parses the file."""

    def test_override_beats_file(self) -> None:
        """A non-blank override wins even when the file would differ."""
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "ex.txt"
            file.write_text("from-file\n", encoding="utf-8")
            self.assertEqual(
                select.load_exclusions(file, "Over, ride"), ["over", "ride"]
            )

    def test_file_comments_and_blanks(self) -> None:
        """Comments and blank lines are ignored; the rest is parsed."""
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "ex.txt"
            file.write_text(
                "# heading\n\nAlpha  # trailing\n  \nbeta, Gamma\n", encoding="utf-8"
            )
            self.assertEqual(
                select.load_exclusions(file, ""), ["alpha", "beta", "gamma"]
            )

    def test_nothing_and_non_file(self) -> None:
        """No configuration excludes nothing; a missing path or directory fails."""
        self.assertEqual(select.load_exclusions(None, "  "), [])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(reads.SelectionError):
                select.load_exclusions(Path(tmp) / "absent", "")
            with self.assertRaises(reads.SelectionError):
                select.load_exclusions(Path(tmp), "")


class SummaryTextTest(unittest.TestCase):
    """``summary_text`` renders public text safely on one line."""

    def test_folds_escapes_and_bounds(self) -> None:
        """Control characters fold, markers escape, mentions vanish, length is capped."""
        text = "Hi\n@alice ::set-output ##[cmd] <b>x</b> | [y]"
        rendered = outputs.summary_text(text)
        self.assertEqual(
            rendered,
            "Hi &#64;alice : :set-output # #\\[cmd\\] &lt;b&gt;x&lt;/b&gt; \\| \\[y\\]",
        )
        self.assertEqual(outputs.summary_text("a" * 10, 4), "aaaa...")


class SkipRulesTest(SelectionCase):
    """Each skip reason in the table is detected and counted."""

    def test_excluded_repository(self) -> None:
        """An excluded repository is skipped before any read."""
        self.gh.add("alpha", 1)
        selection, _, _ = self.run_main("--exclude-repos", "Alpha")
        self.assert_only_skip(selection, "repository")
        self.assertFalse(any(args[:2] == ["api", "graphql"] for args in self.gh.calls))

    def test_foreign_owner_in_search(self) -> None:
        """A search hit outside the organisation is not in scope."""
        self.gh.search.append(
            search_pull(
                "alpha", 1, repository={"name": "alpha", "nameWithOwner": "other/alpha"}
            )
        )
        selection, _, _ = self.run_main()
        self.assertEqual(selection["skip_counts"]["repository"], 1)
        self.assertEqual(selection["skipped"][0]["repository"], "other/alpha")

    def test_archived_repository(self) -> None:
        """An archived repository is skipped once the read reveals it."""
        self.gh.add("alpha", 1)
        self.gh.archived.add("org/alpha")
        selection, _, _ = self.run_main()
        self.assert_only_skip(selection, "repository")

    def test_missing_head_repository(self) -> None:
        """A deleted fork cannot be checked out, so the pull request is skipped."""
        self.gh.add("alpha", 1, headRepository=None, isCrossRepository=True)
        selection, _, _ = self.run_main()
        self.assert_only_skip(selection, "repository")

    def test_bot_author_from_search(self) -> None:
        """A bot author in the search payload is skipped before any read."""
        self.gh.search.append(
            search_pull("alpha", 1, author={"login": "dependabot[bot]", "is_bot": True})
        )
        selection, _, _ = self.run_main()
        self.assert_only_skip(selection, "bot_author")
        self.assertFalse(any(args[:2] == ["api", "graphql"] for args in self.gh.calls))

    def test_bot_author_from_read(self) -> None:
        """A named pull request is never searched, so the read catches its bot author."""
        for author in ({"__typename": "Bot", "login": "x"}, None):
            with self.subTest(author=author):
                self.install(FakeGitHub())
                self.gh.pulls[("org/alpha", 1)] = pull_node("alpha", 1, author=author)
                selection, _, _ = self.run_main("--pull-requests", "alpha#1")
                self.assert_only_skip(selection, "bot_author")

    def test_closed(self) -> None:
        """A pull request closed or merged since the search is skipped."""
        for state in ("CLOSED", "MERGED"):
            with self.subTest(state=state):
                self.install(FakeGitHub())
                self.gh.add("alpha", 1, state=state)
                selection, _, _ = self.run_main()
                self.assert_only_skip(selection, "closed")

    def test_draft(self) -> None:
        """A draft is skipped."""
        self.gh.add("alpha", 1, isDraft=True)
        selection, _, _ = self.run_main()
        self.assert_only_skip(selection, "draft")

    def test_conflicting_but_not_unknown(self) -> None:
        """CONFLICTING skips; UNKNOWN is GitHub still computing and does not."""
        self.gh.add("alpha", 1, mergeable="CONFLICTING")
        self.gh.add("beta", 2, mergeable="UNKNOWN")
        selection, _, _ = self.run_main()
        self.assertEqual(selection["skip_counts"]["conflicting"], 1)
        self.assertEqual([e["key"] for e in selection["pull_requests"]], ["beta-2"])

    def test_changes_requested(self) -> None:
        """A standing request for changes skips."""
        self.gh.add("alpha", 1, reviews={"nodes": [review("bob", "CHANGES_REQUESTED")]})
        selection, _, _ = self.run_main()
        self.assert_only_skip(selection, "changes_requested")

    def test_already_approved_on_head_only(self) -> None:
        """An approval of the current head skips; one of an older head does not."""
        self.gh.add("alpha", 1, reviews={"nodes": [review("bob", "APPROVED")]})
        self.gh.add("beta", 2, reviews={"nodes": [review("bob", "APPROVED", OTHER)]})
        selection, _, _ = self.run_main()
        self.assertEqual(selection["skip_counts"]["already_approved"], 1)
        self.assertEqual([e["key"] for e in selection["pull_requests"]], ["beta-2"])

    def test_copilot_pending(self) -> None:
        """A pending Copilot review request skips, whatever its case."""
        self.gh.add(
            "alpha",
            1,
            reviewRequests={
                "nodes": [
                    {"requestedReviewer": {"__typename": "Bot", "login": "COPILOT"}}
                ]
            },
        )
        selection, _, _ = self.run_main()
        self.assert_only_skip(selection, "copilot_pending")

    def test_copilot_feedback(self) -> None:
        """An unresolved Copilot thread skips."""
        self.gh.add(
            "alpha",
            1,
            reviewThreads={
                "nodes": [
                    {
                        "isResolved": False,
                        "comments": {
                            "nodes": [
                                {
                                    "author": {
                                        "__typename": "Bot",
                                        "login": "copilot-pull-request-reviewer",
                                    }
                                }
                            ]
                        },
                    }
                ]
            },
        )
        selection, _, _ = self.run_main()
        self.assert_only_skip(selection, "copilot_feedback")

    def test_ci_states(self) -> None:
        """Pending, failing and absent CI each skip under their own reason."""
        for rollup_state, reason in (
            ("PENDING", "ci_pending"),
            ("EXPECTED", "ci_pending"),
            ("FAILURE", "ci_failing"),
            ("ERROR", "ci_failing"),
            (None, "ci_none"),
        ):
            with self.subTest(rollup=rollup_state):
                self.install(FakeGitHub())
                self.gh.add(
                    "alpha",
                    1,
                    commits={
                        "totalCount": 1,
                        "nodes": [
                            {"commit": {"statusCheckRollup": rollup(rollup_state)}}
                        ],
                    },
                )
                selection, _, _ = self.run_main()
                self.assert_only_skip(selection, reason)

    def test_already_assessed(self) -> None:
        """A ledger entry for this head skips; another head does not."""
        self.gh.add("alpha", 1)
        self.gh.add("beta", 2)
        entries = [
            ledger_entry("alpha", 1, HEAD, dry_run=False),
            ledger_entry("beta", 2, OTHER, dry_run=False),
        ]
        selection, _, _ = self.run_main(entries=entries)
        self.assertEqual(selection["skip_counts"]["already_assessed"], 1)
        self.assertEqual([e["key"] for e in selection["pull_requests"]], ["beta-2"])

    def test_dry_run_entries_count_for_dry_runs_alone(self) -> None:
        """A dry-run assessment skips another dry run but never a live run."""
        self.gh.add("alpha", 1)
        entries = [ledger_entry("alpha", 1, HEAD, dry_run=True)]
        selection, _, _ = self.run_main("--dry-run", entries=entries)
        self.assert_only_skip(selection, "already_assessed")
        self.gh.calls.clear()
        selection, _, _ = self.run_main(entries=entries)
        self.assertEqual([e["key"] for e in selection["pull_requests"]], ["alpha-1"])

    def test_reassess_bypasses_the_ledger(self) -> None:
        """``--reassess`` reviews a head the ledger already holds."""
        self.gh.add("alpha", 1)
        entries = [ledger_entry("alpha", 1, HEAD, dry_run=False)]
        selection, _, _ = self.run_main("--reassess", entries=entries)
        self.assertEqual([e["key"] for e in selection["pull_requests"]], ["alpha-1"])
        self.assertEqual(selection["skip_counts"]["already_assessed"], 0)

    def test_too_large(self) -> None:
        """A diff over the byte cap is skipped after the files read."""
        self.gh.add("alpha", 1)
        self.gh.files[("org/alpha", 1)] = [
            rest_file("big.txt", "x" * (reads.MAX_DIFF_BYTES + 1))
        ]
        selection, _, _ = self.run_main()
        self.assert_only_skip(selection, "too_large")
        self.assertFalse(
            any("/commits" in args[1] for args in self.gh.calls if len(args) > 1)
        )

    def test_cap_keeps_the_oldest_updated(self) -> None:
        """Over the cap, the most recently updated pull requests are dropped."""
        self.gh.add("beta", 3, updatedAt="2026-01-03T00:00:00Z")
        self.gh.add("alpha", 9, updatedAt="2026-01-01T00:00:00Z")
        self.gh.add("alpha", 2, updatedAt="2026-01-02T00:00:00Z")
        selection, _, _ = self.run_main("--max-pull-requests", "2")
        self.assertEqual(selection["skip_counts"]["cap"], 1)
        self.assertEqual(selection["skipped"][0]["number"], 3)
        self.assertEqual(
            [e["key"] for e in selection["pull_requests"]], ["alpha-2", "alpha-9"]
        )

    def test_cap_defaults_to_the_matrix_limit(self) -> None:
        """Zero lifts the count cap only as far as the matrix allows."""
        self.gh.add("alpha", 1)
        with patch.object(select, "MATRIX_LIMIT", 0):
            selection, _, _ = self.run_main("--max-pull-requests", "0")
        self.assert_only_skip(selection, "cap")

    def test_cap_by_selection_bytes(self) -> None:
        """The serialised size budget stops the selection before the verifier would."""
        self.gh.add("alpha", 1)
        self.gh.add("alpha", 2)
        selection, _, _ = self.run_main()
        one = len(json.dumps(selection["pull_requests"][0], indent=2).encode("utf-8"))
        with patch.object(select, "MAX_SELECTION_BYTES", one + one // 2):
            selection, _, _ = self.run_main()
        self.assertEqual(selection["skip_counts"]["cap"], 1)
        self.assertEqual([e["key"] for e in selection["pull_requests"]], ["alpha-1"])


class NamedPullRequestsTest(SelectionCase):
    """``--pull-requests`` reads the named pull requests directly."""

    def test_bypasses_exclusions_and_search(self) -> None:
        """Named pull requests skip the search and ignore the exclusion list."""
        self.gh.pulls[("org/alpha", 1)] = pull_node("alpha", 1)
        selection, out, _ = self.run_main(
            "--pull-requests",
            "Alpha#1",
            "--exclude-repos",
            "alpha",
            "--repositories",
            "beta",
        )
        self.assertFalse(any(args[:2] == ["search", "prs"] for args in self.gh.calls))
        self.assertEqual([e["key"] for e in selection["pull_requests"]], ["alpha-1"])
        self.assertEqual(selection["explicit_pull_requests"], ["org/alpha#1"])
        self.assertEqual(selection["explicit_repositories"], ["beta"])
        self.assertEqual(selection["exclusions"], ["alpha"])
        self.assertEqual(selection["candidates_seen"], 1)
        self.assertEqual(
            (out / "excluded-repos.txt").read_text(encoding="utf-8"), "alpha\n"
        )

    def test_other_rules_still_apply(self) -> None:
        """A named draft is still a draft."""
        self.gh.pulls[("org/alpha", 1)] = pull_node("alpha", 1, isDraft=True)
        selection, _, _ = self.run_main("--pull-requests", "alpha#1")
        self.assert_only_skip(selection, "draft")

    def test_unreadable_named_pull_request_is_an_error(self) -> None:
        """Naming a pull request that cannot be read fails the run."""
        with self.assertRaises(SystemExit) as caught:
            self.run_main("--pull-requests", "alpha#404")
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("named pull request org/alpha#404", self.stderr.getvalue())


class OutputsTest(SelectionCase):
    """The written files carry the documented shapes."""

    def test_selection_shape(self) -> None:
        """Header fields, every skip key, sorted entries and the entry shape."""
        self.gh.add("beta", 2)
        self.gh.add("alpha", 5, updatedAt="2026-01-01T00:00:00Z")
        self.gh.add("alpha", 3, isDraft=True)
        selection, _, stderr = self.run_main(
            "--dry-run", "--bot-slug", "my-app", "--exclude-repos", "zeta, gamma"
        )
        self.assertEqual(selection["schema"], select.SCHEMA)
        self.assertEqual(selection["org"], "org")
        self.assertRegex(
            selection["generated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"
        )
        self.assertTrue(selection["dry_run"])
        self.assertEqual(selection["model"], "claude-opus-5.5")
        self.assertEqual(selection["approve_tiers"], ["low-risk", "trivial"])
        self.assertEqual(selection["bot_login"], "my-app[bot]")
        self.assertEqual(selection["explicit_pull_requests"], [])
        self.assertEqual(selection["explicit_repositories"], [])
        self.assertEqual(selection["exclusions"], ["gamma", "zeta"])
        self.assertEqual(selection["candidates_seen"], 3)
        self.assertEqual(list(selection["skip_counts"]), list(select.SKIP_REASONS))
        self.assertEqual(
            selection["skipped"],
            [
                {
                    "repository": "org/alpha",
                    "number": 3,
                    "url": "https://github.com/org/alpha/pull/3",
                    "title": "Fix: Thing 3",
                    "reason": "draft",
                }
            ],
        )
        self.assertEqual(
            [e["key"] for e in selection["pull_requests"]], ["alpha-5", "beta-2"]
        )
        entry = selection["pull_requests"][0]
        self.assertEqual(
            entry,
            {
                "key": "alpha-5",
                "repository": "org/alpha",
                "repo_name": "alpha",
                "number": 5,
                "url": "https://github.com/org/alpha/pull/5",
                "title": "Fix: Thing 5",
                "body": "Body",
                "author": "alice",
                "author_association": "MEMBER",
                "head_sha": HEAD,
                "head_repository": "org/alpha",
                "head_ref": "fix/thing",
                "base_ref": "main",
                "base_sha": BASE,
                "is_fork": False,
                "labels": [],
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "ci": "success",
                "checks": [
                    {"name": "Testing", "status": "COMPLETED", "conclusion": "SUCCESS"}
                ],
                "reviews": [],
                "commits": [
                    {
                        "sha": HEAD,
                        "message": "Fix: Thing",
                        "verified": True,
                        "author": "alice",
                    }
                ],
                "files": [
                    {
                        "path": "README.md",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 1,
                        "previous_path": None,
                        "patch": PATCH,
                        "patch_truncated": False,
                    }
                ],
                "diff_bytes": len(PATCH),
            },
        )
        self.assertRegex(entry["key"], r"^[A-Za-z0-9_.-]+$")
        self.assertIn("Selected 2 pull request(s) from 3 open; skipped", stderr)

    def test_no_bot_slug_means_null_login(self) -> None:
        """Secretless runs record no bot login."""
        selection, _, _ = self.run_main()
        self.assertIsNone(selection["bot_login"])
        self.assertEqual(selection["pull_requests"], [])

    def test_matrix_excluded_and_summary(self) -> None:
        """matrix.json, excluded-repos.txt and the summary reflect the selection."""
        self.gh.add("alpha", 1, title="Ping @alice\n## not a heading | x")
        self.gh.add("beta", 2, isDraft=True)
        _, out, _ = self.run_main("--exclude-repos", "gamma")
        matrix = json.loads((out / "matrix.json").read_text(encoding="utf-8"))
        self.assertEqual(
            matrix,
            {
                "include": [
                    {
                        "key": "alpha-1",
                        "repository": "org/alpha",
                        "repo_name": "alpha",
                        "number": 1,
                        "head_sha": HEAD,
                        "head_repository": "org/alpha",
                    }
                ]
            },
        )
        self.assertEqual(
            (out / "excluded-repos.txt").read_text(encoding="utf-8"), "gamma\n"
        )
        summary = (out / "selection-summary.md").read_text(encoding="utf-8")
        self.assertIn("## Pull request selection", summary)
        self.assertIn("Candidates seen: 2; selected: 1; skipped: 1.", summary)
        self.assertIn(
            "- [alpha#1](https://github.com/org/alpha/pull/1) "
            "Ping &#64;alice ## not a heading \\| x",
            summary,
        )
        self.assertNotIn("\n## not", summary)
        self.assertIn("| draft | 1 |", summary)
        self.assertNotIn("| cap |", summary)

    def test_empty_summary(self) -> None:
        """Nothing selected and nothing skipped renders without a table."""
        _, out, _ = self.run_main()
        summary = (out / "selection-summary.md").read_text(encoding="utf-8")
        self.assertIn("Nothing selected.", summary)
        self.assertNotIn("Skipped because", summary)
        self.assertIn("approve tiers `low-risk, trivial`", summary)


class FailureTest(SelectionCase):
    """Operational failures exit 1 with a prefixed, safe message."""

    def test_github_error(self) -> None:
        """A failing search takes the failure path and writes nothing."""
        with (
            patch.object(
                github, "run_gh", side_effect=github.GitHubError("boom (HTTP 500)")
            ),
            self.assertRaises(SystemExit) as caught,
        ):
            self.run_main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("select pulls: 'boom (HTTP 500)'", self.stderr.getvalue())

    def test_bad_ledger(self) -> None:
        """An unreadable prior ledger is an operational failure."""
        with self.assertRaises(SystemExit) as caught:
            self.run_main(ledger_text="not json")
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("select pulls: ", self.stderr.getvalue())

    def test_selection_error(self) -> None:
        """Bad operator input is reported, not traced."""
        with self.assertRaises(SystemExit) as caught:
            self.run_main("--max-pull-requests", "lots")
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("max_pull_requests must be", self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
