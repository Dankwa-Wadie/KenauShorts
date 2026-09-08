"""Cross-platform sleep prevention for long-running render & publish jobs."""
from __future__ import annotations

import contextlib
import logging
import platform
import subprocess
import sys
from typing import Generator

LOG = logging.getLogger("kenaushorts.power")

@contextlib.contextmanager
def keep_awake(reason: str = "KenauShorts processing video job") -> Generator[None, None, None]:
    """Context manager to prevent the computer from sleeping during video rendering."""
    system = platform.system()
    proc = None

    if system == "Darwin":
        try:
            proc = subprocess.Popen(
                ["caffeinate", "-i", "-w", str(subprocess.os.getpid())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            LOG.debug("caffeinate started on macOS")
        except Exception as e:
            LOG.warning("Could not start caffeinate: %s", e)

    elif system == "Windows":
        try:
            import ctypes
            # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            LOG.debug("SetThreadExecutionState enabled on Windows")
        except Exception as e:
            LOG.warning("Could not set Windows execution state: %s", e)

    elif system == "Linux":
        # Linux systemd-inhibit or desktop inhibit could be added if running in terminal
        pass

    try:
        yield
    finally:
        if system == "Darwin" and proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        elif system == "Windows":
            try:
                import ctypes
                ES_CONTINUOUS = 0x80000000
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
                LOG.debug("SetThreadExecutionState reset on Windows")
            except Exception:
                pass
