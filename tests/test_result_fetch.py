# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The report job's bounded, provenance-checked gather of result artifacts."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

github = import_module("bot_github")
fetcher = import_module("artifact_fetch")

REPOSITORY = "lfreleng-actions/caller"
RUN_ID = 77
NAMESPACE = "77-1-abc123"
PREFIX = f"code-review-result-{NAMESPACE}-"
KEYS = {"repo-157", "repo-158"}
RESULT = b'{"schema": 1}'


def gather_module() -> ModuleType:
    """The module under test, imported when a test runs."""
    return import_module("result_fetch")


def artifact(
    name: str, created: str, artifact_id: int = 1, size: int = 100
) -> dict[str, Any]:
    """One entry of the run's artifact listing."""
    return {
        "id": artifact_id,
        "name": name,
        "size_in_bytes": size,
        "created_at": created,
        "expired": False,
    }


def apply_job(
    key: str,
    start: str,
    end: str,
    *,
    job_id: int = 500,
    attempt: int = 1,
    caller: str | None = "Code Review",
) -> dict[str, Any]:
    """An apply leg as the jobs API lists it, under the caller's job name."""
    name = f"Apply ({key})" if caller is None else f"{caller} / Apply ({key})"
    return {
        "id": job_id,
        "name": name,
        "run_attempt": attempt,
        "steps": [
            {
                "name": "Apply verdict",
                "started_at": "2026-11-05T10:00:00Z",
                "completed_at": start,
            },
            {"name": "Attach apply result", "started_at": start, "completed_at": end},
        ],
    }


def review_job(key: str, job_id: int = 400, attempt: int = 1) -> dict[str, Any]:
    """A review leg: untrusted, with no apply step, done before any apply."""
    return {
        "id": job_id,
        "name": f"Code Review / Review ({key})",
        "run_attempt": attempt,
        "started_at": "2026-11-05T09:00:00Z",
        "completed_at": "2026-11-05T09:00:06Z",
        "steps": [
            {
                "name": "Attach session",
                "started_at": "2026-11-05T09:00:00Z",
                "completed_at": "2026-11-05T09:00:05Z",
            }
        ],
    }


class FakeGitHub:
    """Serves the artifact and per-attempt jobs listings ``gh`` would."""

    def __init__(
        self,
        artifacts: list[dict[str, Any]],
        jobs: dict[int, list[dict[str, Any]]],
        *,
        jobs_fail: bool = False,
        artifacts_fail: bool = False,
    ) -> None:
        """Hold the listings, and whether either read fails."""
        self.artifacts = artifacts
        self.jobs = jobs
        self.jobs_fail = jobs_fail
        self.artifacts_fail = artifacts_fail
        self.endpoints: list[str] = []

    def run_gh(self, args: list[str], **_: Any) -> str:
        """Answer one ``gh api <endpoint> --paginate --slurp`` call."""
        endpoint = args[1]
        self.endpoints.append(endpoint)
        if "/artifacts" in endpoint:
            if self.artifacts_fail:
                raise github.GitHubError("HTTP 502", 502)
            # Two pages, to prove each is read.
            half = len(self.artifacts) // 2
            return json.dumps(
                [
                    {"total_count": 0, "artifacts": self.artifacts[:half]},
                    {"total_count": 0, "artifacts": self.artifacts[half:]},
                ]
            )
        if self.jobs_fail:
            raise github.GitHubError("HTTP 403", 403)
        attempt = int(endpoint.split("/attempts/")[1].split("/")[0])
        return json.dumps([{"total_count": 0, "jobs": self.jobs.get(attempt, [])}])


def write_files(
    _repository: str, _artifact_id: int, _size: int, output: Path, _profile: Any
) -> list[str]:
    """Stand in for the bounded fetch: write a result and report it."""
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.json").write_bytes(RESULT)
    return ["result.json"]


class StreamedZip:
    """A ``gh`` process that streams ``payload`` as the artifact's zip."""

    def __init__(self, payload: bytes) -> None:
        """Hold the bytes the download reads."""
        self.stdout = io.BytesIO(payload)
        self.returncode = 0

    def kill(self) -> None:
        """Nothing to stop."""

    def wait(self, timeout: float | None = None) -> int:
        """Already done."""
        return 0


