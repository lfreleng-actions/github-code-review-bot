# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Diff rules that corroborate or veto an agent's verdict.

The agent's tier is a claim. ``classify_trivial`` checks the claim
"trivial" against the patch itself, and ``find_vetoes`` names changes
that no approvable tier may carry, whatever the agent concluded. Both
read only the per-file patches gathered by the selection job.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from typing import Any

WORKFLOW_RE = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
ACTION_RE = re.compile(r"(?:^|/)action\.ya?ml$")
SHA_RE = re.compile(r"[0-9a-f]{40}")
# Plain-text files are not here on purpose: allow-lists, exclusion
# lists and requirement pins all end in .txt and none is prose.
DOC_SUFFIXES = (".md", ".markdown", ".rst", ".adoc")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")
LOCKFILE_NAMES = frozenset(
    {
        "uv.lock",
        "poetry.lock",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.lock",
        "go.sum",
        "Gemfile.lock",
    }
)
REQUIREMENTS_RE = re.compile(r"^requirements[^/]*\.(txt|in)$")
# A pip pin, a hash continuation, a comment or a blank line.
REQUIREMENT_LINE_RE = re.compile(
    r"^[+-]\s*(?:#.*|--hash=\S+\s*\\?|[A-Za-z0-9_.\[\]-]+(?:\[[^\]]*\])?"
    r"\s*[=<>~!]=?[^\\#]*\\?\s*(?:#.*)?)?\s*$"
)
# A bare "=" is TOML assignment, not a version pin, so a changed
# "[tool]" setting never passes as a dependency bump.
PYPROJECT_LINE_RE = re.compile(
    r'^[+-]\s*"?[A-Za-z0-9_.\[\]-]+\s*(\[[^\]]*\])?\s*(?:[<>~!]=?|==)[^"]*"?,?\s*$'
)
PACKAGE_JSON_LINE_RE = re.compile(r'^[+-]\s*"[^"]+":\s*"[~^]?\d[^"]*",?\s*$')
GO_MOD_LINE_RE = re.compile(r"^[+-]\s+\S+\s+v\d\S*(\s*//.*)?$")
CARGO_LINE_RE = re.compile(
    r'^[+-]\s*[A-Za-z0-9_-]+\s*=\s*("[^"]*"|\{.*version\s*=.*\})\s*$'
)
REV_LINE_RE = re.compile(r"^[+-]\s*rev:\s*\S+(\s+#.*)?$")
FROM_LINE_RE = re.compile(r"^[+-]\s*FROM\s", re.IGNORECASE)
PINNED_FROM_RE = re.compile(
    r"^\+\s*FROM\s+(--platform=\S+\s+)?\S+@sha256:[0-9a-f]{64}(\s+AS\s+\S+)?\s*$",
    re.IGNORECASE,
)
USES_RE = re.compile(r"^\s*-?\s*uses:\s*(?P<action>[^@\s#]+)(?:@(?P<ref>[^\s#]+))?")
LOCAL_REF_PREFIXES = ("./", "$/", "docker://")
PERMISSION_KEYS = frozenset(
    {
        "actions",
        "attestations",
        "checks",
        "contents",
        "deployments",
        "discussions",
        "id-token",
        "issues",
        "models",
        "packages",
        "pages",
        "pull-requests",
        "repository-projects",
        "security-events",
        "statuses",
        "permissions",
    }
)
PERMISSION_WRITE_RE = re.compile(
    r"^\+\s*(?P<key>[a-z-]+)\s*:\s*(write|write-all)\s*(#.*)?$"
)
PERMISSIONS_ALL_RE = re.compile(r"^\+\s*permissions\s*:\s*write-all")
EMPTY_PERMISSIONS_RE = re.compile(r"^-.*permissions:\s*\{\}")
SECRET_RE = re.compile(
    r"secrets\.(?!GITHUB_TOKEN\b)[A-Za-z0-9_]+|secrets\s*:\s*inherit"
)
ACTION_PORT_RE = re.compile(r"^-\s{2}[A-Za-z0-9_-]+:\s*$")
PIPE_TO_SHELL_RE = re.compile(r"\b(curl|wget)\b.*\|\s*(ba)?sh\b")
TRIGGER_TARGET = "pull_request_target"


def changed_lines(patch: str) -> list[str]:
    """Return the added and removed lines of a REST patch, markers kept.

    REST patches carry hunks only, so a line starting with ``+`` or
    ``-`` is always a change; hunk headers, context and the
    "No newline" note are left out.
    """
    return [line for line in patch.splitlines() if line[:1] in {"+", "-"}]


def is_doc(path: str) -> bool:
    """Whether a path is documentation the classifier trusts by name alone.

    Under ``docs/`` only prose and images count: a build hook or a
    requirements pin living there is code or configuration.
    """
    name = posixpath.basename(path)
    if name == "CODEOWNERS" or REQUIREMENTS_RE.match(name):
        return False
    lowered = path.lower()
    if lowered.endswith(DOC_SUFFIXES) or name.startswith(("LICENSE", "NOTICE")):
        return True
    return path.startswith("docs/") and lowered.endswith(IMAGE_SUFFIXES)


def is_lockfile(path: str) -> bool:
    """Whether a path is a generated lockfile whose content is machine-owned."""
    name = posixpath.basename(path)
    return name in LOCKFILE_NAMES or name.endswith(".lock")


def is_workflow_or_action(path: str) -> bool:
    """Whether a path is a GitHub workflow or composite action definition."""
    return bool(WORKFLOW_RE.match(path) or ACTION_RE.search(path))


