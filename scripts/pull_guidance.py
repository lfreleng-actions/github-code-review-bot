# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The base repository's own guidance for contributors, read for the packet.

The review session runs with ``--no-custom-instructions``, since the
checkout is the pull request's head and the pull request controls
it. The root ``AGENTS.md`` is read here instead, from the base SHA the
entry records, so a pull request cannot change the guidance it is
judged by. It reaches the agent as bounded data in the packet.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any, cast
from urllib.parse import quote

import bot_github as github
from pull_reads import require_count, truncate_utf8

GUIDANCE_PATH = "AGENTS.md"
MAX_GUIDANCE_BYTES = 16 * 1024


def base_guidance(repository: str, base_sha: str) -> list[dict[str, Any]]:
    """The root ``AGENTS.md`` at ``base_sha``, as an empty or one-entry list.

    A missing file is no guidance. Anything but a regular file at that
    path (a directory, a symlink, a submodule) is left out. The text is
    cut to ``MAX_GUIDANCE_BYTES`` and flagged ``truncated`` when cut.
    GitHub sends no content for a file over 1 MiB; that file is
    recorded with empty text and flagged too.
    """
    context = f"{repository}@{base_sha[:12]} {GUIDANCE_PATH}"
    endpoint = (
        f"repos/{repository}/contents/{quote(GUIDANCE_PATH)}"
        f"?ref={quote(base_sha, safe='')}"
    )
    try:
        parsed = github.decode_response(github.run_gh(["api", endpoint]))
    except github.GitHubError as exc:
        if github.is_absent(exc):
            return []
        raise
    # A directory answers with a list of its entries.
    if not isinstance(parsed, dict):
        return []
    reply = cast("dict[str, Any]", parsed)
    # GitHub follows a symlink to a file and answers with the target's path.
    if reply.get("type") != "file" or reply.get("path") != GUIDANCE_PATH:
        return []
    size = require_count(reply, "size", context)
    encoding = reply.get("encoding")
    if encoding == "none":
        return [entry(base_sha, size, "", truncated=True)]
    if encoding != "base64" or not isinstance(reply.get("content"), str):
        raise github.GitHubError(f"{context}: unexpected content encoding")
    try:
        raw = base64.b64decode(reply["content"])
    except (binascii.Error, ValueError) as exc:
        raise github.GitHubError(f"{context}: invalid base64 content") from exc
    full = raw.decode("utf-8", "replace")
    text = truncate_utf8(full, MAX_GUIDANCE_BYTES)
    return [entry(base_sha, len(raw), text, truncated=text != full)]


def entry(ref: str, size: int, text: str, *, truncated: bool) -> dict[str, Any]:
    """One guidance entry as the selection records it."""
    return {
        "path": GUIDANCE_PATH,
        "ref": ref,
        "bytes": size,
        "truncated": truncated,
        "text": text,
    }
