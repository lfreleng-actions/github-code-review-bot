# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The report job: results in, report and next ledger out."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib import import_module
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

report = import_module("review_report")
ledger = import_module("ledger")

SHA = "a" * 40
URL = "https://github.com/lfreleng-actions/repo/pull/157"


def make_result(**overrides: Any) -> dict[str, Any]:
    """A valid result.json object with overrides applied."""
    result: dict[str, Any] = {
        "schema": 1,
        "key": "repo-157",
        "repository": "lfreleng-actions/repo",
        "number": 157,
        "url": URL,
        "title": "Fix: Correct the thing",
        "head_sha": SHA,
        "verdict": "approved",
        "tier": "trivial",
        "reasons": [],
        "summary": "Pins one action.",
        "findings": [],
        "injection_attempts": [],
        "review_url": f"{URL}#pullrequestreview-1",
        "dry_run": False,
        "run_attempt": 1,
        "premium_requests": 1.5,
        "agent_seconds": 42,
    }
    result.update(overrides)
    return result


def make_selection(**overrides: Any) -> dict[str, Any]:
    """A selection with one pull request and two skips."""
    selection: dict[str, Any] = {
        "schema": 1,
        "dry_run": False,
        "candidates_seen": 13,
        "skipped": [
            {
                "repository": "lfreleng-actions/zeta",
                "number": 5,
                "url": "https://github.com/lfreleng-actions/zeta/pull/5",
                "title": "Draft work",
                "reason": "draft",
            },
            {
                "repository": "lfreleng-actions/alpha",
                "number": 9,
                "url": "https://github.com/lfreleng-actions/alpha/pull/9",
                "title": None,
                "reason": "ci_failing",
            },
        ],
        "pull_requests": [{"key": "repo-157", "author": "octocat"}],
    }
    selection.update(overrides)
    return selection


class OneLineTest(unittest.TestCase):
    """``one_line`` renders untrusted text as one harmless table cell."""

    def test_control_characters_and_whitespace_fold(self) -> None:
        """Newlines, tabs and C1 controls become single spaces."""
        self.assertEqual(report.one_line("a\nb\t\tc\x85d  e"), "a b c d e")

    def test_workflow_commands_and_mentions_escaped(self) -> None:
        """Nothing that a log or GitHub would act on survives."""
        self.assertEqual(report.one_line("::warning::x"), ": :warning: :x")
        self.assertEqual(report.one_line(":::"), ": : :")
        self.assertEqual(report.one_line("##[group]"), "# #[group]")
        self.assertEqual(report.one_line("hi @octocat"), "hi &#64;octocat")
        self.assertEqual(report.one_line("a|b"), "a\\|b")

    def test_truncation_marks_the_cut(self) -> None:
        """Long text is cut to the limit and ends in an ellipsis."""
        rendered = report.one_line("x" * 500, limit=20)
        self.assertEqual(len(rendered), 20)
        self.assertTrue(rendered.endswith("..."))

    def test_none_renders_empty(self) -> None:
        """A null title or summary is an empty cell, not ``None``."""
        self.assertEqual(report.one_line(None), "")


