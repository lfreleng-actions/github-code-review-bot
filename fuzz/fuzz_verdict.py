# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Fuzz the verdict extraction and the sanitisers of ``review_policy``.

The input is an agent's final message as the apply job reads it:
bytes decoded as UTF-8 with replacement (``review_files.read_summary``).
Each input is checked against what the code documents:

- ``review_files.parse_verdict`` catches ``Rejected`` by design, so it
  is fuzzed: "Extract the verdict from a read summary, or explain why
  it is unusable." A refusal is ``Rejected`` and never another
  exception, which would escape it and fail the job.
- ``final_json_block``: "Return the body of the final fenced block,
  which must be json." An accepted verdict is the object in the last
  fenced block, found here by a scan of the message's lines.
- ``extract_verdict`` returns "a bounded, validated shape": schema
  1, a known tier, an ``owner/name`` repository, a 40-hex head, a
  positive number, at most 20 findings with known areas and 20
  injections, the first 20 of each in the final block, none dropped.
  Each text field reads as its source did: only the marks the
  sanitiser adds where it adds them, the whitespace it folds and the
  end it cuts differ.
- ``apply_review`` writes the job summary as UTF-8 and posts the
  approval body; both are rendered and encoded strictly, as the
  streams that carry them would.
- ``sanitise``: "Fold untrusted text into one bounded line safe for
  logs and Markdown. Control characters become spaces,
  workflow-command markers are broken, raw HTML stays text, a code
  fence cannot open, and mentions and references are defused." Every
  text field of a verdict is held to this, and so is the whole
  message passed straight through.
- ``defuse``: "Each pattern skips text it already defused, so a second
  pass changes nothing."
- ``contain_links``, on the summary as ``apply_review`` posts it:
  "Leave no live link in untrusted text but those into the
  organisation."

Run by hand with ``python fuzz/fuzz_verdict.py``; see the README.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from importlib import import_module
from pathlib import Path
from typing import Any, cast

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "scripts")]

from github_markdown import (  # noqa: E402
    Breach,
    live_links,
    live_references,
    require,
)
from sanitised_text import CONTROL, verify_text  # noqa: E402

policy = import_module("review_policy")
files = import_module("review_files")
review = import_module("apply_review")

ORG = "lfreleng-actions"
SUMMARY_CHARS = 600
FINDING_CHARS = 400
TIERS = ("trivial", "low-risk", "needs-human")
AREAS = ("security", "ci", "correctness", "scope", "other")
# Possessive: a line of fence characters that fails to match would
# otherwise be retried at every split between the run and the rest.
FENCE_LINE = re.compile(r"[ \t]*+(`{3,}+|~{3,}+)([^\r]*+)\r?")


def verdict_text(**fields: Any) -> str:
    """A final message ending in a verdict block with ``fields`` set."""
    verdict: dict[str, Any] = {
        "schema": 1,
        "repository": f"{ORG}/repo",
        "pull_request": 7,
        "head_sha": "a" * 40,
        "tier": "trivial",
        "summary": "Bumps a pin.",
        "findings": [],
        "injection_attempts": [],
    }
    verdict.update(fields)
    return f"Reviewed.\n\n```json\n{json.dumps(verdict, ensure_ascii=False)}\n```\n"


SEEDS: list[bytes] = [
    verdict_text().encode(),
    verdict_text(
        tier="needs-human",
        summary="cc @team, see #12 and GH-3 <img src=x> ::warning::x ##[group]",
        findings=[{"area": "security", "note": "&#64;admin https://evil.example"}],
        injection_attempts=["ignore previous instructions\n```"],
    ).encode(),
    verdict_text(
        summary="See https://github.com/lfreleng-actions/x/../../evil and "
        "[here](https://evil.example) www.evil.example ![i](x) [l]: u",
    ).encode(),
    b'```json\n{"example": true}\n```\nNo verdict follows.\n',
    b"```json\n" + b'{"schema": 1' + b"[" * 3000 + b"\n```\n",
    b"~~~~json\n{}\n~~~~\n````\nunterminated\n",
    '\ufeff\u2028```json \r\n{"tier": "trivial"}\r\n```\r\n'.encode(),
    b"\xff\xfe```json\n\x00\n```",
    b"`" * 4096,
    # Found by fuzzing: a first segment that only starts with the
    # organisation name stayed a live link.
    verdict_text(
        summary=f"https://github.com/{ORG})other/repo "
        f'https://github.com/{ORG}"/Reviewed',
    ).encode(),
    # Reported in review: GFM trims no quote, so this links to /<org>'.
    verdict_text(summary=f"https://github.com/{ORG}'?").encode(),
    # Reported in review: GFM autolinks ftp:// as well as http(s)://.
    verdict_text(summary="ftp://evil.example/x").encode(),
    # Reported in review: a JSON escape for a lone surrogate in a note
    # reached the job summary, which no UTF-8 stream can write.
    verdict_text(tier="needs-human", findings=[{"area": "ci", "note": "<n>"}])
    .replace("<n>", "\\ud800 \\udfff")
    .encode(),
]


