"""Catch malformed embedded Python in the owner Ansible preflight."""
import unittest
from pathlib import Path


class AnsiblePreflightTests(unittest.TestCase):
    def test_embedded_python_preflight_is_valid(self):
        source = (Path(__file__).resolve().parents[1] /
                  "ansible/roles/aeon_mud/tasks/main.yml").read_text()
        marker = "      - |\n"
        self.assertEqual(source.count(marker), 1)
        embedded = source.split(marker, 1)[1].split("\n  changed_when:", 1)[0]
        code = "\n".join(line[8:] for line in embedded.splitlines())
        compiled = compile(code, "<ansible-preflight>", "exec")
        self.assertIn("from cryptography.fernet import Fernet", code)
        exec(compiled, {})


if __name__ == "__main__":
    unittest.main()
