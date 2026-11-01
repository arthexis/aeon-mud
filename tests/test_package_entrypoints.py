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

    def test_cli_is_canonical_and_legacy_exports_match(self):
        self.assertIs(aeon_mud_client.main, cli.main)
        self.assertIs(aeon_mud_client.build_parser, cli.build_parser)
        self.assertIs(aeon_mud_client.command_connect, cli.command_connect)

    def test_cli_invokes_selected_command(self):
        with mock.patch.object(cli, "build_parser") as parser:
            parser.return_value.parse_args.return_value.func.return_value = None
            self.assertEqual(cli.main(["paths"]), 0)
            parser.return_value.parse_args.assert_called_once_with(["paths"])

    def test_legacy_script_and_package_self_test(self):
        for command in (
            [sys.executable, "aeon_mud_client.py", "self-test"],
            [sys.executable, "-m", "aeon_mud", "self-test"],
        ):
            with self.subTest(command=command):
                run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertIn("Self-test passed", run.stdout)

    def test_help_alias_matches_argparse_help(self):
        # Every supported form should resolve to the same nested argparse help.
        for argv, expected_usage in (
            (["help"], "usage: __main__.py [-h]"),
            (["-h"], "usage: __main__.py [-h]"),
            (["-h", "connect"], "usage: __main__.py connect"),
            (["--help", "connect"], "usage: __main__.py connect"),
            (["connect", "help"], "usage: __main__.py connect"),
            (["connect", "-h"], "usage: __main__.py connect"),
            (["help", "connect"], "usage: __main__.py connect"),
            (["help", "profile", "list"], "usage: __main__.py profile list"),
            (["profile", "help", "list"], "usage: __main__.py profile list"),
            (["profile", "list", "help"], "usage: __main__.py profile list"),
            (["-h", "secret", "set"], "usage: __main__.py secret set"),
            (["history", "clear", "help"], "usage: __main__.py history clear"),
            (["transcript", "decrypt", "--help"], "usage: __main__.py transcript decrypt"),
        ):
            with self.subTest(argv=argv):
                run = subprocess.run(
                    [sys.executable, "-m", "aeon_mud", *argv],
                    cwd=ROOT, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertIn(expected_usage, run.stdout)
                self.assertNotIn("invalid choice", run.stderr)

    def test_help_normalizer_preserves_nonhelp_commands(self):
        self.assertEqual(
            cli.normalize_help_argv(["connect", "tapestries", "--login", "Merlin"]),
            ["connect", "tapestries", "--login", "Merlin"],
        )
        self.assertEqual(cli.normalize_help_argv(["connect", "help"]), ["connect", "--help"])
        self.assertEqual(cli.normalize_help_argv(["-h", "connect"]), ["connect", "--help"])

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
