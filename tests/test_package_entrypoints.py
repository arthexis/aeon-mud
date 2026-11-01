"""Verify legacy, package and console entry point compatibility."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import aeon_mud
import aeon_mud_client
from aeon_mud import cli


ROOT = Path(__file__).resolve().parents[1]


class PackageEntrypointTests(unittest.TestCase):
    def test_package_version(self):
        self.assertEqual(aeon_mud.__version__, "0.1.0")

    def test_cli_forwards_args_and_return_code(self):
        with mock.patch.object(aeon_mud_client, "main", return_value=7) as main:
            self.assertEqual(cli.main(["self-test"]), 7)
            main.assert_called_once_with(["self-test"])

    def test_cli_forwards_default_argv(self):
        with mock.patch.object(aeon_mud_client, "main", return_value=0) as main:
            self.assertEqual(cli.main(), 0)
            main.assert_called_once_with(None)

    def test_legacy_script_and_package_self_test(self):
        for command in (
            [sys.executable, "aeon_mud_client.py", "self-test"],
            [sys.executable, "-m", "aeon_mud", "self-test"],
        ):
            with self.subTest(command=command):
                run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertIn("Self-test passed", run.stdout)

    def test_module_help_contains_connect(self):
        run = subprocess.run(
            [sys.executable, "-m", "aeon_mud", "--help"],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("connect", run.stdout)
        self.assertIn("profile", run.stdout)


if __name__ == "__main__":
    unittest.main()
