"""Architecture guardrails: package dependencies, CLI ownership and exports."""

import ast
import importlib
import subprocess
import sys
import unittest
from pathlib import Path

import aeon_mud_client as legacy
from aeon_mud import cli


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "aeon_mud"


def package_imports(path: Path) -> set[str]:
    """Return direct intra-package dependencies without executing modules."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.module:
                imports.add(node.module.split(".")[0])
            elif node.module and node.module.startswith("aeon_mud."):
                imports.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("aeon_mud."):
                    imports.add(alias.name.split(".")[1])
    return imports


class PackageArchitectureTests(unittest.TestCase):
    def test_no_package_module_imports_legacy_wrapper(self):
        for path in PACKAGE.glob("*.py"):
            with self.subTest(module=path.name):
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source)
                imports = [
                    alias.name for node in ast.walk(tree)
                    if isinstance(node, ast.Import) for alias in node.names
                ] + [
                    node.module for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.module
                ]
                self.assertNotIn("aeon_mud_client", imports)

    def test_no_circular_package_imports(self):
        paths = {path.stem: path for path in PACKAGE.glob("*.py")
                 if path.stem != "__init__" and path.stem != "__main__"}
        edges = {name: package_imports(path) & paths.keys()
                 for name, path in paths.items()}
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(name: str) -> None:
            self.assertNotIn(name, visiting, f"Circular import through {name}")
            if name in visited:
                return
            visiting.add(name)
            for target in edges[name]:
                visit(target)
            visiting.remove(name)
            visited.add(name)

        for name in paths:
            visit(name)

    def test_all_package_modules_import_independently(self):
        for path in PACKAGE.glob("*.py"):
            if path.stem.startswith("__"):
                continue
            with self.subTest(module=path.stem):
                importlib.import_module("aeon_mud." + path.stem)

    def test_cli_is_sole_implementation_owner(self):
        self.assertIs(legacy.main, cli.main)
        self.assertIs(legacy.command_connect, cli.command_connect)
        self.assertIs(legacy.build_parser, cli.build_parser)
        self.assertEqual(cli.main.__module__, "aeon_mud.cli")

    def test_python_module_and_legacy_script_help_equivalent(self):
        commands = [
            [sys.executable, "-m", "aeon_mud", "connect", "--help"],
            [sys.executable, "aeon_mud_client.py", "connect", "--help"],
        ]
        outputs = []
        for command in commands:
            result = subprocess.run(command, cwd=ROOT, text=True,
                                    capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append(result.stdout)
        self.assertEqual(*outputs)


if __name__ == "__main__":
    unittest.main()