def uses_of(line: str) -> tuple[str, str | None] | None:
    """Parse a ``uses:`` step line into (action, ref), or None."""
    found = USES_RE.match(line)
    if found is None:
        return None
    return found["action"], found["ref"]


def ref_is_pinned(action: str, ref: str | None) -> bool:
    """Whether an action reference is a commit SHA or needs none."""
    if action.startswith(LOCAL_REF_PREFIXES):
        return True
    return ref is not None and SHA_RE.fullmatch(ref) is not None


def uses_lines_pinned(lines: list[str]) -> bool:
    """Whether changed workflow lines are only SHA bumps of the same actions."""
    removed: list[str] = []
    added: list[str] = []
    for line in lines:
        parsed = uses_of(line[1:])
        if parsed is None:
            return False
        action, ref = parsed
        if line[0] == "-":
            removed.append(action)
            continue
        if not ref_is_pinned(action, ref):
            return False
        added.append(action)
    return sorted(removed) == sorted(added)


def dockerfile_lines_pinned(lines: list[str]) -> bool:
    """Whether changed Dockerfile lines only move FROM to a digest."""
    for line in lines:
        if not FROM_LINE_RE.match(line):
            return False
        if line[0] == "+" and not PINNED_FROM_RE.match(line):
            return False
    return True


def every_line(pattern: re.Pattern[str]) -> Callable[[list[str]], bool]:
    """A rule satisfied when every changed line matches one pattern."""
    return lambda lines: all(pattern.match(line) for line in lines)


# Manifests whose only trivial change is a version pin, by basename.
PIN_RULES: dict[str, Callable[[list[str]], bool]] = {
    "pyproject.toml": every_line(PYPROJECT_LINE_RE),
    "package.json": every_line(PACKAGE_JSON_LINE_RE),
    "go.mod": every_line(GO_MOD_LINE_RE),
    "Cargo.toml": every_line(CARGO_LINE_RE),
    ".pre-commit-config.yaml": every_line(REV_LINE_RE),
}


def line_rule(path: str) -> Callable[[list[str]], bool] | None:
    """The rule every changed line of a path must satisfy, or None."""
    name = posixpath.basename(path)
    if REQUIREMENTS_RE.match(name):
        pinned = REQUIREMENT_LINE_RE if name.endswith(".txt") else PYPROJECT_LINE_RE
        return every_line(pinned)
    if is_workflow_or_action(path):
        return uses_lines_pinned
    if name.startswith("Dockerfile"):
        return dockerfile_lines_pinned
    return PIN_RULES.get(name)


def file_is_trivial(file: dict[str, Any]) -> bool:
    """Whether one changed file is a documentation, lock or pin-only change."""
    path = str(file.get("path") or "")
    if is_doc(path):
        return True
    patch = file.get("patch")
    if file.get("patch_truncated") or not isinstance(patch, str):
        return False
    if is_lockfile(path):
        return True
    rule = line_rule(path)
    return rule is not None and rule(changed_lines(patch))


def classify_trivial(files: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """Return whether every file is trivial and the paths that are not."""
    non_trivial = [
        str(file.get("path") or "") for file in files if not file_is_trivial(file)
    ]
    return not non_trivial, non_trivial


def workflow_vetoes(path: str, lines: list[str]) -> list[str]:
    """Vetoes that apply to workflow files only."""
    found: list[str] = []
    for line in lines:
        if line[0] == "-":
            if EMPTY_PERMISSIONS_RE.match(line):
                found.append(f"{path} removes an empty permissions block")
            continue
        if TRIGGER_TARGET in line:
            found.append(f"{path} adds a {TRIGGER_TARGET} trigger")
        grant = PERMISSION_WRITE_RE.match(line)
        if PERMISSIONS_ALL_RE.match(line):
            found.append(f"{path} grants permissions: write-all")
        elif grant is not None and grant["key"] in PERMISSION_KEYS:
            found.append(f"{path} grants {grant['key']}: write")
        if SECRET_RE.search(line):
            found.append(f"{path} adds a reference to a secret")
    return found


def step_vetoes(path: str, lines: list[str]) -> list[str]:
    """Vetoes shared by workflow and action files: unpinned actions."""
    found: list[str] = []
    for line in lines:
        parsed = uses_of(line[1:]) if line[0] == "+" else None
        if parsed is None:
            continue
        action, ref = parsed
        if not ref_is_pinned(action, ref):
            shown = ref if ref is not None else "no ref"
            found.append(f"{path} uses {action} at {shown}, not a commit SHA")
    return found


def file_vetoes(file: dict[str, Any]) -> list[str]:
    """Every veto one changed file raises, in the order found."""
    path = str(file.get("path") or "")
    patch = file.get("patch")
    if is_doc(path):
        return []
    if file.get("patch_truncated") or not isinstance(patch, str):
        return [f"patch for {path} is unreadable"]
    lines = changed_lines(patch)
    found: list[str] = []
    if WORKFLOW_RE.match(path):
        found.extend(workflow_vetoes(path, lines))
    if is_workflow_or_action(path):
        found.extend(step_vetoes(path, lines))
    if ACTION_RE.search(path) and any(ACTION_PORT_RE.match(line) for line in lines):
        found.append(f"{path}: input or output removed or renamed")
    if any(line[0] == "+" and PIPE_TO_SHELL_RE.search(line) for line in lines):
        found.append(f"{path} pipes a download into a shell")
    return found


def find_vetoes(files: list[dict[str, Any]]) -> list[str]:
    """Human-readable vetoes across all files, each stated once."""
    vetoes: list[str] = []
    for file in files:
        for veto in file_vetoes(file):
            if veto not in vetoes:
                vetoes.append(veto)
    return vetoes
