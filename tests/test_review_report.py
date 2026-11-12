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
binding = import_module("result_binding")
ledger = import_module("ledger")

SHA = "a" * 40
URL = "https://github.com/lfreleng-actions/repo/pull/157"
APPROVE = report.VERDICTS_BY_MODE["approve"]


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
        "mode": "approve",
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
        "pull_requests": [
            {
                "key": "repo-157",
                "author": "octocat",
                "repository": "lfreleng-actions/repo",
                "number": 157,
                "head_sha": SHA,
            },
            {
                "key": "repo-9",
                "author": "hubot",
                "repository": "lfreleng-actions/repo",
                "number": 9,
                "head_sha": SHA,
            },
        ],
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
        self.assertEqual(
            report.one_line("hi @octocat #4"), "hi @\u200boctocat #\u200b4"
        )
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
        self.assertIs(binding.check_result(result, APPROVE), result)

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
                binding.check_result(make_result(**overrides), APPROVE)
        with self.assertRaises(ValueError):
            binding.check_result(["not", "an", "object"], APPROVE)


class LoadResultsTest(unittest.TestCase):
    """``load_results`` reads what it can and names the rest."""

    def setUp(self) -> None:
        """A results directory with one subdirectory per artifact."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.artifacts: list[dict[str, Any]] = []

    def place(self, name: str, content: bytes, key: str = "repo-157") -> Path:
        """Write ``name/result.json`` and list it in the manifest."""
        directory = self.root / name
        directory.mkdir()
        path = directory / "result.json"
        path.write_bytes(content)
        self.artifacts.append(
            {"directory": name, "key": key, "attempt": 1, "verified": True}
        )
        (self.root / "provenance.json").write_text(
            json.dumps({"schema": 1, "artifacts": self.artifacts, "notes": []})
        )
        return path

    def test_results_ordered_by_key(self) -> None:
        """Directory names do not decide the order; keys do."""
        self.place(
            "z-dir", json.dumps(make_result(key="repo-9", number=9)).encode(), "repo-9"
        )
        self.place("a-dir", json.dumps(make_result()).encode())
        results, unreadable, _ = binding.load_results(
            self.root, make_selection(), APPROVE
        )
        self.assertEqual([r["key"] for r in results], ["repo-157", "repo-9"])
        self.assertEqual(unreadable, [])

    def test_broken_files_noted_not_fatal(self) -> None:
        """Garbage, bad shape, oversize and symlinks are each a note."""
        self.place("good", json.dumps(make_result()).encode())
        self.place("garbage", b"{not json")
        self.place("shape", json.dumps(make_result(verdict="meh")).encode())
        self.place("big", b" " * (binding.MAX_RESULT_BYTES + 1))
        (self.root / "linked").mkdir()
        (self.root / "linked" / "result.json").symlink_to(
            self.root / "good" / "result.json"
        )
        self.place("missing", b"{}")
        (self.root / "missing" / "result.json").unlink()
        self.artifacts.append(
            {"directory": "linked", "key": "repo-157", "attempt": 1, "verified": True}
        )
        (self.root / "provenance.json").write_text(
            json.dumps({"schema": 1, "artifacts": self.artifacts, "notes": []})
        )
        results, unreadable, _ = binding.load_results(
            self.root, make_selection(), APPROVE
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(
            sorted(unreadable), ["big", "garbage", "linked", "missing", "shape"]
        )

    def test_missing_directory_is_empty(self) -> None:
        """No results dir means no results, not a failure."""
        self.assertEqual(
            binding.load_results(self.root / "absent", make_selection(), APPROVE),
            ([], [], []),
        )

    def test_an_unreadable_manifest_trusts_nothing(self) -> None:
        """A manifest that cannot be read leaves every result out, with a note."""
        self.place("good", json.dumps(make_result()).encode())
        (self.root / "provenance.json").write_text("[]")
        self.assertEqual(
            binding.load_results(self.root, make_selection(), APPROVE),
            ([], [], ["provenance manifest unreadable"]),
        )


class ProvenanceTest(unittest.TestCase):
    """Results bind to the selection, and only verified ones are recorded."""

    def setUp(self) -> None:
        """A results directory laid out as result_fetch.py writes it."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.artifacts: list[dict[str, Any]] = []
        self.notes: list[str] = []

    def place(self, result: dict[str, Any], *, verified: bool, attempt: int = 1) -> str:
        """Write one artifact's result and its manifest entry."""
        key = str(result["key"])
        name = f"code-review-result-ns-{key}-{attempt}"
        (self.root / name).mkdir()
        (self.root / name / "result.json").write_text(json.dumps(result))
        self.artifacts.append(
            {
                "name": name,
                "directory": name,
                "key": key,
                "attempt": attempt,
                "verified": verified,
            }
        )
        (self.root / "provenance.json").write_text(
            json.dumps({"schema": 1, "artifacts": self.artifacts, "notes": self.notes})
        )
        return name

    def load(self, selection: dict[str, Any] | None = None) -> Any:
        """Load the results against the selection."""
        return binding.load_results(self.root, selection or make_selection(), APPROVE)

    def recorded(self, results: list[dict[str, Any]]) -> list[tuple[int, str]]:
        """The (number, verdict) pairs a ledger built from the results holds."""
        built = report.build_ledger(ledger.empty_ledger(), results, 42, "approve")
        return [(entry["number"], entry["verdict"]) for entry in built["entries"]]

    def test_a_verified_result_is_recorded(self) -> None:
        """A normal run's genuine results reach the ledger."""
        self.place(make_result(), verified=True)
        results, unreadable, notes = self.load()
        self.assertEqual((unreadable, notes), ([], []))
        self.assertTrue(results[0]["verified"])
        self.assertEqual(self.recorded(results), [(157, "approved")])

    def test_a_result_for_an_unselected_key_is_ignored(self) -> None:
        """A forged key outside the selection never reaches the report."""
        name = self.place(make_result(key="other-1", number=1), verified=True)
        results, unreadable, _ = self.load()
        self.assertEqual(results, [])
        self.assertEqual(unreadable, [name])

    def test_a_forged_result_for_a_selected_key_suppresses_nothing(self) -> None:
        """An unverified needs-human is shown but kept out of the ledger."""
        self.place(make_result(verdict="needs-human", review_url=None), verified=False)
        results, _, _ = self.load()
        self.assertEqual([r["verdict"] for r in results], ["needs-human"])
        self.assertFalse(results[0]["verified"])
        self.assertEqual(self.recorded(results), [])
        built = report.build_report(make_selection(), results, 0)
        self.assertEqual(built["unverified"], 1)
        rendered = report.render_markdown(built, make_selection(), [])
        self.assertIn("1 result(s) unverified, kept out of the ledger", rendered)

    def test_a_forged_verified_flag_in_the_file_is_overruled(self) -> None:
        """Only the manifest says verified; the result file cannot."""
        self.place(make_result(verified=True), verified=False)
        results, _, _ = self.load()
        self.assertFalse(results[0]["verified"])

    def test_a_result_must_match_the_selected_pull_request(self) -> None:
        """Repository, number, head and attempt all have to agree."""
        for overrides in (
            {"repository": "lfreleng-actions/other"},
            {"number": 158},
            {"head_sha": "b" * 40},
            {"run_attempt": 2},
            {"run_attempt": True},
        ):
            with self.subTest(overrides=overrides):
                for child in self.root.iterdir():
                    if child.is_dir():
                        (child / "result.json").unlink()
                        child.rmdir()
                self.artifacts.clear()
                self.place(make_result(**overrides), verified=True)
                results, unreadable, _ = self.load()
                self.assertEqual(results, [])
                self.assertEqual(len(unreadable), 1)

    def test_dry_run_comes_from_the_selection(self) -> None:
        """A result cannot make a live run's verdict look like a dry one."""
        self.place(make_result(dry_run=True), verified=True)
        results, _, _ = self.load(make_selection(dry_run=False))
        self.assertIs(results[0]["dry_run"], False)

    def test_a_legitimate_retry_is_recorded_once(self) -> None:
        """Two verified attempts of one key: the latest stands, once."""
        self.place(make_result(verdict="needs-human", run_attempt=1), verified=True)
        self.place(make_result(run_attempt=2), verified=True, attempt=2)
        results, _, _ = self.load()
        self.assertEqual(
            [(r["run_attempt"], r["verdict"]) for r in results], [(2, "approved")]
        )
        self.assertEqual(self.recorded(results), [(157, "approved")])

    def test_a_later_forgery_cannot_displace_a_verified_result(self) -> None:
        """The latest verified attempt wins over a later unverified one."""
        self.place(make_result(run_attempt=1), verified=True)
        self.place(
            make_result(verdict="needs-human", run_attempt=2),
            verified=False,
            attempt=2,
        )
        results, _, _ = self.load()
        self.assertEqual(
            [(r["run_attempt"], r["verified"]) for r in results], [(1, True)]
        )

    def test_a_jobs_listing_failure_records_nothing(self) -> None:
        """With provenance unknown, every result stays out of the ledger."""
        self.notes.append("jobs listing failed, so no result is verified: HTTP 403")
        self.place(make_result(), verified=False)
        self.place(make_result(key="repo-9", number=9), verified=False)
        results, _, notes = self.load()
        self.assertEqual(len(results), 2)
        self.assertEqual(self.recorded(results), [])
        rendered = report.render_markdown(
            report.build_report(make_selection(), results, 0),
            make_selection(),
            [],
            notes,
        )
        self.assertIn("- jobs listing failed", rendered)

    def test_no_manifest_means_no_results(self) -> None:
        """A result.json without the gather's manifest is never read."""
        (self.root / "loose").mkdir()
        (self.root / "loose" / "result.json").write_text(json.dumps(make_result()))
        self.assertEqual(self.load(), ([], [], []))

    def test_a_manifest_directory_must_be_a_plain_name(self) -> None:
        """A manifest cannot point the read outside the results directory."""
        self.artifacts.append(
            {"directory": "../x", "key": "repo-157", "attempt": 1, "verified": True}
        )
        (self.root / "provenance.json").write_text(
            json.dumps({"schema": 1, "artifacts": self.artifacts, "notes": []})
        )
        results, unreadable, _ = self.load()
        self.assertEqual((results, unreadable), ([], ["../x"]))


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

    def test_stale_statuses_are_reported(self) -> None:
        """Each entry judged without a stale status is listed, others are not."""
        selection = make_selection()
        selection["pull_requests"][0].update(
            url=URL, stale_statuses=["pre-commit.ci - pr"]
        )
        selection["pull_requests"][1]["stale_statuses"] = []
        built = report.build_report(selection, [make_result()], 0)
        self.assertEqual(
            built["stale_statuses"],
            [
                {
                    "repository": "lfreleng-actions/repo",
                    "number": 157,
                    "url": URL,
                    "contexts": ["pre-commit.ci - pr"],
                }
            ],
        )
        rendered = report.render_markdown(built, selection, [])
        self.assertIn("## Stale statuses", rendered)
        self.assertIn(
            f"| [lfreleng-actions/repo#\u200b157]({URL}) | pre-commit.ci - pr |",
            rendered,
        )

    def test_no_stale_status_means_no_section(self) -> None:
        """A run that discounted nothing reports an empty list and no section."""
        built = report.build_report(make_selection(), [make_result()], 0)
        self.assertEqual(built["stale_statuses"], [])
        rendered = report.render_markdown(built, make_selection(), [])
        self.assertNotIn("Stale statuses", rendered)

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
        self.assertIn("Mode: approve; dry run: yes", rendered)
        self.assertIn("1 result file(s) unreadable: dir-x", rendered)
        self.assertIn(
            f"| [lfreleng-actions/repo#\u200b157]({URL}) | Fix: Correct the thing"
            f" | octocat | trivial | approved | [review]({URL}#pullrequestreview-1) |",
            rendered,
        )
        self.assertIn("| lfreleng-actions/repo#\u200b2 | ", rendered)
        self.assertIn(
            "| ? | trivial | needs-human | not corroborated : :x @\u200bbob |", rendered
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
        self.assertIn("Mode: approve; dry run: no", rendered)


class AntagonisticReportTest(unittest.TestCase):
    """The antagonistic mode counts its own verdicts and remembers posted reviews."""

    def test_verdicts_ledger_and_comment_column(self) -> None:
        """commented and would-comment are remembered; refused and failed are not."""
        selection = make_selection(mode="antagonistic")
        results = [
            make_result(key="a-1", number=1, verdict="commented", comment_count=3),
            make_result(key="b-2", number=2, verdict="would-comment", comment_count=0),
            make_result(key="c-3", number=3, verdict="refused", review_url=None),
            make_result(key="d-4", number=4, verdict="failed", review_url=None),
        ]
        for result in results:
            result["verified"] = True
        built = report.build_report(selection, results, 0)
        self.assertEqual(built["mode"], "antagonistic")
        self.assertEqual(
            {k: built["counts"][k] for k in report.VERDICTS_BY_MODE["antagonistic"]},
            {"commented": 1, "would-comment": 1, "refused": 1, "failed": 1},
        )
        ledger_after = report.build_ledger(
            ledger.empty_ledger(), results, 42, "antagonistic"
        )
        self.assertEqual(
            [entry["verdict"] for entry in ledger_after["entries"]],
            ["commented", "would-comment"],
        )
        rendered = report.render_markdown(built, selection, [])
        self.assertIn("Mode: antagonistic; dry run: no", rendered)
        self.assertIn("| Author | Comments | Verdict |", rendered)
        self.assertIn("| ? | 3 | commented |", rendered)
        self.assertIn("| ? | ? | refused |", rendered)

    def test_a_settled_refusal_is_recorded_and_no_other(self) -> None:
        """The session's own empty verdict skips the head; a gate or failure does not."""
        results = [
            make_result(key="a-1", number=1, verdict="refused", settled=True),
            make_result(key="b-2", number=2, verdict="refused", settled=False),
            make_result(key="c-3", number=3, verdict="refused"),
            make_result(key="d-4", number=4, verdict="failed", settled=True),
        ]
        for result in results:
            result["verified"] = True
        ledger_after = report.build_ledger(
            ledger.empty_ledger(), results, 42, "antagonistic"
        )
        self.assertEqual(
            [(e["number"], e["verdict"]) for e in ledger_after["entries"]],
            [(1, "refused")],
        )

    def test_each_mode_refuses_the_other_modes_verdicts(self) -> None:
        """An approval cannot hide in an antagonistic report, nor the reverse."""
        with self.assertRaises(ValueError):
            binding.check_result(
                make_result(verdict="approved"),
                report.VERDICTS_BY_MODE["antagonistic"],
            )
        with self.assertRaises(ValueError):
            binding.check_result(make_result(verdict="commented"), APPROVE)

    def test_a_selection_without_a_known_mode_is_refused(self) -> None:
        """The report cannot guess which verdicts and ledger apply."""
        for mode in (None, "approve-all"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                report.mode_of(make_selection(mode=mode))


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
            recorded=report.RECORDED_BY_MODE["approve"],
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
        artifacts: list[dict[str, Any]] = []
        for name, result in (
            ("r1", make_result()),
            ("r2", make_result(key="repo-9", number=9, verdict="failed")),
        ):
            (self.results / name).mkdir()
            (self.results / name / "result.json").write_text(json.dumps(result))
            artifacts.append(
                {
                    "directory": name,
                    "key": result["key"],
                    "attempt": 1,
                    "verified": True,
                }
            )
        (self.results / "provenance.json").write_text(
            json.dumps({"schema": 1, "artifacts": artifacts, "notes": []})
        )
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
        self.assertIn("2 result(s), 0 unverified, 2 skipped", out.getvalue())

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