class GatherCase(unittest.TestCase):
    """Runs ``gather`` against fakes in a scratch directory."""

    def setUp(self) -> None:
        """A scratch output directory."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.output = Path(holder.name) / "results"

    def gather(
        self, fake: FakeGitHub, attempts: int = 1, keys: set[str] | None = None
    ) -> dict[str, Any]:
        """Run the gather with the fetch stood in."""
        module = gather_module()
        with (
            patch.object(github, "run_gh", fake.run_gh),
            patch.object(module.artifact_fetch, "fetch_artifact", write_files),
        ):
            return module.gather(
                REPOSITORY, RUN_ID, attempts, NAMESPACE, keys or KEYS, self.output
            )

    def verdicts(self, manifest: dict[str, Any]) -> dict[str, bool]:
        """Each fetched artifact's name and whether it was verified."""
        return {entry["name"]: entry["verified"] for entry in manifest["artifacts"]}


class ProvenanceTest(GatherCase):
    """An artifact is verified only inside its own apply leg's upload step."""

    @unittest.expectedFailure
    def test_a_normal_run_verifies_every_genuine_result(self) -> None:
        """Both job-name forms match, so a real run records its results."""
        fake = FakeGitHub(
            [
                artifact(f"{PREFIX}repo-157-1", "2026-11-05T10:01:02Z", 1),
                artifact(f"{PREFIX}repo-158-1", "2026-11-05T10:02:02Z", 2),
                artifact("code-review-session-other", "2026-11-05T09:00:00Z", 3),
            ],
            {
                1: [
                    review_job("repo-157"),
                    apply_job(
                        "repo-157", "2026-11-05T10:01:00Z", "2026-11-05T10:01:04Z"
                    ),
                    apply_job(
                        "repo-158",
                        "2026-11-05T10:02:00Z",
                        "2026-11-05T10:02:03Z",
                        job_id=501,
                        caller=None,
                    ),
                ]
            },
        )
        manifest = self.gather(fake)
        self.assertEqual(
            self.verdicts(manifest),
            {f"{PREFIX}repo-157-1": True, f"{PREFIX}repo-158-1": True},
        )
        self.assertEqual(manifest["notes"], [])
        self.assertEqual(
            (self.output / f"{PREFIX}repo-157-1" / "result.json").read_bytes(), RESULT
        )
        self.assertIn(
            f"repos/{REPOSITORY}/actions/runs/{RUN_ID}/attempts/1/jobs?per_page=100",
            fake.endpoints,
        )

    @unittest.expectedFailure
    def test_job_names_match_exactly_or_by_the_caller_suffix(self) -> None:
        """A name that merely contains the apply name is not the apply leg."""
        module = gather_module()
        keys = {"repo-157"}
        self.assertEqual(module.job_key("Apply (repo-157)", keys), "repo-157")
        self.assertEqual(
            module.job_key("Code Review / Apply (repo-157)", keys), "repo-157"
        )
        for name in (
            "Review (repo-157) Apply (repo-157)",
            "Code Review / Apply (repo-157) ",
            "Code Review/Apply (repo-157)",
            "Apply (repo-1571)",
            "Apply (repo-157",
            None,
        ):
            with self.subTest(name=name):
                self.assertIsNone(module.job_key(name, keys))

    @unittest.expectedFailure
    def test_a_forgery_before_the_apply_step_is_unverified(self) -> None:
        """A review leg's upload under a selected key cannot be trusted."""
        fake = FakeGitHub(
            [artifact(f"{PREFIX}repo-157-1", "2026-11-05T09:00:03Z")],
            {
                1: [
                    review_job("repo-157"),
                    apply_job(
                        "repo-157", "2026-11-05T10:01:00Z", "2026-11-05T10:01:04Z"
                    ),
                ]
            },
        )
        self.assertEqual(
            self.verdicts(self.gather(fake)), {f"{PREFIX}repo-157-1": False}
        )

    @unittest.expectedFailure
    def test_skew_is_tolerated_and_no_further(self) -> None:
        """A few seconds either side of the step count; more does not."""
        module = gather_module()
        start = module.timestamp("2026-11-05T10:01:00Z")
        end = module.timestamp("2026-11-05T10:01:04Z")
        windows = [(start, end)]
        for created, expected in (
            ("2026-11-05T10:00:55Z", True),
            ("2026-11-05T10:01:09Z", True),
            ("2026-11-05T10:00:54Z", False),
            ("2026-11-05T10:01:10Z", False),
        ):
            with self.subTest(created=created):
                self.assertIs(
                    module.verified(module.timestamp(created), windows), expected
                )
        self.assertFalse(module.verified(None, windows))
        self.assertIsNone(module.timestamp("2026-11-05T10:01:00"))

    @unittest.expectedFailure
    def test_another_keys_window_does_not_verify(self) -> None:
        """A name claiming one key gains nothing from another key's window."""
        fake = FakeGitHub(
            [artifact(f"{PREFIX}repo-157-1", "2026-11-05T10:02:01Z")],
            {
                1: [
                    apply_job(
                        "repo-157", "2026-11-05T10:01:00Z", "2026-11-05T10:01:04Z"
                    ),
                    apply_job(
                        "repo-158",
                        "2026-11-05T10:02:00Z",
                        "2026-11-05T10:02:03Z",
                        job_id=501,
                    ),
                ]
            },
        )
        self.assertEqual(
            self.verdicts(self.gather(fake)), {f"{PREFIX}repo-157-1": False}
        )

    @unittest.expectedFailure
    def test_a_review_rerun_cannot_replace_another_keys_result(self) -> None:
        """A second attempt's review leg re-uploads key 157's attempt-1 name.

        With ``overwrite`` the forgery replaces the genuine artifact, but
        it is created in attempt 2's review, outside attempt 1's apply
        window, so it is unverified; and attempt 2's own apply result
        for 157 is the one that verifies.
        """
        fake = FakeGitHub(
            [
                artifact(f"{PREFIX}repo-157-1", "2026-11-05T11:00:03Z", 9),
                artifact(f"{PREFIX}repo-157-2", "2026-11-05T11:05:02Z", 10),
            ],
            {
                1: [
                    apply_job(
                        "repo-157", "2026-11-05T10:01:00Z", "2026-11-05T10:01:04Z"
                    ),
                ],
                2: [
                    review_job("repo-158", job_id=600, attempt=2),
                    apply_job(
                        "repo-157",
                        "2026-11-05T11:05:00Z",
                        "2026-11-05T11:05:04Z",
                        job_id=601,
                        attempt=2,
                    ),
                ],
            },
        )
        self.assertEqual(
            self.verdicts(self.gather(fake, attempts=2)),
            {f"{PREFIX}repo-157-2": True, f"{PREFIX}repo-157-1": False},
        )

    @unittest.expectedFailure
    def test_a_job_listed_again_keeps_its_own_attempt(self) -> None:
        """An attempt-1 leg listed under attempt 2 still verifies attempt 1 alone."""
        leg = apply_job("repo-157", "2026-11-05T10:01:00Z", "2026-11-05T10:01:04Z")
        fake = FakeGitHub(
            [
                artifact(f"{PREFIX}repo-157-1", "2026-11-05T10:01:02Z", 1),
                artifact(f"{PREFIX}repo-157-2", "2026-11-05T10:01:02Z", 2),
            ],
            {1: [leg], 2: [leg]},
        )
        self.assertEqual(
            self.verdicts(self.gather(fake, attempts=2)),
            {f"{PREFIX}repo-157-1": True, f"{PREFIX}repo-157-2": False},
        )

    @unittest.expectedFailure
    def test_a_jobs_listing_failure_verifies_nothing(self) -> None:
        """Fail closed: the results are fetched, noted, and none is verified."""
        fake = FakeGitHub(
            [artifact(f"{PREFIX}repo-157-1", "2026-11-05T10:01:02Z")],
            {},
            jobs_fail=True,
        )
        manifest = self.gather(fake)
        self.assertEqual(self.verdicts(manifest), {f"{PREFIX}repo-157-1": False})
        self.assertTrue(
            any("jobs listing failed" in note for note in manifest["notes"])
        )

    @unittest.expectedFailure
    def test_a_skipped_upload_step_verifies_nothing(self) -> None:
        """An apply leg whose upload step never ran has no window."""
        leg = apply_job("repo-157", "2026-11-05T10:01:00Z", "2026-11-05T10:01:04Z")
        leg["steps"][1]["started_at"] = None
        fake = FakeGitHub(
            [artifact(f"{PREFIX}repo-157-1", "2026-11-05T10:01:02Z")], {1: [leg]}
        )
        self.assertEqual(
            self.verdicts(self.gather(fake)), {f"{PREFIX}repo-157-1": False}
        )

    @unittest.expectedFailure
    def test_review_legs_match_under_any_key_or_caller(self) -> None:
        """Every invocation's review legs count, whatever key they carry."""
        module = gather_module()
        for name in (
            "Review (repo-157)",
            "Code Review / Review (other-9)",
            "Plumbing (dry) / Review (x)",
            "Review (x) / Review (repo-157)",
        ):
            with self.subTest(name=name):
                self.assertTrue(module.review_leg(name))
        for other in (
            "Reviewer (repo-157)",
            "Code Review / Review (repo-157) ",
            "Code Review/Review (repo-157)",
            "Review ()",
            "Apply (repo-157)",
            "Review (x) / Apply (repo-157)",
            "Agent outcomes",
            None,
        ):
            with self.subTest(name=other):
                self.assertFalse(module.review_leg(other))

    @unittest.expectedFailure
    def test_another_invocations_review_leg_unverifies_an_upload(self) -> None:
        """Two calls in one run: a review leg overlapping the upload wins nothing.

        One invocation's apply leg uploads while another invocation's
        review leg, under its own caller job and key, is still running.
        That leg could have replaced the artifact, so it is unverified;
        the same upload with the review leg finished first verifies.
        """
        name = f"{PREFIX}repo-157-1"
        upload = apply_job(
            "repo-157",
            "2026-11-05T10:01:00Z",
            "2026-11-05T10:01:04Z",
            caller="Plumbing (live)",
        )

        def other_review(start: str | None, end: str | None) -> dict[str, Any]:
            leg = review_job("other-9", job_id=700)
            leg["name"] = "Plumbing (dry) / Review (other-9)"
            leg["started_at"] = start
            leg["completed_at"] = end
            return leg

        for start, end, expected in (
            ("2026-11-05T10:00:30Z", "2026-11-05T10:01:30Z", False),
            ("2026-11-05T10:00:30Z", None, False),
            ("2026-11-05T10:00:30Z", "2026-11-05T10:00:57Z", False),
            ("2026-11-05T10:01:08Z", "2026-11-05T10:02:00Z", False),
            (None, "2026-11-05T10:00:10Z", False),
            ("2026-11-05T10:00:30Z", "2026-11-05T10:00:54Z", True),
            ("2026-11-05T10:01:10Z", "2026-11-05T10:02:00Z", True),
        ):
            with self.subTest(start=start, end=end):
                fake = FakeGitHub(
                    [artifact(name, "2026-11-05T10:01:02Z")],
                    {1: [other_review(start, end), upload]},
                )
                manifest = self.gather(fake)
                self.assertEqual(self.verdicts(manifest), {name: expected})
                self.assertEqual(
                    any("review leg" in note for note in manifest["notes"]),
                    not expected,
                )

    @unittest.expectedFailure
    def test_a_review_leg_in_an_earlier_attempt_counts(self) -> None:
        """A review leg listed only under attempt 1 still blocks attempt 2."""
        leg = review_job("other-9", job_id=700)
        leg["started_at"] = "2026-11-05T11:00:00Z"
        leg["completed_at"] = None
        fake = FakeGitHub(
            [artifact(f"{PREFIX}repo-157-2", "2026-11-05T11:05:02Z")],
            {
                1: [leg],
                2: [
                    apply_job(
                        "repo-157",
                        "2026-11-05T11:05:00Z",
                        "2026-11-05T11:05:04Z",
                        job_id=601,
                        attempt=2,
                    )
                ],
            },
        )
        self.assertEqual(
            self.verdicts(self.gather(fake, attempts=2)),
            {f"{PREFIX}repo-157-2": False},
        )

    @unittest.expectedFailure
    def test_a_jobs_listing_not_read_in_full_verifies_nothing(self) -> None:
        """An entry that is not a job could hide a review leg."""
        fake = FakeGitHub(
            [artifact(f"{PREFIX}repo-157-1", "2026-11-05T10:01:02Z")],
            {
                1: [
                    "Code Review / Review (other-9)",  # type: ignore[list-item]
                    apply_job(
                        "repo-157", "2026-11-05T10:01:00Z", "2026-11-05T10:01:04Z"
                    ),
                ]
            },
        )
        manifest = self.gather(fake)
        self.assertEqual(self.verdicts(manifest), {f"{PREFIX}repo-157-1": False})
        self.assertTrue(
            any("jobs listing failed" in note for note in manifest["notes"])
        )