class CheckResultTest(unittest.TestCase):
    """``check_result`` admits the result.json contract alone."""

    def test_valid_result_returned(self) -> None:
        """A conforming result comes back unchanged."""
        result = make_result()
        self.assertIs(report.check_result(result), result)

    def test_invalid_shapes_rejected(self) -> None:
        """Each contract field is checked."""
        bad: list[dict[str, Any]] = [
            {"schema": 2},
            {"verdict": "maybe"},
            {"key": ""},
            {"repository": "no-slash"},
            {"number": 0},
            {"number": True},
            {"head_sha": "abc"},
            {"dry_run": "no"},
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                report.check_result(make_result(**overrides))
        with self.assertRaises(ValueError):
            report.check_result(["not", "an", "object"])


class LoadResultsTest(unittest.TestCase):
    """``load_results`` reads what it can and names the rest."""

    def setUp(self) -> None:
        """A results directory with one subdirectory per artifact."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)

    def place(self, name: str, content: bytes) -> Path:
        """Write ``name/result.json``."""
        directory = self.root / name
        directory.mkdir()
        path = directory / "result.json"
        path.write_bytes(content)
        return path

    def test_results_ordered_by_key(self) -> None:
        """Directory names do not decide the order; keys do."""
        self.place("z-dir", json.dumps(make_result(key="alpha-1")).encode())
        self.place("a-dir", json.dumps(make_result(key="beta-2")).encode())
        results, unreadable = report.load_results(self.root)
        self.assertEqual([r["key"] for r in results], ["alpha-1", "beta-2"])
        self.assertEqual(unreadable, [])

    def test_broken_files_noted_not_fatal(self) -> None:
        """Garbage, bad shape, oversize and symlinks are each a note."""
        self.place("good", json.dumps(make_result()).encode())
        self.place("garbage", b"{not json")
        self.place("shape", json.dumps(make_result(verdict="meh")).encode())
        self.place("big", b" " * (report.MAX_RESULT_BYTES + 1))
        (self.root / "linked").mkdir()
        (self.root / "linked" / "result.json").symlink_to(
            self.root / "good" / "result.json"
        )
        results, unreadable = report.load_results(self.root)
        self.assertEqual(len(results), 1)
        self.assertEqual(unreadable, ["big", "garbage", "linked", "shape"])

    def test_missing_directory_is_empty(self) -> None:
        """No results dir means no results, not a failure."""
        self.assertEqual(report.load_results(self.root / "absent"), ([], []))

    def test_subdirectories_without_a_result_are_ignored(self) -> None:
        """Only ``*/result.json`` counts; other files are not results."""
        (self.root / "other").mkdir()
        (self.root / "other" / "check.json").write_text("{}")
        self.assertEqual(report.load_results(self.root), ([], []))


class ReportShapeTest(unittest.TestCase):
    """Counts, spend and the rendered tables follow the contract."""

    def test_counts_and_spend(self) -> None:
        """Verdicts are counted; odd spend values are ignored."""
        results = [
            make_result(key="a-1"),
            make_result(key="b-2", verdict="needs-human", premium_requests=None),
            make_result(
                key="c-3",
                verdict="would-approve",
                premium_requests=float("inf"),
                agent_seconds=-5,
            ),
            make_result(key="d-4", verdict="failed", premium_requests=1.25),
        ]
        built = report.build_report(make_selection(), results, 7)
        self.assertEqual(
            built["counts"],
            {
                "approved": 1,
                "would-approve": 1,
                "needs-human": 1,
                "failed": 1,
                "skipped": 2,
                "candidates_seen": 13,
            },
        )
        self.assertEqual(built["premium_requests"], 2.75)
        self.assertEqual(built["agent_seconds"], 42 * 3)
        self.assertEqual(built["ledger_entries"], 7)
        self.assertEqual(
            [row["reason"] for row in built["skipped"]], ["ci_failing", "draft"]
        )

    def test_markdown_rows(self) -> None:
        """Each result row carries link, author, tier, verdict and detail."""
        results = [
            make_result(),
            make_result(
                key="repo-2",
                number=2,
                verdict="needs-human",
                review_url=None,
                reasons=["not corroborated ::x @bob"],
                url="javascript:alert(1)",
            ),
            make_result(
                key="repo-3", number=3, review_url=None, reasons=[], summary="S"
            ),
        ]
        selection = make_selection(dry_run=True)
        rendered = report.render_markdown(
            report.build_report(selection, results, 1), selection, ["dir-x"]
        )
        self.assertIn("# Code review run", rendered)
        self.assertIn("Dry run: yes", rendered)
        self.assertIn("1 result file(s) unreadable: dir-x", rendered)
        self.assertIn(
            f"| [lfreleng-actions/repo#157]({URL}) | Fix: Correct the thing | octocat"
            f" | trivial | approved | [review]({URL}#pullrequestreview-1) |",
            rendered,
        )
        self.assertIn("| lfreleng-actions/repo#2 | ", rendered)
        self.assertIn(
            "| ? | trivial | needs-human | not corroborated : :x &#64;bob |", rendered
        )
        self.assertIn("| approved | S |", rendered)
        self.assertIn("| ci_failing |", rendered)
        self.assertLess(rendered.index("ci_failing"), rendered.index("| draft |"))
        self.assertIn("Premium requests: 4.5; agent seconds: 126.", rendered)
        self.assertIn("Ledger entries: 1.", rendered)

    def test_empty_sections_say_none(self) -> None:
        """No results and no skips render as ``None.``, not empty tables."""
        selection = make_selection(skipped=[], pull_requests=[])
        rendered = report.render_markdown(
            report.build_report(selection, [], 0), selection, []
        )
        self.assertEqual(rendered.count("None."), 2)
        self.assertIn("Dry run: no", rendered)


class MainTest(unittest.TestCase):
    """The CLI writes all three outputs and carries the ledger forward."""

    def setUp(self) -> None:
        """Evidence, results and output locations under one directory."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.selection = self.root / "selection.json"
        self.selection.write_text(json.dumps(make_selection()))
        self.prior = self.root / "ledger.json"
        prior = ledger.empty_ledger()
        ledger.record(
            prior,
            make_result(key="old-1", number=1, head_sha="b" * 40),
            run_id=1,
        )
        self.prior.write_text(json.dumps(prior))
        self.results = self.root / "results"
        self.results.mkdir()

    def argv(self) -> list[str]:
        """The full argument list, run id 42."""
        out = self.root / "out"
        return [
            "--selection",
            str(self.selection),
            "--results",
            str(self.results),
            "--ledger",
            str(self.prior),
            "--run-id",
            "42",
            "--output-md",
            str(out / "report.md"),
            "--output-json",
            str(out / "report.json"),
            "--output-ledger",
            str(out / "ledger" / "ledger.json"),
        ]

    def test_outputs_written_and_ledger_extended(self) -> None:
        """Recorded verdicts join the prior ledger; failures do not."""
        for name, result in (
            ("r1", make_result()),
            ("r2", make_result(key="repo-9", number=9, verdict="failed")),
        ):
            (self.results / name).mkdir()
            (self.results / name / "result.json").write_text(json.dumps(result))
        with redirect_stdout(io.StringIO()) as out:
            report.main(self.argv())
        written = json.loads((self.root / "out" / "report.json").read_text())
        self.assertEqual(written["counts"]["approved"], 1)
        self.assertEqual(written["counts"]["failed"], 1)
        self.assertEqual(written["ledger_entries"], 2)
        new_ledger = json.loads(
            (self.root / "out" / "ledger" / "ledger.json").read_text()
        )
        self.assertEqual([e["run_id"] for e in new_ledger["entries"]], [1, 42])
        self.assertEqual(new_ledger["entries"][1]["head_sha"], SHA)
        self.assertIn("## Results", (self.root / "out" / "report.md").read_text())
        self.assertIn("2 result(s), 2 skipped", out.getvalue())

    def test_no_results_keeps_the_prior_ledger(self) -> None:
        """An absent results dir still yields a report and the same ledger."""
        argv = self.argv()
        argv[argv.index("--results") + 1] = str(self.root / "nowhere")
        with redirect_stdout(io.StringIO()):
            report.main(argv)
        new_ledger = json.loads(
            (self.root / "out" / "ledger" / "ledger.json").read_text()
        )
        self.assertEqual(new_ledger, json.loads(self.prior.read_text()))

    def test_unreadable_evidence_exits_one(self) -> None:
        """A missing or malformed selection or ledger is an operational failure."""
        self.prior.write_text('{"schema": 2, "entries": []}')
        with (
            redirect_stderr(io.StringIO()) as err,
            self.assertRaises(SystemExit) as caught,
        ):
            report.main(self.argv())
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("report: ", err.getvalue())
        self.selection.unlink()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            report.main(self.argv())
        self.assertEqual(caught.exception.code, 1)

    def test_run_id_must_be_positive(self) -> None:
        """Zero or negative run ids are a usage error."""
        argv = self.argv()
        argv[argv.index("--run-id") + 1] = "0"
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            report.main(argv)
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
