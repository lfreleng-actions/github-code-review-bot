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
sys.path.insert(0, str(Path(__file__).resolve().parent))
github = import_module("bot_github")
ledger = import_module("ledger")
reads = import_module("pull_reads")
changes = import_module("pull_changes")
select = import_module("select_pulls")
rules = import_module("selection_rules")
outputs = import_module("selection_outputs")
report = import_module("review_report")
BODIES = import_module("review_fixtures").BODIES
CLEAN = BODIES["v2-green-clean"]

HEAD = "a" * 40
BASE = "b" * 40
OTHER = "c" * 40
PATCH = "@@ -1 +1 @@\n-a\n+b"
REST_PATH_RE = re.compile(r"repos/([^/]+/[^/]+)/pulls/(\d+)/reviews\?per_page=100")
COMPARE_PATH_RE = re.compile(
    r"repos/([^/]+/[^/]+)/compare/([0-9a-f]{40})\.\.\.([0-9a-f]{40})\?per_page=100"
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
        "baseRef": {"target": {"oid": BASE}},
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
        # Files and commits per comparison, keyed by repository, base SHA
        # and head SHA; an unlisted comparison has one file and one commit.
        self.diffs: dict[
            tuple[str, str, str], tuple[list[dict[str, Any]], list[dict[str, Any]]]
        ] = {}
        self.reviews: dict[tuple[str, int], list[dict[str, Any]]] = {}
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
        endpoint = args[1] if args[:1] == ["api"] else ""
        compared = COMPARE_PATH_RE.fullmatch(endpoint)
        if compared is not None:
            files, commits = self.diff(*compared.groups())
            return json.dumps([{"files": files, "commits": commits}])
        found = REST_PATH_RE.fullmatch(endpoint)
        if found is None:
            raise AssertionError(f"unexpected gh call {args}")
        key = (found.group(1), int(found.group(2)))
        return json.dumps([self.reviews.get(key, self.rest_reviews(key))])

    def diff(
        self, repository: str, base: str, head: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """The files and commits of one comparison."""
        return self.diffs.get(
            (repository, base, head), ([rest_file()], [rest_commit()])
        )

    def rest_reviews(self, key: tuple[str, int]) -> list[dict[str, Any]]:
        """The REST form of the reviews the GraphQL node carries."""
        nodes = self.pulls[key]["reviews"]["nodes"]
        return [
            {
                "user": {
                    "login": node["author"]["login"],
                    "type": node["author"]["__typename"],
                },
                "state": node["state"],
                "commit_id": node["commit"]["oid"],
                "submitted_at": node.get("submittedAt"),
                "body": node.get("body", ""),
            }
            for node in nodes
        ]


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
            "Hi @\u200balice : :set-output # #\\[cmd\\] &lt;b&gt;x&lt;/b&gt; \\| \\[y\\]",
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
        """A diff over the byte cap is skipped after the comparison read."""
        self.gh.add("alpha", 1)
        self.gh.diffs[("org/alpha", BASE, HEAD)] = (
            [rest_file("big.txt", "x" * (reads.MAX_DIFF_BYTES + 1))],
            [rest_commit()],
        )
        selection, _, _ = self.run_main()
        self.assert_only_skip(selection, "too_large")

    def test_a_comparison_that_moves_during_the_reads_is_skipped(self) -> None:
        """A pull request that moves after the state read is left to the next run."""
        read_comparison = changes.pull_comparison
        cases: list[dict[str, Any]] = [
            {"headRefOid": OTHER},
            {"baseRefName": "release"},
            {"baseRef": {"target": {"oid": OTHER}}},
        ]
        for overrides in cases:

            def moving(
                repository: str,
                base_sha: str,
                head_sha: str,
                moved: dict[str, Any] = overrides,
            ) -> Any:
                """Read the comparison, then move the pull request."""
                self.gh.pulls[(repository, 1)].update(moved)
                return read_comparison(repository, base_sha, head_sha)

            with (
                self.subTest(overrides=overrides),
                patch.object(changes, "pull_comparison", side_effect=moving),
            ):
                self.install(FakeGitHub())
                self.gh.add("alpha", 1)
                selection, _, _ = self.run_main()
                self.assert_only_skip(selection, "moved")

    def test_a_change_and_restore_during_the_reads_keeps_the_selected_diff(
        self,
    ) -> None:
        """The entry's diff is the selected comparison's, whatever the live one was.

        A push or a retarget that is undone before the identity re-read
        leaves that re-read unchanged, so only reads bound to the
        selected head and base SHAs keep the other diff out of the entry.
        """
        cases: list[tuple[str, Any, tuple[str, str]]] = [
            ("headRefOid", OTHER, (BASE, OTHER)),
            ("baseRef", {"target": {"oid": OTHER}}, (OTHER, HEAD)),
        ]
        for field, moved, (base, head) in cases:
            with self.subTest(field=field):
                fake = FakeGitHub()
                fake.add("alpha", 1)
                fake.diffs[("org/alpha", base, head)] = (
                    [rest_file("other.txt")],
                    [{**rest_commit(), "sha": OTHER}],
                )
                node = fake.pulls[("org/alpha", 1)]

                def racing(
                    args: list[str],
                    *,
                    input: str | None = None,
                    read: bool | None = None,
                    field: str = field,
                    moved: Any = moved,
                    node: dict[str, Any] = node,
                    fake: FakeGitHub = fake,
                ) -> str:
                    """Move the pull request for each REST read, then restore it."""
                    if args[:1] != ["api"] or args[1] == "graphql":
                        return fake(args, input=input, read=read)
                    selected = node[field]
                    node[field] = moved
                    try:
                        return fake(args, input=input, read=read)
                    finally:
                        node[field] = selected

                self.install(fake)
                with patch.object(github, "run_gh", side_effect=racing):
                    selection, _, _ = self.run_main()
                (entry,) = selection["pull_requests"]
                self.assertEqual([f["path"] for f in entry["files"]], ["README.md"])
                self.assertEqual([c["sha"] for c in entry["commits"]], [HEAD])

    def test_a_stale_base_sha_cannot_hide_a_revert_of_newer_base_content(
        self,
    ) -> None:
        """The diff runs from the base branch's tip, not the recorded base SHA.

        The base added ``permissions: {}`` to a workflow after the base
        SHA GitHub last recorded for the pull request; the head merged
        that commit, then removed the block. Compared from the stale
        SHA the addition and the removal cancel out, and only a README
        edit is left for the vetoes and the agent to see.
        """
        self.gh.add("alpha", 1, baseRefOid=OTHER)
        workflow = rest_file(
            ".github/workflows/ci.yaml", "@@ -1,2 +1 @@\n name: CI\n-permissions: {}"
        )
        self.gh.diffs[("org/alpha", OTHER, HEAD)] = ([rest_file()], [rest_commit()])
        self.gh.diffs[("org/alpha", BASE, HEAD)] = (
            [rest_file(), workflow],
            [rest_commit()],
        )
        selection, _, _ = self.run_main()
        (entry,) = selection["pull_requests"]
        self.assertEqual(entry["base_sha"], BASE)
        self.assertEqual(
            [f["path"] for f in entry["files"]],
            ["README.md", ".github/workflows/ci.yaml"],
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


class AuthorAssociationTest(SelectionCase):
    """The approve mode reviews pull requests from organisation members alone."""

    @unittest.expectedFailure
    def test_untrusted_author_is_skipped_and_counted(self) -> None:
        """Any association besides OWNER and MEMBER skips as ``untrusted_author``."""
        for association in (
            "NONE",
            "CONTRIBUTOR",
            "FIRST_TIMER",
            "FIRST_TIME_CONTRIBUTOR",
            "COLLABORATOR",
            "MANNEQUIN",
        ):
            with self.subTest(association=association):
                self.install(FakeGitHub())
                self.gh.add("alpha", 1, authorAssociation=association)
                selection, _, _ = self.run_main()
                self.assert_only_skip(selection, "untrusted_author")

    def test_owner_and_member_are_kept(self) -> None:
        """The organisation's owners and members are trusted authors."""
        for association in ("OWNER", "MEMBER"):
            with self.subTest(association=association):
                self.install(FakeGitHub())
                self.gh.add("alpha", 1, authorAssociation=association)
                selection, _, _ = self.run_main()
                self.assertEqual(
                    [e["key"] for e in selection["pull_requests"]], ["alpha-1"]
                )
                self.assertEqual(sum(selection["skip_counts"].values()), 0)

    def test_a_member_fork_is_kept(self) -> None:
        """Fork status is not the criterion: a member's fork is reviewed."""
        self.gh.add(
            "alpha",
            1,
            isCrossRepository=True,
            headRepository={"nameWithOwner": "alice/alpha"},
        )
        selection, _, _ = self.run_main()
        (entry,) = selection["pull_requests"]
        self.assertTrue(entry["is_fork"])
        self.assertEqual(entry["author_association"], "MEMBER")

    @unittest.expectedFailure
    def test_a_missing_association_is_untrusted(self) -> None:
        """A state without an association never passes the author rule."""
        state: dict[str, Any] = {
            "repository_archived": False,
            "head_repository": "org/alpha",
            "author": {"login": "alice", "is_bot": False},
        }
        for association in (None, ""):
            with self.subTest(association=association):
                if association is not None:
                    state["author_association"] = association
                self.assertEqual(
                    rules.first_skip(state, rules.Rules(mode="approve")),
                    "untrusted_author",
                )

    @unittest.expectedFailure
    def test_a_named_untrusted_pull_request_is_skipped(self) -> None:
        """Naming an outside author's pull request does not get it approved."""
        self.gh.pulls[("org/alpha", 1)] = pull_node(
            "alpha", 1, authorAssociation="CONTRIBUTOR"
        )
        selection, _, _ = self.run_main("--pull-requests", "alpha#1")
        self.assert_only_skip(selection, "untrusted_author")

    @unittest.expectedFailure
    def test_the_reason_follows_the_author_reasons(self) -> None:
        """``untrusted_author`` sits directly after the two author reasons."""
        reasons = list(rules.SKIP_REASONS)
        self.assertEqual(reasons[reasons.index("human_author") + 1], "untrusted_author")
        self.assertEqual(reasons.index("human_author"), reasons.index("bot_author") + 1)

    @unittest.expectedFailure
    def test_trusted_associations(self) -> None:
        """OWNER and MEMBER, and nothing else, are trusted."""
        self.assertEqual(rules.TRUSTED_ASSOCIATIONS, frozenset({"OWNER", "MEMBER"}))


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
        self.assertEqual(selection["mode"], "approve")
        self.assertEqual(selection["bot_authors"], [])
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
        self.assertEqual(list(selection["skip_counts"]), list(rules.SKIP_REASONS))
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
            "Ping @\u200balice ## not a heading \\| x",
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


MONKEY = {"__typename": "Bot", "login": "lf-releng-code-monkey-bot"}
MONKEY_SEARCH = {"login": "lf-releng-code-monkey-bot[bot]", "is_bot": True}


def bot_review(
    login: str,
    commit: str = HEAD,
    *,
    body: str = CLEAN,
    submitted_at: str = "2026-02-01T00:00:00Z",
) -> dict[str, Any]:
    """One review node from a bot, Copilot by default in the callers.

    GraphQL reads no ``body``; the fake serves it on the REST history.
    """
    return {
        "author": {"__typename": "Bot", "login": login},
        "state": "COMMENTED",
        "submittedAt": submitted_at,
        "commit": {"oid": commit},
        "body": body,
    }


COPILOT_ON_HEAD = {"nodes": [bot_review("copilot-pull-request-reviewer")]}


class LoadBotAuthorsTest(unittest.TestCase):
    """``load_bot_authors`` reads one folded login per line."""

    def test_comments_suffixes_case_and_repeats(self) -> None:
        """Comments go, ``[bot]`` and case fold away, repeats collapse in order."""
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "authors.txt"
            file.write_text(
                "# heading\nCopilot  # agent\n\nmonkey[bot]\ncopilot\n",
                encoding="utf-8",
            )
            self.assertEqual(select.load_bot_authors(file), ["copilot", "monkey"])

    def test_bad_logins_and_missing_files(self) -> None:
        """A login with a slash or space, or an absent file, is refused."""
        self.assertEqual(select.load_bot_authors(None), [])
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "authors.txt"
            for bad in ("org/bot\n", "two words\n", "-dash\n"):
                file.write_text(bad, encoding="utf-8")
                with self.subTest(bad=bad), self.assertRaises(reads.SelectionError):
                    select.load_bot_authors(file)
            with self.assertRaises(reads.SelectionError):
                select.load_bot_authors(Path(tmp) / "absent")


class AntagonisticSelectionTest(SelectionCase):
    """The antagonistic mode reviews the listed bots and nobody else."""

    def setUp(self) -> None:
        """Write a bot author list naming Code Monkey and Copilot."""
        super().setUp()
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.authors = Path(holder.name) / "authors.txt"
        self.authors.write_text(
            "lf-releng-code-monkey-bot\nCopilot\n", encoding="utf-8"
        )

    def add_bot(self, repo: str, number: int, **overrides: Any) -> None:
        """Register a Code Monkey pull request Copilot has reviewed at its head."""
        self.gh.search.append(search_pull(repo, number, author=MONKEY_SEARCH))
        node: dict[str, Any] = {"author": MONKEY, "reviews": COPILOT_ON_HEAD}
        node.update(overrides)
        self.gh.pulls[(f"org/{repo}", number)] = pull_node(repo, number, **node)

    def run_antagonistic(
        self, *extra: str, entries: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Run the selection in the antagonistic mode; return selection.json."""
        selection, _, _ = self.run_main(
            "--mode",
            "antagonistic",
            "--approve-tiers",
            "none",
            "--bot-authors-file",
            str(self.authors),
            *extra,
            entries=entries,
        )
        return selection

    def test_a_settled_refusal_waits_for_reassess(self) -> None:
        """The report's ledger skips a settled refusal; a failure is retried."""
        self.add_bot("alpha", 1)
        self.add_bot("beta", 2)
        results = [
            {
                "repository": f"org/{repo}",
                "number": number,
                "head_sha": HEAD,
                "verdict": verdict,
                "tier": None,
                "dry_run": False,
                "settled": True,
                "verified": True,
            }
            for repo, number, verdict in (
                ("alpha", 1, "refused"),
                ("beta", 2, "failed"),
            )
        ]
        entries = report.build_ledger(
            ledger.empty_ledger(), results, 7, "antagonistic"
        )["entries"]
        selection = self.run_antagonistic(entries=entries)
        self.assertEqual([e["key"] for e in selection["pull_requests"]], ["beta-2"])
        self.assertEqual(selection["skip_counts"]["already_assessed"], 1)
        self.gh.calls.clear()
        selection = self.run_antagonistic("--reassess", entries=entries)
        self.assertEqual(
            [e["key"] for e in selection["pull_requests"]], ["alpha-1", "beta-2"]
        )

    def test_listed_bots_kept_humans_and_other_bots_skipped(self) -> None:
        """Search applies the author rule with the inverse sense of approve."""
        self.add_bot("alpha", 1)
        self.gh.add("beta", 2)
        self.gh.search.append(
            search_pull("gamma", 3, author={"login": "dependabot[bot]", "is_bot": True})
        )
        selection = self.run_antagonistic()
        self.assertEqual(selection["mode"], "antagonistic")
        self.assertEqual(
            selection["bot_authors"], ["lf-releng-code-monkey-bot", "copilot"]
        )
        self.assertEqual(selection["approve_tiers"], [])
        self.assertEqual([e["key"] for e in selection["pull_requests"]], ["alpha-1"])
        self.assertEqual(
            selection["pull_requests"][0]["author"], "lf-releng-code-monkey-bot"
        )
        self.assertEqual(selection["skip_counts"]["human_author"], 1)
        self.assertEqual(selection["skip_counts"]["bot_author"], 1)

    def test_named_pull_requests_obey_the_author_rule(self) -> None:
        """Naming a human's pull request does not put it in front of this mode."""
        self.gh.add("alpha", 1)
        selection = self.run_antagonistic("--pull-requests", "alpha#1")
        self.assert_only_skip(selection, "human_author")

    def test_ci_never_gates_but_copilot_review_does(self) -> None:
        """Red or absent CI is reviewed; a pending Copilot review waits."""
        self.add_bot(
            "alpha",
            1,
            commits={
                "totalCount": 1,
                "nodes": [{"commit": {"statusCheckRollup": rollup("FAILURE")}}],
            },
        )
        self.add_bot(
            "beta",
            2,
            reviewRequests={
                "nodes": [
                    {
                        "requestedReviewer": {
                            "__typename": "Bot",
                            "login": "copilot-pull-request-reviewer",
                        }
                    }
                ]
            },
        )
        selection = self.run_antagonistic()
        self.assertEqual([e["key"] for e in selection["pull_requests"]], ["alpha-1"])
        self.assertEqual(selection["skip_counts"]["copilot_pending"], 1)
        self.assertEqual(selection["skip_counts"]["ci_failing"], 0)

    def test_a_copilot_review_of_this_head_is_required(self) -> None:
        """No Copilot review, or one of an earlier push, waits for the next."""
        self.add_bot("alpha", 1, reviews={"nodes": []})
        self.add_bot(
            "beta",
            2,
            reviews={"nodes": [bot_review("copilot-pull-request-reviewer", OTHER)]},
        )
        self.add_bot("gamma", 3)
        selection = self.run_antagonistic()
        self.assertEqual([e["key"] for e in selection["pull_requests"]], ["gamma-3"])
        self.assertEqual(selection["skip_counts"]["copilot_unreviewed"], 2)

    def copilot_on_head(self, body: str) -> dict[str, Any]:
        """Run one Code Monkey pull request whose head Copilot reviewed with ``body``."""
        node = bot_review("copilot-pull-request-reviewer", body=body)
        self.add_bot("alpha", 1, reviews={"nodes": [node]})
        return self.run_antagonistic()

    def test_a_copilot_review_with_findings_in_its_body_waits(self) -> None:
        """Body-only findings, with no thread open, are not a clean review."""
        bodies = {
            "yellow": BODIES["v2-yellow-first-current"],
            "green with suppressed": BODIES["v2-green-clean"]
            + "\n<details>\n<summary>Suppressed comments (1)</summary>\n\n"
            "**scripts/x.py:12**\n* Something a reviewer would want to know.\n"
            "</details>\n",
            "previously missed": BODIES["v2-blue-previously-missed-current"],
            "no assessment": BODIES["legacy-no-comments"],
            "unreadable": BODIES["v2-green-clean"] + "\n<!-- hidden -->\n",
            "empty": "",
        }
        for name, body in bodies.items():
            with self.subTest(name=name):
                self.gh.search.clear()
                self.assert_only_skip(self.copilot_on_head(body), "copilot_findings")

    def test_a_suppressed_entry_with_a_non_ascii_path_waits(self) -> None:
        """A backticked entry under a stated zero still counts, whatever its path."""
        body = (
            CLEAN + "\n<details>\n<summary>Suppressed comments (0)</summary>\n\n"
            "**`src/café.py:12`**\n* Something a reviewer would want to know.\n"
            "</details>\n"
        )
        self.assert_only_skip(self.copilot_on_head(body), "copilot_findings")

    def test_a_clean_green_or_blue_review_is_selected(self) -> None:
        """Approval, or a blue review with nothing outstanding, lets the mode run."""
        for name in ("v2-green-clean", "v2-blue-clean", "v2-blue-clean-current"):
            with self.subTest(name=name):
                self.gh.search.clear()
                selection = self.copilot_on_head(BODIES[name])
                self.assertEqual(
                    [e["key"] for e in selection["pull_requests"]], ["alpha-1"]
                )

    def test_only_the_latest_current_head_review_is_classified(self) -> None:
        """A clean review of an older head never counts; the newest of this head
        decides, whichever way it went.
        """
        old_clean = bot_review("copilot-pull-request-reviewer", OTHER)
        yellow = BODIES["v2-yellow-first-current"]

        def copilot(body: str, when: str) -> dict[str, Any]:
            return bot_review(
                "copilot-pull-request-reviewer", body=body, submitted_at=when
            )

        cases = {
            "older head clean, this head yellow": (
                [old_clean, copilot(yellow, "2026-02-02T00:00:00Z")],
                "copilot_findings",
            ),
            "clean then yellow": (
                [
                    copilot(CLEAN, "2026-02-01T00:00:00Z"),
                    copilot(yellow, "2026-02-02T00:00:00Z"),
                ],
                "copilot_findings",
            ),
            "yellow after clean, listed first": (
                [
                    copilot(yellow, "2026-02-02T00:00:00Z"),
                    copilot(CLEAN, "2026-02-01T00:00:00Z"),
                ],
                "copilot_findings",
            ),
            "yellow then clean": (
                [
                    copilot(yellow, "2026-02-01T00:00:00Z"),
                    copilot(CLEAN, "2026-02-02T00:00:00Z"),
                ],
                None,
            ),
        }
        for name, (nodes, reason) in cases.items():
            with self.subTest(name=name):
                self.gh.search.clear()
                self.add_bot("alpha", 1, reviews={"nodes": nodes})
                selection = self.run_antagonistic()
                if reason is None:
                    self.assertEqual(
                        [e["key"] for e in selection["pull_requests"]], ["alpha-1"]
                    )
                else:
                    self.assert_only_skip(selection, reason)

    def test_approve_mode_ignores_the_copilot_review_body(self) -> None:
        """The cleanliness rule is the antagonistic mode's alone."""
        yellow = bot_review(
            "copilot-pull-request-reviewer", body=BODIES["v2-yellow-first-current"]
        )
        self.gh.add("alpha", 1, reviews={"nodes": [yellow]})
        selection, _, _ = self.run_main()
        self.assertEqual([e["key"] for e in selection["pull_requests"]], ["alpha-1"])

    def test_a_head_this_app_reviewed_costs_no_session(self) -> None:
        """GitHub keeps the review, so it outlives any ledger that loses it."""
        own = bot_review("lf-releng-antagonistic-review-bot")
        self.add_bot("alpha", 1, reviews={"nodes": [*COPILOT_ON_HEAD["nodes"], own]})
        older = bot_review("lf-releng-antagonistic-review-bot", OTHER)
        self.add_bot("beta", 2, reviews={"nodes": [*COPILOT_ON_HEAD["nodes"], older]})
        selection = self.run_antagonistic(
            "--bot-slug", "lf-releng-antagonistic-review-bot"
        )
        self.assertEqual([e["key"] for e in selection["pull_requests"]], ["beta-2"])
        self.assertEqual(selection["skip_counts"]["already_reviewed"], 1)

    def test_an_old_review_beyond_the_state_window_still_counts(self) -> None:
        """The full history decides, not the state query's last 100 reviews."""
        self.add_bot("alpha", 1)
        noise = [
            {
                "user": {"login": "alice", "type": "User"},
                "state": "COMMENTED",
                "commit_id": OTHER,
            }
        ] * 120
        own = {
            "user": {"login": "lf-releng-antagonistic-review-bot[bot]", "type": "Bot"},
            "state": "COMMENTED",
            "commit_id": HEAD,
        }
        copilot = {
            "user": {"login": "copilot-pull-request-reviewer[bot]", "type": "Bot"},
            "state": "COMMENTED",
            "commit_id": HEAD,
            "body": CLEAN,
        }
        self.gh.reviews[("org/alpha", 1)] = [copilot, own, *noise]
        selection = self.run_antagonistic(
            "--bot-slug", "lf-releng-antagonistic-review-bot"
        )
        self.assert_only_skip(selection, "already_reviewed")

    def test_a_human_named_like_a_bot_stands_in_for_neither(self) -> None:
        """Folding drops ``[bot]``, so the account type decides: a human whose
        login matches the App or Copilot neither blocks nor satisfies a rule.
        """
        self.add_bot("alpha", 1)
        lookalikes = [
            {
                "user": {"login": name, "type": "User"},
                "state": "COMMENTED",
                "commit_id": HEAD,
            }
            for name in ("lf-releng-antagonistic-review-bot", "copilot")
        ]
        real_copilot = {
            "user": {"login": "copilot-pull-request-reviewer[bot]", "type": "Bot"},
            "state": "COMMENTED",
            "commit_id": HEAD,
            "body": CLEAN,
        }
        self.gh.reviews[("org/alpha", 1)] = lookalikes
        selection = self.run_antagonistic(
            "--bot-slug", "lf-releng-antagonistic-review-bot"
        )
        self.assert_only_skip(selection, "copilot_unreviewed")
        self.gh.reviews[("org/alpha", 1)] = [*lookalikes, real_copilot]
        selection = self.run_antagonistic(
            "--bot-slug", "lf-releng-antagonistic-review-bot"
        )
        self.assertEqual([e["key"] for e in selection["pull_requests"]], ["alpha-1"])

    def test_approve_mode_reads_no_review_history(self) -> None:
        """The extra paginated read is the antagonistic mode's alone."""
        self.gh.add("alpha", 1)
        self.run_main()
        self.assertFalse(any("/reviews?" in " ".join(c) for c in self.gh.calls))

    def test_three_rounds_cap_a_pull_request(self) -> None:
        """Three reviews from this App, on any heads, hand it to a maintainer;
        two leave room for a third round on a new head.
        """

        def own(commit: str) -> dict[str, Any]:
            return {
                "user": {
                    "login": "lf-releng-antagonistic-review-bot[bot]",
                    "type": "Bot",
                },
                "state": "COMMENTED",
                "commit_id": commit,
            }

        copilot = {
            "user": {"login": "copilot-pull-request-reviewer[bot]", "type": "Bot"},
            "state": "COMMENTED",
            "commit_id": HEAD,
            "body": CLEAN,
        }
        self.add_bot("alpha", 1)
        self.add_bot("beta", 2)
        self.gh.reviews[("org/alpha", 1)] = [copilot, *(own(c * 40) for c in "bcd")]
        self.gh.reviews[("org/beta", 2)] = [copilot, own(BASE), own(OTHER)]
        selection = self.run_antagonistic(
            "--bot-slug", "lf-releng-antagonistic-review-bot"
        )
        self.assertEqual([e["key"] for e in selection["pull_requests"]], ["beta-2"])
        self.assertEqual(selection["skip_counts"]["round_cap"], 1)

    def test_the_selection_records_who_can_take_a_hand_back(self) -> None:
        """The hand-back list travels in selection.json for the applier."""
        authors = self.authors.parent / "handback.txt"
        authors.write_text("lf-releng-code-monkey-bot[bot]\n", encoding="utf-8")
        selection = self.run_antagonistic("--handback-authors-file", str(authors))
        self.assertEqual(selection["handback_authors"], ["lf-releng-code-monkey-bot"])

    def test_a_pull_request_skipped_early_costs_no_history_read(self) -> None:
        """Closed or draft, a named bot pull request never walks its reviews."""
        self.add_bot("alpha", 1, state="CLOSED")
        self.add_bot("beta", 2, isDraft=True)
        self.run_antagonistic("--pull-requests", "alpha#1 beta#2")
        self.assertFalse(any("/reviews?" in " ".join(c) for c in self.gh.calls))

    def test_an_approve_tier_is_refused(self) -> None:
        """The mode approves nothing, so asking it to is an error."""
        with self.assertRaises(SystemExit) as caught:
            self.run_antagonistic("--approve-tiers", "trivial")
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("approves nothing", self.stderr.getvalue())

    def test_an_empty_author_list_is_refused(self) -> None:
        """With no listed bot the mode would review nothing; it says so."""
        self.authors.write_text("# nobody\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.run_antagonistic()
        self.assertIn("needs a bot author list", self.stderr.getvalue())

    def test_unknown_mode_is_refused(self) -> None:
        """Only the two modes are accepted."""
        with self.assertRaises(SystemExit) as caught:
            self.run_main("--mode", "approve-all")
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("mode must be one of", self.stderr.getvalue())

    def test_an_outside_bot_association_is_reviewed(self) -> None:
        """The antagonistic mode's author rule alone decides: listed bots pass."""
        for association in ("NONE", "CONTRIBUTOR"):
            with self.subTest(association=association):
                self.install(FakeGitHub())
                self.add_bot("alpha", 1, authorAssociation=association)
                selection = self.run_antagonistic()
                self.assertEqual(
                    [e["key"] for e in selection["pull_requests"]], ["alpha-1"]
                )
                self.assertEqual(selection["skip_counts"].get("untrusted_author", 0), 0)

    def test_an_untrusted_human_is_a_human_author(self) -> None:
        """An outside human skips under the antagonistic author rule, unchanged."""
        self.gh.add("alpha", 1, authorAssociation="NONE")
        selection = self.run_antagonistic()
        self.assert_only_skip(selection, "human_author")

    def test_approve_mode_still_skips_every_bot(self) -> None:
        """A listed bot is no exception in the approve mode."""
        self.add_bot("alpha", 1)
        selection, _, _ = self.run_main("--bot-authors-file", str(self.authors))
        self.assert_only_skip(selection, "bot_author")


if __name__ == "__main__":
    unittest.main()