class BoundTest(GatherCase):
    """Only selected keys are fetched, within a bound, and never unbounded."""

    @unittest.expectedFailure
    def test_an_unselected_key_is_never_fetched(self) -> None:
        """A forged name for a key outside the selection is noted and ignored."""
        fake = FakeGitHub(
            [
                artifact(f"{PREFIX}repo-999-1", "2026-11-05T10:01:02Z"),
                artifact(f"{PREFIX}repo-157-7", "2026-11-05T10:01:02Z"),
                artifact(f"{PREFIX}repo-157-x", "2026-11-05T10:01:02Z"),
            ],
            {1: []},
        )
        manifest = self.gather(fake)
        self.assertEqual(manifest["artifacts"], [])
        self.assertFalse(self.output.exists() and any(self.output.iterdir()))
        self.assertEqual(len(manifest["notes"]), 2)

    @unittest.expectedFailure
    def test_artifacts_past_the_bound_are_refused(self) -> None:
        """One per selected key per attempt, plus a margin, and no more."""
        module = gather_module()
        listing = [
            artifact(f"{PREFIX}repo-157-1", f"2026-11-05T10:01:{n:02d}Z", n)
            for n in range(10)
        ]
        notes: list[str] = []
        chosen = module.candidates(listing, NAMESPACE, {"repo-157"}, 1, notes)
        self.assertEqual(len(chosen), 1 + module.MARGIN)
        self.assertEqual(chosen[0][0]["id"], 9)
        self.assertEqual(len(notes), 10 - 1 - module.MARGIN)
        self.assertTrue(all("over the bound" in note for note in notes))

    @unittest.expectedFailure
    def test_an_oversized_compressible_result_is_refused_unextracted(self) -> None:
        """A small zip that would expand past the cap writes nothing."""
        module = gather_module()
        bomb = io.BytesIO()
        with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("result.json", b"0" * (module.MAX_RESULT_BYTES * 4))
        payload = bomb.getvalue()
        self.assertLess(len(payload), module.MAX_RESULT_BYTES)
        entry = artifact(
            f"{PREFIX}repo-157-1", "2026-11-05T10:01:02Z", size=len(payload)
        )
        target = self.output / str(entry["name"])
        with patch.object(
            fetcher.subprocess, "Popen", return_value=StreamedZip(payload)
        ):
            outcome = module.fetch_one(REPOSITORY, entry, target)
        self.assertIsInstance(outcome, str)
        self.assertTrue(outcome.startswith("refused"))
        self.assertFalse((target / "result.json").exists())

    @unittest.expectedFailure
    def test_an_unsupported_zip_version_is_refused(self) -> None:
        """zipfile's NotImplementedError is a refusal, not a crash."""
        module = gather_module()
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("result.json", RESULT)
        payload = bytearray(archive.getvalue())
        # The central directory's "version needed to extract", past 6.3.
        payload[payload.index(b"PK\x01\x02") + 6] = 99
        entry = artifact(
            f"{PREFIX}repo-157-1", "2026-11-05T10:01:02Z", size=len(payload)
        )
        target = self.output / str(entry["name"])
        with patch.object(
            fetcher.subprocess, "Popen", return_value=StreamedZip(bytes(payload))
        ):
            outcome = module.fetch_one(REPOSITORY, entry, target)
        self.assertIsInstance(outcome, str)
        self.assertTrue(outcome.startswith("refused"))
        self.assertFalse((target / "result.json").exists())

    @unittest.expectedFailure
    def test_an_invalid_utf8_filename_is_refused(self) -> None:
        """A UTF-8-flagged name that does not decode is a refusal."""
        module = gather_module()
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("result.json", RESULT)
        payload = bytearray(archive.getvalue())
        central = payload.index(b"PK\x01\x02")
        # Set the UTF-8 flag and make the name's first byte invalid.
        payload[central + 9] |= 0x08
        payload[central + 46] = 0xFF
        entry = artifact(
            f"{PREFIX}repo-157-1", "2026-11-05T10:01:02Z", size=len(payload)
        )
        target = self.output / str(entry["name"])
        with patch.object(
            fetcher.subprocess, "Popen", return_value=StreamedZip(bytes(payload))
        ):
            outcome = module.fetch_one(REPOSITORY, entry, target)
        self.assertIsInstance(outcome, str)
        self.assertTrue(str(outcome).startswith("refused: UnicodeDecodeError"))
        self.assertFalse((target / "result.json").exists())

    @unittest.expectedFailure
    def test_an_unexpected_exception_refuses_one_artifact(self) -> None:
        """Any exception from one fetch is a refusal; the rest still stand."""
        module = gather_module()

        class Unforeseen(Exception):
            """An exception no handler names."""

        fake = FakeGitHub(
            [
                artifact(f"{PREFIX}repo-157-1", "2026-11-05T10:01:02Z", 1),
                artifact(f"{PREFIX}repo-158-1", "2026-11-05T10:01:02Z", 2),
            ],
            {
                1: [
                    apply_job(
                        "repo-158", "2026-11-05T10:01:00Z", "2026-11-05T10:01:04Z"
                    )
                ]
            },
        )

        def broken(
            repository: str, artifact_id: int, size: int, output: Path, profile: Any
        ) -> list[str]:
            if artifact_id == 1:
                raise Unforeseen("surprise")
            return write_files(repository, artifact_id, size, output, profile)

        with (
            patch.object(github, "run_gh", fake.run_gh),
            patch.object(module.artifact_fetch, "fetch_artifact", broken),
        ):
            manifest = module.gather(
                REPOSITORY, RUN_ID, 1, NAMESPACE, KEYS, self.output
            )
        self.assertEqual(self.verdicts(manifest), {f"{PREFIX}repo-158-1": True})
        self.assertIn(
            f"{PREFIX}repo-157-1: refused: Unforeseen: 'surprise'", manifest["notes"]
        )

    @unittest.expectedFailure
    def test_an_interrupt_still_stops_the_fetch(self) -> None:
        """Only Exception is caught: KeyboardInterrupt and SystemExit escape."""
        module = gather_module()
        entry = artifact(f"{PREFIX}repo-157-1", "2026-11-05T10:01:02Z")
        for stop in (KeyboardInterrupt(), SystemExit(3)):
            with (
                self.subTest(stop=type(stop).__name__),
                patch.object(module.artifact_fetch, "fetch_artifact", side_effect=stop),
                self.assertRaises(type(stop)),
            ):
                module.fetch_one(REPOSITORY, entry, self.output / "x")

    @unittest.expectedFailure
    def test_a_reported_oversize_is_never_downloaded(self) -> None:
        """GitHub's own size stops the fetch before any transfer."""
        module = gather_module()
        limit = fetcher.zip_limit(module.RESULT_FILES)
        entry = artifact(f"{PREFIX}repo-157-1", "2026-11-05T10:01:02Z", size=limit + 1)
        with patch.object(fetcher, "download") as download:
            outcome = module.fetch_one(REPOSITORY, entry, self.output / "x")
        self.assertTrue(str(outcome).startswith("refused"))
        download.assert_not_called()

    @unittest.expectedFailure
    def test_a_failed_fetch_is_noted_and_the_rest_stand(self) -> None:
        """One artifact that cannot be fetched never fails the gather."""
        module = gather_module()
        fake = FakeGitHub(
            [
                artifact(f"{PREFIX}repo-157-1", "2026-11-05T10:01:02Z", 1),
                artifact(f"{PREFIX}repo-158-1", "2026-11-05T10:01:02Z", 2),
            ],
            {
                1: [
                    apply_job(
                        "repo-158", "2026-11-05T10:01:00Z", "2026-11-05T10:01:04Z"
                    )
                ]
            },
        )

        def flaky(
            repository: str, artifact_id: int, size: int, output: Path, profile: Any
        ) -> list[str]:
            if artifact_id == 1:
                raise github.GitHubError("HTTP 404", 404)
            return write_files(repository, artifact_id, size, output, profile)

        with (
            patch.object(github, "run_gh", fake.run_gh),
            patch.object(module.artifact_fetch, "fetch_artifact", flaky),
        ):
            manifest = module.gather(
                REPOSITORY, RUN_ID, 1, NAMESPACE, KEYS, self.output
            )
        self.assertEqual(self.verdicts(manifest), {f"{PREFIX}repo-158-1": True})
        self.assertTrue(
            any("refused: GitHubError" in note for note in manifest["notes"])
        )

    @unittest.expectedFailure
    def test_an_artifact_listing_failure_gathers_nothing(self) -> None:
        """No listing means no results, with a note, not a failed run."""
        manifest = self.gather(FakeGitHub([], {}, artifacts_fail=True))
        self.assertEqual(manifest["artifacts"], [])
        self.assertTrue(manifest["notes"][0].startswith("artifact listing failed"))


