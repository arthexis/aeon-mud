"""Offline tests for the immutable owner deployment, without Ansible or privileged writes."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_immutable.py"
spec = importlib.util.spec_from_file_location("aeon_deploy_immutable", SCRIPT)
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)

SHA_A = "a" * 40
SHA_B = "b" * 40


class OwnerDeployTests(unittest.TestCase):
    def test_sha_and_clean_tree_are_required(self):
        with mock.patch.object(deployment, "source_revision", return_value=SHA_A):
            with mock.patch.object(deployment.subprocess, "check_output", return_value=""):
                self.assertEqual(deployment.assert_source(Path("."), SHA_A), SHA_A)
                with self.assertRaises(ValueError):
                    deployment.assert_source(Path("."), SHA_B)
            with mock.patch.object(deployment.subprocess, "check_output", return_value=" M aeon_mud/cli.py"):
                with self.assertRaises(ValueError):
                    deployment.assert_source(Path("."), SHA_A)
        with self.assertRaises(ValueError):
            deployment.assert_source(Path("."), "not-a-sha")

    def test_idempotent_install_and_rollback_links(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / "source"
            (source / "aeon_mud").mkdir(parents=True)
            (source / "aeon_mud" / "__init__.py").write_text("")
            (source / "aeon_mud" / "cli.py").write_text(
                "def main():\n    return 0\n", encoding="utf-8")
            (source / "aeon_mud_client.py").write_text("# compatibility\n")
            root = home / "share"
            entry = home / "bin" / "aeon-mud"
            with mock.patch.object(deployment, "assert_source", side_effect=lambda _source, sha: sha):
                self.assertEqual(deployment.install(source, SHA_A, root, entry), (SHA_A, True))
                self.assertEqual(deployment.install(source, SHA_A, root, entry), (SHA_A, False))
                self.assertEqual(deployment.install(source, SHA_B, root, entry), (SHA_B, True))
                self.assertEqual((root / "current").resolve(), root / "releases" / SHA_B)
                self.assertEqual((root / "previous").resolve(), root / "releases" / SHA_A)
                self.assertEqual(entry.readlink(), root / "current" / "aeon-mud.pyz")

    def test_failed_activation_restores_previous_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / "source"
            (source / "aeon_mud").mkdir(parents=True)
            (source / "aeon_mud" / "__init__.py").write_text("")
            (source / "aeon_mud" / "cli.py").write_text(
                "def main():\n    return 0\n", encoding="utf-8")
            (source / "aeon_mud_client.py").write_text("# compatibility")
            root = home / "share"
            entry = home / "bin" / "aeon-mud"
            with mock.patch.object(deployment, "assert_source", side_effect=lambda _source, sha: sha):
                deployment.install(source, SHA_A, root, entry)
                real_run = deployment.subprocess.run
                def fail_active(argv, **kwargs):
                    if argv[0] == str(entry):
                        raise RuntimeError("new executable cannot start")
                    return real_run(argv, **kwargs)
                with mock.patch.object(deployment.subprocess, "run", side_effect=fail_active):
                    with self.assertRaisesRegex(RuntimeError, "cannot start"):
                        deployment.install(source, SHA_B, root, entry)
            self.assertEqual((root / "current").resolve(), root / "releases" / SHA_A)

    def test_explicit_legacy_adoption_preserves_virtualenv_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / "source"
            (source / "aeon_mud").mkdir(parents=True)
            (source / "aeon_mud" / "__init__.py").write_text("")
            (source / "aeon_mud" / "cli.py").write_text(
                "def main():\n    return 0\n", encoding="utf-8")
            (source / "aeon_mud_client.py").write_text("# compatibility")
            root = home / "share"
            legacy = root / "venv" / "bin" / "aeon-mud"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("#!/bin/sh\nexit 0\n")
            legacy.chmod(0o755)
            entry = home / "bin" / "aeon-mud"
            entry.parent.mkdir()
            entry.symlink_to(legacy)
            with mock.patch.object(deployment, "assert_source", side_effect=lambda _source, sha: sha):
                with self.assertRaisesRegex(RuntimeError, "unmanaged"):
                    deployment.install(source, SHA_A, root, entry)
                self.assertEqual(entry.readlink(), legacy)
                self.assertEqual(deployment.install(source, SHA_A, root, entry,
                                                    adopt_legacy=True), (SHA_A, True))
                self.assertEqual((root / "legacy-entry").readlink(), legacy)
                self.assertEqual(legacy.read_text(), "#!/bin/sh\nexit 0\n")
                self.assertEqual(entry.readlink(), root / "current" / "aeon-mud.pyz")
                self.assertEqual((root / "current").resolve(), root / "releases" / SHA_A)
                self.assertEqual(deployment.install(source, SHA_A, root, entry,
                                                    adopt_legacy=True), (SHA_A, False))

    def test_failed_legacy_adoption_restores_original_command(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / "source"
            (source / "aeon_mud").mkdir(parents=True)
            (source / "aeon_mud" / "__init__.py").write_text("")
            (source / "aeon_mud" / "cli.py").write_text(
                "def main():\n    return 0\n", encoding="utf-8")
            (source / "aeon_mud_client.py").write_text("# compatibility")
            root = home / "share"
            legacy = root / "venv" / "bin" / "aeon-mud"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("#!/bin/sh\nexit 0\n")
            entry = home / "bin" / "aeon-mud"
            entry.parent.mkdir()
            entry.symlink_to(legacy)
            real_run = deployment.subprocess.run
            def refuse_active(argv, **kwargs):
                if argv[0] == str(entry):
                    raise RuntimeError("activation health check failed")
                return real_run(argv, **kwargs)
            with mock.patch.object(deployment, "assert_source", return_value=SHA_A), \
                 mock.patch.object(deployment.subprocess, "run", side_effect=refuse_active):
                with self.assertRaisesRegex(RuntimeError, "activation health"):
                    deployment.install(source, SHA_A, root, entry, adopt_legacy=True)
            self.assertEqual(entry.readlink(), legacy)
            self.assertFalse((root / "current").exists())
            self.assertEqual(legacy.read_text(), "#!/bin/sh\nexit 0\n")

    def test_legacy_adoption_rejects_other_links_and_occupied_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / "source"
            (source / "aeon_mud").mkdir(parents=True)
            (source / "aeon_mud" / "__init__.py").write_text("")
            (source / "aeon_mud" / "cli.py").write_text(
                "def main():\n    return 0\n", encoding="utf-8")
            (source / "aeon_mud_client.py").write_text("# compatibility")
            root = home / "share"
            legacy = root / "venv" / "bin" / "aeon-mud"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("#!/bin/sh\nexit 0\n")
            entry = home / "bin" / "aeon-mud"
            entry.parent.mkdir()
            other = home / "other"
            other.write_text("unmanaged")
            entry.symlink_to(other)
            with mock.patch.object(deployment, "assert_source", return_value=SHA_A):
                with self.assertRaisesRegex(RuntimeError, "unmanaged"):
                    deployment.install(source, SHA_A, root, entry, adopt_legacy=True)
                entry.unlink()
                entry.symlink_to(legacy)
                (root / "legacy-entry").symlink_to(other)
                with self.assertRaisesRegex(RuntimeError, "backup path"):
                    deployment.install(source, SHA_A, root, entry, adopt_legacy=True)
            self.assertEqual(entry.readlink(), legacy)
            self.assertFalse((root / "current").exists())

    def test_unmanaged_binary_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            entry = home / "bin" / "aeon-mud"
            entry.parent.mkdir()
            entry.write_text("unmanaged")
            with mock.patch.object(deployment, "assert_source", return_value=SHA_A):
                with self.assertRaisesRegex(RuntimeError, "unmanaged"):
                    deployment.install(home, SHA_A, home / "share", entry)
            self.assertEqual(entry.read_text(), "unmanaged")


if __name__ == "__main__":
    unittest.main()