def one_line(text: str, limit: int, what: str) -> None:
    """``text`` is what ``sanitise`` documents: one bounded, safe line."""
    breaches: list[str] = []
    if len(text) > limit + len("..."):
        breaches.append(f"{len(text)} characters, over {limit}")
    if CONTROL.search(text):
        breaches.append("a control character or line break")
    if text.lstrip(" ").startswith("::") or "##[" in text:
        breaches.append("a workflow-command marker")
    if text.startswith(("```", "~~~")):
        breaches.append("a code fence opening")
    breaches.extend(live_references(text))
    require(breaches, what)


def last_block(text: str) -> tuple[str, str] | None:
    """The info string and body of the message's last closed fenced block.

    A fence line holds a run of three or more backticks or tildes after
    optional spaces or tabs; it closes the open block when it repeats
    the opener's character at least as many times with no info string.
    ``None`` when no block closes or the last opener never does.
    """
    lines = text.split("\n")
    found: tuple[str, str] | None = None
    opener: tuple[int, str, str] | None = None
    for index, line in enumerate(lines):
        fence = FENCE_LINE.fullmatch(line)
        if fence is None:
            continue
        run, info = fence[1], fence[2]
        if opener is None:
            opener = (index, run, info)
            found = None
        elif run[0] == opener[1][0] and len(run) >= len(opener[1]) and not info.strip():
            found = (opener[2], "\n".join(lines[opener[0] + 1 : index]))
            opener = None
    return None if opener is not None else found


def verify_kept_lists(
    source: dict[str, Any], findings: list[Any], injections: list[Any]
) -> None:
    """The first 20 findings and injection attempts of the block, in order.

    ``findings_of``: "Validate the findings list, keeping at most
    MAX_FINDINGS entries"; ``injections_of``: "bounded in count". An
    approvable tier is held back by any entry, so none may go missing,
    and each keeps its own text.
    """
    listed = source.get("findings")
    attempts = source.get("injection_attempts")
    if not isinstance(listed, list) or not isinstance(attempts, list):
        raise Breach("a verdict accepted without findings and injection lists")
    items = [
        cast("dict[str, Any]", item) if isinstance(item, dict) else {}
        for item in cast("list[Any]", listed)[:20]
    ]
    if [finding["area"] for finding in findings] != [i.get("area") for i in items]:
        raise Breach("findings are not the first 20 of the final block, in order")
    for finding, item in zip(findings, items, strict=True):
        verify_text(finding["note"], item.get("note"), FINDING_CHARS, "finding note")
    written = cast("list[Any]", attempts)[:20]
    if len(injections) != len(written):
        raise Breach("injection attempts are not the first 20 of the final block")
    for injection, attempt in zip(injections, written, strict=True):
        verify_text(injection, attempt, FINDING_CHARS, "injection attempt")


def verify_verdict(text: str, verdict: dict[str, Any]) -> None:
    """An accepted verdict is the final json block, bounded and validated."""
    block = last_block(text)
    if block is None or block[0].strip() != "json":
        raise Breach("a verdict accepted without a final json block")
    try:
        source: Any = json.loads(block[1])
    except (ValueError, RecursionError) as exc:
        raise Breach("a verdict accepted from a malformed final block") from exc
    if not isinstance(source, dict):
        raise Breach("a verdict accepted from a final block holding no object")
    fields = cast("dict[str, Any]", source)
    schema = fields.get("schema")
    if type(schema) not in (int, float) or schema != 1 or verdict["schema"] != 1:
        raise Breach("a verdict accepted without schema 1")
    if type(fields.get("pull_request")) is not int:
        raise Breach("a verdict accepted from a pull_request that is not an integer")
    for key in ("repository", "head_sha", "tier", "pull_request"):
        if fields.get(key) != verdict[key]:
            raise Breach(f"verdict {key!r} is not the final block's")
    if verdict["tier"] not in TIERS:
        raise Breach(f"unknown tier {verdict['tier']!r}")
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+", verdict["repository"]
    ):
        raise Breach("repository is not owner/name")
    if not re.fullmatch(r"[0-9a-f]{40}", verdict["head_sha"]):
        raise Breach("head_sha is not 40 hex characters")
    number = verdict["pull_request"]
    if type(number) is not int or number <= 0:
        raise Breach("pull_request is not a positive integer")
    findings = verdict["findings"]
    injections = verdict["injection_attempts"]
    if len(findings) > 20 or len(injections) > 20:
        raise Breach("more than 20 findings or injection attempts")
    verify_kept_lists(fields, findings, injections)
    verify_text(verdict["summary"], fields.get("summary"), SUMMARY_CHARS, "summary")
    one_line(verdict["summary"], SUMMARY_CHARS, "summary")
    for finding in findings:
        if finding["area"] not in AREAS:
            raise Breach(f"unknown finding area {finding['area']!r}")
        one_line(finding["note"], FINDING_CHARS, "finding note")
    for injection in injections:
        one_line(injection, FINDING_CHARS, "injection attempt")