class MainTest(GatherCase):
    """The CLI writes the manifest and never fails on an artifact."""

    @unittest.expectedFailure
    def test_manifest_written_beside_the_results(self) -> None:
        """The report reads provenance.json from the results directory."""
        module = gather_module()
        selection = self.output.parent / "selection.json"
        selection.write_text(
            json.dumps({"pull_requests": [{"key": "repo-157"}, {"key": 3}]})
        )
        fake = FakeGitHub(
            [artifact(f"{PREFIX}repo-157-1", "2026-11-05T10:01:02Z")],
            {
                1: [
                    apply_job(
                        "repo-157", "2026-11-05T10:01:00Z", "2026-11-05T10:01:04Z"
                    )
                ]
            },
        )
        argv = [
            "--repository",
            REPOSITORY,
            "--run-id",
            str(RUN_ID),
            "--run-attempt",
            "1",
            "--namespace",
            NAMESPACE,
            "--selection",
            str(selection),
            "--output",
            str(self.output),
        ]
        with (
            patch.object(github, "run_gh", fake.run_gh),
            patch.object(module.artifact_fetch, "fetch_artifact", write_files),
            redirect_stdout(io.StringIO()) as out,
        ):
            module.main(argv)
        manifest = json.loads((self.output / module.MANIFEST).read_text())
        self.assertEqual(
            manifest["artifacts"],
            [
                {
                    "name": f"{PREFIX}repo-157-1",
                    "directory": f"{PREFIX}repo-157-1",
                    "key": "repo-157",
                    "attempt": 1,
                    "verified": True,
                }
            ],
        )
        self.assertIn("1 fetched, 1 verified", out.getvalue())

    @unittest.expectedFailure
    def test_a_bad_selection_or_argument_fails(self) -> None:
        """Without a usable selection there is nothing to bound by."""
        module = gather_module()
        selection = self.output.parent / "selection.json"
        selection.write_text("[]")
        argv = [
            "--repository",
            REPOSITORY,
            "--run-id",
            "1",
            "--run-attempt",
            "1",
            "--namespace",
            NAMESPACE,
            "--selection",
            str(selection),
            "--output",
            str(self.output),
        ]
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            module.main(argv)
        self.assertEqual(caught.exception.code, 1)
        argv[argv.index("--namespace") + 1] = "../x"
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            module.main(argv)
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
