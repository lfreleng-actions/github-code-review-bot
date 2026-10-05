# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Verify trusted evidence, and hold the caps on untrusted session files.

``verify --directory DIR --selection-sha256 HEX --ledger-sha256 HEX``
checks the exact bytes of ``selection.json`` and ``ledger.json``
against digests the select job published as job outputs, never
against values found in the downloaded artifact.

``SESSION_FILES`` names the files the apply job takes from a review
session and the cap on each; ``artifact_fetch`` enforces them while
extracting the artifact. ``LEDGER_FILES`` does the same for a prior
run's ledger.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
from pathlib import Path

MAX_SELECTION_BYTES = 16 * 1024 * 1024
MAX_LEDGER_BYTES = 4 * 1024 * 1024
MAX_SUMMARY_BYTES = 8 * 1024 * 1024
MAX_USAGE_BYTES = 1024 * 1024
SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")

# (name, byte cap, required)
SESSION_FILES: tuple[tuple[str, int, bool], ...] = (
    ("session-summary.md", MAX_SUMMARY_BYTES, True),
    ("usage.json", MAX_USAGE_BYTES, False),
)
LEDGER_FILES: tuple[tuple[str, int, bool], ...] = (
    ("ledger.json", MAX_LEDGER_BYTES, True),
)


def read_regular(path: Path, limit: int) -> bytes:
    """Read bounded bytes from a regular file without following a final symlink."""
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # NONBLOCK lets fstat reject a FIFO without waiting for a writer.
        # Check the opened descriptor, not a prior stat that could race.
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(path.name, flags, dir_fd=directory_fd), "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"{path.name} must be a regular non-symlink file")
            if info.st_size > limit:
                raise ValueError(f"{path.name} exceeds the {limit}-byte limit")
            content = source.read(limit + 1)
            if len(content) > limit:
                raise ValueError(f"{path.name} exceeds the {limit}-byte limit")
            return content
    finally:
        os.close(directory_fd)


def verify(directory: Path, selection_sha256: str, ledger_sha256: str) -> None:
    """Authenticate the trusted evidence files against independently held hashes."""
    for name, digest, limit in (
        ("selection.json", selection_sha256, MAX_SELECTION_BYTES),
        ("ledger.json", ledger_sha256, MAX_LEDGER_BYTES),
    ):
        if not SHA256_RE.fullmatch(digest):
            raise ValueError(f"trusted SHA-256 for {name} must contain 64 hex digits")
        content = read_regular(directory / name, limit)
        if hashlib.sha256(content).hexdigest() != digest.lower():
            raise ValueError(f"SHA-256 mismatch for {name}")


def main(argv: list[str] | None = None) -> None:
    """Dispatch the verify command."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verification = commands.add_parser("verify", help="check trusted evidence digests")
    verification.add_argument("--directory", type=Path, required=True)
    verification.add_argument("--selection-sha256", required=True)
    verification.add_argument("--ledger-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        verify(args.directory, args.selection_sha256, args.ledger_sha256)
    except (OSError, ValueError) as exc:
        message = ascii(str(exc)).replace("::", ": :").replace("##[", "# #[")
        parser.exit(1, f"evidence: {message}\n")


if __name__ == "__main__":
    main()