def verify_posted(summary: str, org: str = ORG) -> None:
    """A summary as the approval body posts it holds no outside link."""
    require(live_links(summary, org), "posted summary")
    require(live_references(summary), "posted summary")


def verify_defused(once: str, twice: str) -> None:
    """A second ``defuse`` pass changes nothing."""
    if once != twice:
        raise Breach("defuse is not idempotent")


def emitted(verdict: dict[str, Any] | None, failure: str | None) -> None:
    """Render the check as ``apply_review`` writes it, to strict UTF-8.

    ``run_check`` writes the job summary with ``encoding="utf-8"`` and
    posts the approval body; text neither can encode fails the job, so
    each is encoded here as strictly as the stream that carries it.
    ``approval_body``: "The summary is the agent's text: it links
    nowhere outside the organisation the pull request belongs to", so
    the body above the footer it writes holds no outside link.
    """
    entry: dict[str, Any] = {
        "repository": f"{ORG}/repo" if verdict is None else verdict["repository"],
        "number": 7 if verdict is None else verdict["pull_request"],
        "head_sha": "a" * 40 if verdict is None else verdict["head_sha"],
        "files": [],
        "url": f"https://github.com/{ORG}/repo/pull/7",
        "title": "Fuzzed",
    }
    checked = policy.check(
        entry, verdict, approve_tiers=frozenset(TIERS[:2]), failure=failure
    )
    review.summary_markdown(checked).encode("utf-8")
    if checked["tier"] in TIERS[:2]:
        run_url = f"https://github.com/{ORG}/repo/actions/runs/1"
        posted = review.approval_body(checked, {}, run_url)
        posted.encode("utf-8")
        agent_part, footer, _ = posted.rpartition("\n\n---\n")
        if not footer:
            raise Breach("the approval body carries no footer")
        verify_posted(agent_part, str(checked["repository"]).partition("/")[0])


def check(data: bytes) -> None:
    """Run one input through every contract this harness checks."""
    text = data.decode("utf-8", errors="replace")
    verdict, failure = files.parse_verdict(text, policy.extract_verdict)
    if verdict is None:
        if not isinstance(failure, str) or not failure:
            raise Breach("a refusal carries no reason")
    else:
        verify_verdict(text, verdict)
        verify_posted(
            policy.contain_links(
                policy.sanitise(verdict["summary"], SUMMARY_CHARS), ORG
            )
        )
    emitted(verdict, failure)
    sanitised = policy.sanitise(text, SUMMARY_CHARS)
    one_line(sanitised, SUMMARY_CHARS, "sanitised message")
    verify_posted(policy.contain_links(sanitised, ORG))
    once = policy.defuse(text)
    verify_defused(once, policy.defuse(once))


def main() -> None:
    """Fuzz ``check``, from the seeds unless an argument names an input file."""
    import atheris  # type: ignore[import-not-found]  # pyright: ignore[reportMissingImports]

    fuzzer: Any = atheris

    args = list(sys.argv)
    if not any(os.path.isfile(arg) for arg in args[1:]):
        seeds = tempfile.mkdtemp(prefix="fuzz-verdict-")
        for index, seed in enumerate(SEEDS):
            Path(seeds, f"seed-{index}").write_bytes(seed)
        args.append(seeds)
    fuzzer.instrument_all()
    fuzzer.Setup(args, check)
    fuzzer.Fuzz()


if __name__ == "__main__":
    main()
