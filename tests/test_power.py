"""
core/power.py's keep_awake() shells out to a different sleep-inhibition
mechanism per OS. All three branches are mocked here since none of them can
actually be exercised for real in CI regardless of which OS it runs on.
"""
import unittest
from unittest.mock import MagicMock, patch

from core.power import keep_awake


class LinuxSleepInhibitionTests(unittest.TestCase):
    def test_uses_systemd_inhibit_when_available(self):
        fake_proc = MagicMock()
        with patch("core.power.platform.system", return_value="Linux"), \
             patch("core.power.shutil.which", return_value="/usr/bin/systemd-inhibit"), \
             patch("core.power.subprocess.Popen", return_value=fake_proc) as mock_popen:
            with keep_awake(reason="unit test render"):
                pass

        cmd = mock_popen.call_args.args[0]
        self.assertEqual(cmd[0], "systemd-inhibit")
        self.assertIn("--what=sleep:idle", cmd)
        self.assertIn("--why=unit test render", cmd)
        self.assertTrue(fake_proc.terminate.called)

    def test_no_op_when_systemd_inhibit_missing(self):
        with patch("core.power.platform.system", return_value="Linux"), \
             patch("core.power.shutil.which", return_value=None), \
             patch("core.power.subprocess.Popen") as mock_popen:
            with keep_awake():
                pass  # must not raise
        self.assertFalse(mock_popen.called)

    def test_still_yields_if_popen_raises(self):
        with patch("core.power.platform.system", return_value="Linux"), \
             patch("core.power.shutil.which", return_value="/usr/bin/systemd-inhibit"), \
             patch("core.power.subprocess.Popen", side_effect=OSError("boom")):
            entered = False
            with keep_awake():
                entered = True
        self.assertTrue(entered)


class MacosSleepInhibitionTests(unittest.TestCase):
    def test_uses_caffeinate(self):
        fake_proc = MagicMock()
        with patch("core.power.platform.system", return_value="Darwin"), \
             patch("core.power.subprocess.Popen", return_value=fake_proc) as mock_popen:
            with keep_awake():
                pass
        self.assertEqual(mock_popen.call_args.args[0][0], "caffeinate")
        self.assertTrue(fake_proc.terminate.called)


class WindowsSleepInhibitionTests(unittest.TestCase):
    def test_sets_and_resets_execution_state(self):
        fake_ctypes = MagicMock()
        with patch("core.power.platform.system", return_value="Windows"), \
             patch.dict("sys.modules", {"ctypes": fake_ctypes}):
            with keep_awake():
                pass
        self.assertEqual(fake_ctypes.windll.kernel32.SetThreadExecutionState.call_count, 2)


if __name__ == "__main__":
    unittest.main()
