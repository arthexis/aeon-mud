"""Unix owner-only credential persistence without a desktop keyring."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aeon_mud import security
from support import make_profile


@unittest.skipIf(os.name == "nt", "Unix file permissions are platform specific")
class UnixSecretsTests(unittest.TestCase):
    def test_encrypted_reuse_and_endpoint_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "secrets.json"
            profile = make_profile(name="same", host="realm.example", port=4000)
            alice = security.character_secret_key(profile, "Alice")
            bob = security.character_secret_key(profile, "Bob")
            other = security.character_secret_key(
                make_profile(name="same", host="other.example", port=4000), "Alice")
            with mock.patch.object(security.Dpapi, "available", return_value=False):
                store = security.SecretStore(path)
                store.set_password(alice, "not-in-clear")
                store.set_password(bob, "different-password")
                store.set_password(other, "third-password")
                fresh = security.SecretStore(path)
                self.assertEqual(fresh.get_password(alice), "not-in-clear")
                self.assertEqual(fresh.get_password(bob), "different-password")
                self.assertEqual(fresh.get_password(other), "third-password")
                self.assertIsNone(fresh.get_password("missing"))
                self.assertNotIn("not-in-clear", path.read_text())
                self.assertNotIn("different-password", path.read_text())
                self.assertTrue(json.loads(path.read_text())["secrets"][alice].startswith("fernet:"))
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual((path.parent / "secrets.key").stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                fresh.clear_password(alice)
                self.assertIsNone(security.SecretStore(path).get_password(alice))
                self.assertEqual(security.SecretStore(path).get_password(bob), "different-password")

    def test_refuses_unsafe_symlinks_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "secrets.json"
            with mock.patch.object(security.Dpapi, "available", return_value=False):
                path.write_text('{"secrets": {}}')
                path.chmod(0o644)
                with self.assertRaises(PermissionError):
                    security.SecretStore(path)
                path.chmod(0o600)
                store = security.SecretStore(path)
                store.set_password("character", "secret")
                key = root / "secrets.key"
                key.chmod(0o644)
                with self.assertRaises(PermissionError):
                    security.SecretStore(path).get_password("character")
                key.chmod(0o600)
                path.unlink()
                path.symlink_to(key)
                with self.assertRaises(PermissionError):
                    security.SecretStore(path)

    def test_missing_key_and_unknown_token_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.json"
            with mock.patch.object(security.Dpapi, "available", return_value=False):
                first = security.SecretStore(path)
                first.set_password("character", "secret")
                (path.parent / "secrets.key").unlink()
                with self.assertRaisesRegex(RuntimeError, "missing"):
                    security.SecretStore(path).get_password("character")
                path.write_text('{"secrets": {"character": "plaintext"}}')
                path.chmod(0o600)
                with self.assertRaisesRegex(ValueError, "unsupported"):
                    security.SecretStore(path).get_password("character")


if __name__ == "__main__":
    unittest.main()
