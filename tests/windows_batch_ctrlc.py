"""Windows batch Ctrl+C integration test using an isolated, credential-free stub."""
import ctypes
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


def main():
    if os.name != "nt":
        print("SKIP: Windows required")
        return 0
    source = Path(__file__).resolve().parents[1] / "aeon-mud.bat"
    with tempfile.TemporaryDirectory(prefix="aeon-ctrlc-") as directory:
        root = Path(directory)
        shutil.copyfile(source, root / "aeon-mud.bat")
        ready = root / "ready.flag"
        (root / "aeon_mud_client.py").write_text(
            'import pathlib, sys, time\n'
            'pathlib.Path(__file__).with_name("ready.flag").write_text("ready")\n'
            'try:\n'
            '    while True: time.sleep(1)\n'
            'except KeyboardInterrupt:\n'
            '    print("PYTHON_CAUGHT_CTRL_C", flush=True)\n'
            '    sys.exit(130)\n', encoding="utf-8",
        )
        # Do not redirect standard input: an interactive CMD confirmation
        # must have NO automatic "n" response to conceal a pending prompt.
        # Redirect only the program's output via CMD, keeping its own
        # console stdin attached to the newly allocated console.
        proc = subprocess.Popen(
            ["cmd.exe", "/d", "/c", "aeon-mud.bat > output.log 2>&1"],
            cwd=root, creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.AttachConsole.argtypes = [ctypes.c_uint]
        k.AttachConsole.restype = ctypes.c_int
        k.GenerateConsoleCtrlEvent.argtypes = [ctypes.c_uint, ctypes.c_uint]
        k.GenerateConsoleCtrlEvent.restype = ctypes.c_int
        k.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, ctypes.c_int]
        k.SetConsoleCtrlHandler.restype = ctypes.c_int
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and not ready.exists() and proc.poll() is None:
                time.sleep(0.1)
            if not ready.exists():
                raise AssertionError("Stub did not start")
            k.FreeConsole()
            if not k.AttachConsole(proc.pid):
                raise RuntimeError(f"AttachConsole failed: {ctypes.get_last_error()}")
            try:
                if not k.SetConsoleCtrlHandler(None, 1):
                    raise RuntimeError("SetConsoleCtrlHandler failed")
                if not k.GenerateConsoleCtrlEvent(0, 0):
                    raise RuntimeError(f"GenerateConsoleCtrlEvent failed: {ctypes.get_last_error()}")
                time.sleep(0.3)
            finally:
                k.FreeConsole()
            proc.wait(timeout=12)
            observed = (root / "output.log").read_text(encoding="utf-8", errors="replace")
            print("Exit:", proc.returncode, "Output:", repr(observed[-1200:]))
            assert "PYTHON_CAUGHT_CTRL_C" in observed, "Ctrl+C did not reach Python"
            assert "Terminate batch job" not in observed, "CMD requested a second confirmation"
            assert proc.returncode == 130, f"Exit status changed: {proc.returncode}"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
    # A failing CLI invocation must retain argparse's exit code. Avoid
    # PowerShell's NativeCommandError behavior on redirected native stderr.
    failure = subprocess.run(
        ["cmd.exe", "/d", "/c", str(source), "connect", "--invalid-argument-xyz"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15,
        check=False,
    )
    observed = failure.stdout.decode("utf-8", "replace")
    if failure.returncode != 2 or "unrecognized arguments" not in observed:
        raise AssertionError(f"CLI failure propagation: rc={failure.returncode}, output={observed[-600:]!r}")
    print("PASS: no second CMD prompt; exit codes 130 and 2 preserved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
