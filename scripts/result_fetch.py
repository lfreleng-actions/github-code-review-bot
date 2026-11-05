# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Gather this run's result artifacts, bounded, with their provenance.

The report job runs this in place of ``download-artifact``, which
expands every matching archive before anything can check its size. It
lists the run's ``code-review-result-<namespace>-<key>-<attempt>``
artifacts through the API, ignores any whose key the trusted
selection does not hold, and fetches the rest through
``artifact_fetch`` under ``RESULT_FILES``, one directory per artifact.

An artifact name says nothing about who uploaded it: any job in the
run, the untrusted review job included, can upload under any name.
So each artifact is also checked against the run's jobs. It is
**verified** only when its ``created_at`` falls inside the window of
the ``Attach apply result`` step of the ``Apply (<key>)`` job for
the same key and attempt, give or take ``SKEW``, and no review leg of
any invocation in the run, in any attempt, was in progress during that
window. Untrusted code runs only in review legs; within one invocation
every review leg finishes before any apply leg starts (``apply`` needs
``review``), but a caller may run this workflow more than once in a
run, and those invocations share the run's artifacts. Anything else,
a jobs listing that fails or cannot be read in full included, is
unverified, and the report keeps it out of the ledger (docs/DESIGN.md
section 12).

``provenance.json`` in the output directory records each fetched
artifact, its key, attempt and verdict, and a note for each artifact
left out. An artifact that is refused, or whose fetch raises any
exception, is noted as refused and left out; it never fails the run. Exit status is 0 unless the arguments or
the selection are unusable.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import artifact_fetch
import bot_github as github
from bot_evidence import MAX_EVIDENCE_BYTES, read_regular
from result_binding import MAX_RESULT_BYTES
from review_files import MAX_CHECK_BYTES

SCHEMA = 1
PREFIX = "code-review-result-"
MANIFEST = "provenance.json"
APPLY_STEP = "Attach apply result"
# Step and artifact times come from different services, each to the
# second; a few seconds either side absorbs the rounding and skew.
SKEW = timedelta(seconds=5)
# Headroom over one artifact per selected key per attempt.
MARGIN = 2
# The apply job writes check.json and its summary from data the apply
# step itself reads under MAX_CHECK_BYTES, so neither can be larger.
RESULT_FILES: artifact_fetch.Profile = (
    ("result.json", MAX_RESULT_BYTES, True),
    ("check.json", MAX_CHECK_BYTES, False),
    ("check-summary.md", MAX_CHECK_BYTES, False),
)
ATTEMPT_RE = re.compile(r"^[1-9][0-9]{0,5}$")
# A review leg of any invocation, under any key: ``Review (<key>)`` or
# ``<caller job> / Review (<key>)``. Keys never hold parentheses, so an
# apply leg under a caller job named ``Review (...)`` does not match.
REVIEW_RE = re.compile(r"(?:.+ / )?Review \([^()]+\)")

Window = tuple[datetime, datetime]
# A review leg's start and end; None where GitHub gives no time.
Span = tuple[datetime | None, datetime | None]


def paged(endpoint: str, field: str) -> list[dict[str, Any]]:
    """Every object in ``field`` across the pages of an object-shaped listing.

    An entry that is not an object fails the listing: it cannot be
    read in full, so nothing it lists can be relied on.
    """
    pages = github.decode_response(
        github.run_gh(["api", endpoint, "--paginate", "--slurp"])
    )
    if not isinstance(pages, list):
        raise github.GitHubError(f"expected paginated objects from {endpoint}")
    entries: list[dict[str, Any]] = []
    for page in cast("list[Any]", pages):
        items = (
            cast("dict[str, Any]", page).get(field) if isinstance(page, dict) else None
        )
        if not isinstance(items, list):
            raise github.GitHubError(f"a page from {endpoint} carried no {field}")
        for item in cast("list[Any]", items):
            if not isinstance(item, dict):
                raise github.GitHubError(f"a page from {endpoint} carried a non-object")
            entries.append(cast("dict[str, Any]", item))
    return entries


def timestamp(value: object) -> datetime | None:
    """An aware datetime from an API timestamp, or None when there is none."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def parse_name(name: str, namespace: str) -> tuple[str, int] | None:
    """The key and attempt a result artifact's name claims, or None."""
    prefix = f"{PREFIX}{namespace}-"
    if not name.startswith(prefix):
        return None
    key, _, attempt = name[len(prefix) :].rpartition("-")
    if not key or not ATTEMPT_RE.fullmatch(attempt):
        return None
    return key, int(attempt)


