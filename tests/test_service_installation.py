"""
scripts/install_service.py registers studio/server.py with launchd/systemd/
Task Scheduler by its raw file path, not via `python -m studio.server`. That
used to crash immediately with "No module named 'studio'", because running a
script directly puts its own directory on sys.path instead of the repo root
that `from studio import store` needs — so the "Always-On Background
Automation" feature never actually started on any platform. This locks in
the fix (a self-bootstrapping sys.path insert at the top of the file).
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SERVER_SCRIPT = Path(__file__).resolve().parent.parent / "studio" / "server.py"


class DirectScriptInvocationTests(unittest.TestCase):
    def test_running_the_raw_script_path_does_not_crash(self):
        """Reproduces exactly what launchd/systemd/schtasks do: invoke the
        script by absolute path from an unrelated working directory."""
        env = dict(os.environ)
        env["KENAU_STUDIO_PORT"] = "8710"
        with tempfile.TemporaryDirectory() as unrelated_cwd:
            proc = subprocess.Popen(
                [sys.executable, str(SERVER_SCRIPT)],
                cwd=unrelated_cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                time.sleep(1.0)
                exit_code = proc.poll()
                output = proc.stdout.read() if exit_code is not None else ""
                self.assertIsNone(exit_code, f"server process exited early with output:\n{output}")
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
