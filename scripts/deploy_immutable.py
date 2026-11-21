"""Immutable, no-daemon aeon-mud installation controlled by the owner Ansible role.

Only the reviewed SHA from the checked-out source may be installed. No sudo,
network requests, credentials or system services are involved.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipapp

SHA = re.compile(r"[0-9a-f]{40}\Z")


def source_revision(source: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "--verify", "HEAD"],
        text=True, timeout=20,
    ).strip().lower()


def assert_source(source: Path, expected_sha: str) -> str:
    expected = expected_sha.lower()
    if not SHA.fullmatch(expected):
        raise ValueError("expected SHA must be exactly 40 hex characters")
    if source_revision(source) != expected:
        raise ValueError("source revision differs from authorized SHA")
    dirty = subprocess.check_output(
        ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"],
        text=True, timeout=20,
    )
    if dirty.strip():
        raise ValueError("tracked source is dirty")
    return expected


def replace_symlink(link: Path, destination: Path) -> None:
    temporary = link.with_name(link.name + ".next")
    if temporary.is_symlink():
        temporary.unlink()
    if temporary.exists():
        raise RuntimeError("temporary activation path occupied")
    temporary.symlink_to(destination)
    try:
        os.replace(temporary, link)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def install(source: Path, sha: str, root: Path, executable: Path) -> str:
    sha = assert_source(source, sha)
    releases = root / "releases"
    target = releases / sha
    current = root / "current"
    previous = root / "previous"
    artifact = target / "aeon-mud.pyz"
    expected_executable = root / "current" / "aeon-mud.pyz"

    if (source / "aeon_mud").is_symlink() or (source / "aeon_mud_client.py").is_symlink():
        raise RuntimeError("source package must not be symlinked")
    if root.is_symlink() or releases.is_symlink() or target.is_symlink():
        raise RuntimeError("managed release root must not be a symlink")
    if current.exists() and not current.is_symlink():
        raise RuntimeError("current must be a managed symlink")
    if previous.exists() and not previous.is_symlink():
        raise RuntimeError("previous must be a managed symlink")
    if executable.exists() or executable.is_symlink():
        if not executable.is_symlink() or executable.readlink() != expected_executable:
            raise RuntimeError("refusing to replace an unmanaged aeon-mud executable")

    releases.mkdir(parents=True, exist_ok=True)
    executable.parent.mkdir(parents=True, exist_ok=True)
    if not artifact.exists():
        if target.exists():
            raise RuntimeError("incomplete release exists; refusing to overwrite")
        with tempfile.TemporaryDirectory(prefix=".aeon-stage-", dir=releases) as staging:
            staging = Path(staging)
            shutil.copytree(source / "aeon_mud", staging / "aeon_mud",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            shutil.copy2(source / "aeon_mud_client.py", staging / "aeon_mud_client.py")
            (staging / "__main__.py").write_text(
                "from aeon_mud.cli import main\nraise SystemExit(main())\n",
                encoding="utf-8",
            )
            zipapp.create_archive(staging, target=staging.parent / (sha + ".pyz"),
                                  interpreter="/usr/bin/env python3", compressed=True)
            target.mkdir(mode=0o755)
            os.replace(staging.parent / (sha + ".pyz"), artifact)
            artifact.chmod(0o755)
            (target / "REVISION").write_text(sha + "\n", encoding="ascii")
    if not artifact.is_file() or artifact.is_symlink():
        raise RuntimeError("release artifact missing or unsafe")
    if (target / "REVISION").read_text(encoding="ascii").strip() != sha:
        raise RuntimeError("immutable release revision mismatch")

    subprocess.run([sys.executable, str(artifact), "--help"],
                   check=True, timeout=30, stdout=subprocess.DEVNULL)
    old = current.resolve(strict=True) if current.is_symlink() and current.exists() else None
    if old is not None and old.parent != releases:
        raise RuntimeError("existing current release escapes managed root")
    executable_missing = not executable.is_symlink()
    if executable_missing:
        replace_symlink(executable, expected_executable)
    if old != target:
        replace_symlink(current, target)
        try:
            subprocess.run([str(executable), "--help"], check=True, timeout=30,
                           stdout=subprocess.DEVNULL)
        except Exception:
            if old is not None:
                replace_symlink(current, old)
            else:
                current.unlink(missing_ok=True)
                if executable_missing and executable.is_symlink():
                    executable.unlink()
            raise
        if old is not None:
            replace_symlink(previous, old)
    else:
        subprocess.run([str(executable), "--help"], check=True, timeout=30,
                       stdout=subprocess.DEVNULL)
    return sha, old != target or executable_missing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    if os.environ.get("AEON_MUD_DEPLOY_NODE") != "gway-001":
        parser.error("aeon-mud managed deployment is permitted on gway-001 only")
    root = Path.home() / ".local/share/aeon-mud"
    entry = Path.home() / ".local/bin/aeon-mud"
    revision, changed = install(args.source.resolve(), args.expected_sha, root, entry)
    print(("updated" if changed else "unchanged") + " aeon-mud " + revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