def job_key(name: object, keys: set[str]) -> str | None:
    """The selected key an apply job's name names, or None.

    A reusable workflow's jobs are listed under the caller's job name,
    as ``<caller> / Apply (<key>)``; a direct run lists ``Apply (<key>)``.
    Those two forms match and nothing looser.
    """
    if not isinstance(name, str):
        return None
    for key in keys:
        own = f"Apply ({key})"
        if name == own or name.endswith(f" / {own}"):
            return key
    return None


def review_leg(name: object) -> bool:
    """Whether a job's name is a review leg's, of any invocation or key."""
    return isinstance(name, str) and REVIEW_RE.fullmatch(name) is not None


def step_window(job: dict[str, Any]) -> Window | None:
    """The start and end of a job's ``Attach apply result`` step, if it ran."""
    for raw in cast("list[Any]", job.get("steps") or []):
        if not isinstance(raw, dict):
            continue
        step = cast("dict[str, Any]", raw)
        if step.get("name") != APPLY_STEP:
            continue
        start = timestamp(step.get("started_at"))
        end = timestamp(step.get("completed_at"))
        if start is not None and end is not None and start <= end:
            return start, end
    return None


def run_jobs(
    repository: str, run_id: int, attempts: int, keys: set[str]
) -> tuple[dict[tuple[str, int], list[Window]], list[Span]]:
    """Each selected key and attempt's upload windows, and every review leg.

    The listing of attempt ``n`` may carry jobs an earlier attempt ran
    and a later one did not repeat; each job's own ``run_attempt`` says
    which attempt it belongs to, and a job seen twice counts once. The
    review legs are every invocation's, under any key, in any attempt.
    """
    windows: dict[tuple[str, int], list[Window]] = {}
    busy: list[Span] = []
    seen: set[int] = set()
    for attempt in range(1, attempts + 1):
        endpoint = (
            f"repos/{repository}/actions/runs/{run_id}"
            f"/attempts/{attempt}/jobs?per_page=100"
        )
        for job in paged(endpoint, "jobs"):
            job_id = job.get("id")
            if type(job_id) is int:
                if job_id in seen:
                    continue
                seen.add(job_id)
            if review_leg(job.get("name")):
                busy.append(
                    (
                        timestamp(job.get("started_at")),
                        timestamp(job.get("completed_at")),
                    )
                )
                continue
            key = job_key(job.get("name"), keys)
            if type(job_id) is not int or key is None:
                continue
            ran = job.get("run_attempt")
            ran_in = ran if type(ran) is int and ran > 0 else attempt
            window = step_window(job)
            if window is not None:
                windows.setdefault((key, ran_in), []).append(window)
    return windows, busy


def overlapped(window: Window, busy: list[Span]) -> bool:
    """Whether any review leg may have run during an upload window.

    A leg with no start or no end, one still running included, counts
    as overlapping: its span is not known to lie clear of the window.
    """
    low, high = window[0] - SKEW, window[1] + SKEW
    for start, end in busy:
        if start is None or end is None or (start <= high and end >= low):
            return True
    return False


def verified(created: datetime | None, windows: list[Window]) -> bool:
    """Whether an artifact's creation falls in one of its apply windows."""
    if created is None:
        return False
    return any(start - SKEW <= created <= end + SKEW for start, end in windows)


def candidates(
    listing: list[dict[str, Any]],
    namespace: str,
    keys: set[str],
    attempts: int,
    notes: list[str],
) -> list[tuple[dict[str, Any], str, int]]:
    """The live artifacts named for a selected key and attempt, bounded.

    Names outside this run's namespace are not results and pass
    silently; a result name for an unselected key or a future attempt
    is noted and ignored. Past one artifact per selected key per
    attempt, plus ``MARGIN``, the oldest are refused.
    """
    found: list[tuple[dict[str, Any], str, int]] = []
    for entry in listing:
        name = entry.get("name")
        if not isinstance(name, str) or entry.get("expired"):
            continue
        claim = parse_name(name, namespace)
        if claim is None:
            continue
        key, attempt = claim
        if key not in keys:
            notes.append(f"{name}: ignored, its key is not in the selection")
        elif attempt > attempts:
            notes.append(f"{name}: ignored, its attempt has not run")
        else:
            found.append((entry, key, attempt))
    found.sort(key=lambda item: (item[2], str(item[0].get("created_at"))), reverse=True)
    limit = len(keys) * attempts + MARGIN
    for entry, _, _ in found[limit:]:
        notes.append(f"{entry.get('name')}: refused, over the bound of {limit}")
    return found[:limit]


