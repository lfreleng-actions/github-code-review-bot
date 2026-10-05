# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Bounded reads of regular files and verification of trusted evidence."""

from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

evidence = import_module("review_evidence")

SELECTION = b'{"schema": 1, "pull_requests": []}\n'
LEDGER = b'{"schema": 1, "entries": []}\n'


def digest(content: bytes) -> str:
    """The hex SHA-256 of some bytes."""
    return hashlib.sha256(content).hexdigest()


class ReadRegularTest(unittest.TestCase):
    """``read_regular`` takes a bounded regular file and nothing else."""

    def setUp(self) -> None:
        """A scratch directory holding the files under test."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)

    def test_reads_a_regular_file_within_the_limit(self) -> None:
        """A file at exactly the limit is returned whole."""
        path = self.root / "f"
        path.write_bytes(b"x" * 16)
        self.assertEqual(evidence.read_regular(path, 16), b"x" * 16)

    def test_oversize_file_refused(self) -> None:
        """One byte past the limit is a refusal."""
        path = self.root / "f"
        path.write_bytes(b"x" * 17)
        with self.assertRaisesRegex(ValueError, "exceeds the 16-byte limit"):
            evidence.read_regular(path, 16)

    def test_symlink_refused(self) -> None:
        """A symlink in the final component is never followed."""
        target = self.root / "real"
        target.write_bytes(b"x")
        linked = self.root / "link"
        linked.symlink_to(target)
        with self.assertRaises(OSError):
            evidence.read_regular(linked, 16)

    def test_symlinked_parent_refused(self) -> None:
        """A symlink standing in for the directory is refused too."""
        real = self.root / "dir"
        real.mkdir()
        (real / "f").write_bytes(b"x")
        (self.root / "alias").symlink_to(real)
        with self.assertRaises(OSError):
            evidence.read_regular(self.root / "alias" / "f", 16)

    def test_directory_refused(self) -> None:
        """A directory where a file should be is refused."""
        (self.root / "d").mkdir()
        with self.assertRaises((OSError, ValueError)):
            evidence.read_regular(self.root / "d", 16)

    def test_fifo_refused_without_blocking(self) -> None:
        """A FIFO with no writer is rejected rather than waited on."""
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, "regular non-symlink file"):
            evidence.read_regular(fifo, 16)

    def test_missing_file_is_an_os_error(self) -> None:
        """An absent file surfaces as OSError for the caller to report."""
        with self.assertRaises(OSError):
            evidence.read_regular(self.root / "absent", 16)


class VerifyTest(unittest.TestCase):
    """``verify`` authenticates both evidence files against given digests."""

    def setUp(self) -> None:
        """A directory holding a selection and a ledger."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        (self.root / "selection.json").write_bytes(SELECTION)
        (self.root / "ledger.json").write_bytes(LEDGER)

    def test_matching_digests_accepted(self) -> None:
        """Correct digests pass; upper-case hex is the same digest."""
        evidence.verify(self.root, digest(SELECTION), digest(LEDGER))
        evidence.verify(self.root, digest(SELECTION).upper(), digest(LEDGER))

    def test_mismatch_rejected(self) -> None:
        """A wrong digest for either file names the file."""
        with self.assertRaisesRegex(ValueError, "mismatch for ledger.json"):
            evidence.verify(self.root, digest(SELECTION), digest(b"other"))
        with self.assertRaisesRegex(ValueError, "mismatch for selection.json"):
            evidence.verify(self.root, digest(b"other"), digest(LEDGER))

    def test_malformed_digest_rejected_before_reading(self) -> None:
        """A digest that is not 64 hex digits is refused outright."""
        for bad in ("", "abc", "g" * 64, digest(SELECTION) + "0"):
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(ValueError, "64 hex digits"),
            ):
                evidence.verify(self.root, bad, digest(LEDGER))

    def test_missing_file_is_an_os_error(self) -> None:
        """An absent evidence file is an operational failure."""
        (self.root / "ledger.json").unlink()
        with self.assertRaises(OSError):
            evidence.verify(self.root, digest(SELECTION), digest(LEDGER))


class MainTest(unittest.TestCase):
    """The CLI exits 1 with a safe message on any verification failure."""

    def test_failure_exits_one_with_message(self) -> None:
        """The message is prefixed and carries no workflow command syntax."""
        with tempfile.TemporaryDirectory() as holder:
            root = Path(holder)
            (root / "selection.json").write_bytes(SELECTION)
            (root / "ledger.json").write_bytes(LEDGER)
            argv = [
                "verify",
                "--directory",
                holder,
                "--selection-sha256",
                digest(b"wrong"),
                "--ledger-sha256",
                digest(LEDGER),
            ]
            with (
                redirect_stderr(io.StringIO()) as err,
                self.assertRaises(SystemExit) as caught,
            ):
                evidence.main(argv)
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("evidence: 'SHA-256 mismatch for selection.json'", err.getvalue())

    def test_success_is_silent(self) -> None:
        """Matching digests exit normally."""
        with tempfile.TemporaryDirectory() as holder:
            root = Path(holder)
            (root / "selection.json").write_bytes(SELECTION)
            (root / "ledger.json").write_bytes(LEDGER)
            argv = [
                "verify",
                "--directory",
                holder,
                "--selection-sha256",
                digest(SELECTION),
                "--ledger-sha256",
                digest(LEDGER),
            ]
            evidence.main(argv)


if __name__ == "__main__":
    unittest.main()
