# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Report how each review leg's session ended, from GitHub's record of the run.

The session artifact uploads whatever the agent step's outcome, so a
readable summary does not show the session completed: a timeout or a
CLI failure can leave one behind. A rerun whose upload failed can
leave an earlier attempt's artifact in place too, so a leg counts as a
success only when its agent step and its upload both succeeded.

One trusted job runs this once the review legs end: it lists the
run's jobs once with the job-native token, and prints
``outcomes=<json>`` for ``$GITHUB_OUTPUT``, an object from each
selected key to its leg's outcome. Each apply leg looks its own key up
in that map, so the listing is not repeated per leg. The antagonistic
check settles a refusal only on ``success`` (see review_comments.check).

Any doubt reads as ``unknown`` and exits 0: an unsettled refusal costs
one more session on the head, and a failure here must not cost the
pull requests their verdicts.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, cast

import bot_evidence as evidence
import bot_github as github

# The review job's, agent step's and upload step's names in
# code-review.yaml; tests/test_workflow.py pins them.
JOB_NAME = "Review ({})"
AGENT_STEP = "Run review agent (Copilot)"
UPLOAD_STEP = "Preserve session output"
UNKNOWN = "unknown"
CONCLUSION_RE = re.compile(r"^[a-z_]{1,32}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def is_leg(name: object, key: str) -> bool:
    """Whether a job name is the review leg for ``key``.

    A caller's run prefixes a reusable workflow's jobs with its own job
    name and `` / ``, so the leg matches alone or after that prefix.
    """
    if not isinstance(name, str):
        return False
    target = JOB_NAME.format(key)
    return name == target or name.endswith(f" / {target}")


def step_conclusion(job: dict[str, Any], name: str) -> str:
    """The conclusion of the one step called ``name``, or ``unknown``."""
    steps = job.get("steps")
    matched: list[dict[str, Any]] = [
        cast("dict[str, Any]", step)
        for step in cast("list[Any]", steps if isinstance(steps, list) else [])
        if isinstance(step, dict) and cast("dict[str, Any]", step).get("name") == name
    ]
    if len(matched) != 1:
        return UNKNOWN
    value = matched[0].get("conclusion")
    return value if isinstance(value, str) and CONCLUSION_RE.match(value) else UNKNOWN


def leg_outcome(job: dict[str, Any]) -> str:
    """The agent step's conclusion, or ``unknown`` when its upload failed.

    An agent step that did not succeed is reported as it ended. A
    successful one counts only when the upload that followed it
    succeeded too: otherwise the artifact may be an earlier attempt's.
    """
    agent = step_conclusion(job, AGENT_STEP)
    if agent != "success":
        return agent
    return "success" if step_conclusion(job, UPLOAD_STEP) == "success" else UNKNOWN


def conclusion(jobs: list[dict[str, Any]], key: str) -> str:
    """The session outcome of the leg for ``key``, or ``unknown``.

    Every matching leg must agree on ``success``; no leg, or more than
    one leg disagreeing, is not a success.
    """
    found = [leg_outcome(job) for job in jobs if is_leg(job.get("name"), key)]
    if not found:
        return UNKNOWN
    if all(value == "success" for value in found):
        return "success"
    return next(value for value in found if value != "success")


def outcomes(jobs: list[dict[str, Any]], keys: list[str]) -> dict[str, str]:
    """Each key's session outcome from one listing of the run's jobs."""
    return {key: conclusion(jobs, key) for key in keys}


def selected_keys(path: Path) -> list[str]:
    """The keys the trusted selection names, each in the matrix's shape."""
    parsed: Any = json.loads(evidence.read_regular(path, evidence.MAX_EVIDENCE_BYTES))
    entries: Any = (
        cast("dict[str, Any]", parsed).get("pull_requests")
        if isinstance(parsed, dict)
        else None
    )
    if not isinstance(entries, list):
        raise ValueError("selection has no pull_requests array")
    keys: list[str] = []
    for item in cast("list[Any]", entries):
        key = (
            cast("dict[str, Any]", item).get("key") if isinstance(item, dict) else None
        )
        if not isinstance(key, str) or not KEY_RE.match(key):
            raise ValueError("selection holds a malformed key")
        keys.append(key)
    return keys


def run_jobs(repository: str, run_id: int) -> list[dict[str, Any]]:
    """Every latest-attempt job of the run, across pages."""
    endpoint = (
        f"repos/{repository}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"
    )
    pages = github.decode_response(
        github.run_gh(["api", endpoint, "--paginate", "--slurp"])
    )
    if not isinstance(pages, list):
        raise github.GitHubError("expected paginated objects of jobs")
    jobs: list[dict[str, Any]] = []
    for page in cast("list[Any]", pages):
        entries = (
            cast("dict[str, Any]", page).get("jobs") if isinstance(page, dict) else None
        )
        if not isinstance(entries, list):
            raise github.GitHubError("expected a jobs array on every page")
        jobs.extend(
            cast("dict[str, Any]", job)
            for job in cast("list[Any]", entries)
            if isinstance(job, dict)
        )
    return jobs


def main(argv: list[str] | None = None) -> None:
    """Print the outcomes line; never fail the step."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    args = parser.parse_args(argv)
    keys: list[str] = []
    try:
        keys = selected_keys(args.selection)
        result = outcomes(run_jobs(args.repository, args.run_id), keys)
    except (github.GitHubError, OSError, ValueError, RecursionError) as exc:
        print(
            f"agent_outcome: {github.safe_message(exc)}; treating as {UNKNOWN}",
            file=sys.stderr,
        )
        result = dict.fromkeys(keys, UNKNOWN)
    print(f"outcomes={json.dumps(result, separators=(',', ':'), sort_keys=True)}")


if __name__ == "__main__":
    main()