def fetch_one(repository: str, entry: dict[str, Any], output: Path) -> list[str] | str:
    """Fetch one artifact into ``output``, or say why it was left out."""
    try:
        artifact_id = github.require_int(entry, "id", "artifact")
        size = artifact_fetch.artifact_size(entry)
        return artifact_fetch.fetch_artifact(
            repository, artifact_id, size, output, RESULT_FILES
        )
    except artifact_fetch.Refused as exc:
        return f"refused: {github.safe_message(exc)}"
    except Exception as exc:
        # Malformed zip metadata escapes artifact_fetch as assorted
        # exceptions (NotImplementedError, UnicodeDecodeError, ...).
        # A refused artifact is left out and never trusted, so catching
        # them all cannot widen trust; BaseException still propagates.
        return f"refused: {type(exc).__name__}: {github.safe_message(exc)}"


def selected_keys(path: Path) -> set[str]:
    """The keys of the digest-verified selection's pull requests."""
    parsed: Any = json.loads(read_regular(path, MAX_EVIDENCE_BYTES))
    if not isinstance(parsed, dict):
        raise ValueError("selection is not an object")
    entries = cast("dict[str, Any]", parsed).get("pull_requests")
    if not isinstance(entries, list):
        raise ValueError("selection carries no pull_requests list")
    keys: set[str] = set()
    for item in cast("list[Any]", entries):
        key = (
            cast("dict[str, Any]", item).get("key") if isinstance(item, dict) else None
        )
        if isinstance(key, str) and key:
            keys.add(key)
    return keys


def gather(
    repository: str,
    run_id: int,
    attempts: int,
    namespace: str,
    keys: set[str],
    output: Path,
) -> dict[str, Any]:
    """Fetch every candidate result, judge its provenance, return the manifest."""
    notes: list[str] = []
    artifacts: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "artifacts": artifacts,
        "notes": notes,
    }
    try:
        listing = paged(
            f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100",
            "artifacts",
        )
    except github.GitHubError as exc:
        notes.append(f"artifact listing failed: {github.safe_message(exc)}")
        return manifest
    chosen = candidates(listing, namespace, keys, attempts, notes)
    windows: dict[tuple[str, int], list[Window]] | None = None
    busy: list[Span] = []
    if chosen:
        try:
            windows, busy = run_jobs(repository, run_id, attempts, keys)
        except github.GitHubError as exc:
            notes.append(
                "jobs listing failed, so no result is verified: "
                f"{github.safe_message(exc)}"
            )
    for entry, key, attempt in chosen:
        name = str(entry["name"])
        fetched = fetch_one(repository, entry, output / name)
        if isinstance(fetched, str):
            notes.append(f"{name}: {fetched}")
            continue
        created = timestamp(entry.get("created_at"))
        own = [] if windows is None else windows.get((key, attempt), [])
        clear = [window for window in own if not overlapped(window, busy)]
        trusted = verified(created, clear)
        if not trusted and verified(created, own):
            notes.append(f"{name}: unverified, a review leg ran during its upload")
        artifacts.append(
            {
                "name": name,
                "directory": name,
                "key": key,
                "attempt": attempt,
                "verified": trusted,
            }
        )
    return manifest


def main(argv: list[str] | None = None) -> None:
    """Gather the results and write the manifest beside them."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not github.REPO_RE.fullmatch(args.repository):
        parser.error("--repository must be owner/name")
    if args.run_id <= 0 or args.run_attempt <= 0:
        parser.error("--run-id and --run-attempt must be positive")
    if not re.fullmatch(r"[A-Za-z0-9-]+", args.namespace):
        parser.error("--namespace must be letters, digits and hyphens")
    try:
        keys = selected_keys(args.selection)
    except (OSError, ValueError, RecursionError) as exc:
        parser.exit(1, f"results: {github.safe_message(exc)}\n")
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = gather(
        args.repository,
        args.run_id,
        args.run_attempt,
        args.namespace,
        keys,
        args.output,
    )
    (args.output / MANIFEST).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    for note in cast("list[str]", manifest["notes"]):
        print(f"::warning::results: {note}", file=sys.stderr)
    entries = cast("list[dict[str, Any]]", manifest["artifacts"])
    trusted = sum(1 for entry in entries if entry["verified"])
    print(
        f"results: {len(entries)} fetched, {trusted} verified,"
        f" {len(manifest['notes'])} note(s)"
    )


if __name__ == "__main__":
    main()
