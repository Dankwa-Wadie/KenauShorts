"""
scripts/uninstall_service.py is the counterpart to install_service.py — it
must actually remove what that script creates, and must not error out when
there's nothing installed (the common case: most users testing this never
installed the background service at all).
"""
import platform
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import scripts.uninstall_service as uninstall_service


class MacosUninstallTests(unittest.TestCase):
    def test_no_op_when_nothing_installed(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.object(uninstall_service.Path, "home", return_value=Path(home)):
                self.assertTrue(uninstall_service.uninstall_macos())

    def test_removes_an_existing_plist(self):
        with tempfile.TemporaryDirectory() as home:
            plist_dir = Path(home) / "Library" / "LaunchAgents"
            plist_dir.mkdir(parents=True)
            plist_path = plist_dir / "com.kenaushorts.studio.plist"
            plist_path.write_text("<plist>fake</plist>")

            with patch.object(uninstall_service.Path, "home", return_value=Path(home)), \
                 patch.object(uninstall_service.subprocess, "run") as mock_run:
                self.assertTrue(uninstall_service.uninstall_macos())

            self.assertFalse(plist_path.exists())
            self.assertEqual(mock_run.call_args.args[0][:2], ["launchctl", "bootout"])


class WindowsUninstallTests(unittest.TestCase):
    def test_no_op_when_task_does_not_exist(self):
        query_result = MagicMock(returncode=1)
        with patch.object(uninstall_service.subprocess, "run", return_value=query_result) as mock_run:
            self.assertTrue(uninstall_service.uninstall_windows())
        self.assertEqual(mock_run.call_args.args[0][:2], ["schtasks", "/query"])

    def test_deletes_task_when_present(self):
        query_ok = MagicMock(returncode=0)
        delete_ok = MagicMock(returncode=0)
        with patch.object(uninstall_service.subprocess, "run", side_effect=[query_ok, delete_ok]) as mock_run:
            self.assertTrue(uninstall_service.uninstall_windows())
        delete_cmd = mock_run.call_args_list[1].args[0]
        self.assertIn("/delete", delete_cmd)
        self.assertIn("KenauShortsStudio", delete_cmd)

    def test_reports_failure_without_raising(self):
        query_ok = MagicMock(returncode=0)
        delete_fails = MagicMock(returncode=1)
        with patch.object(uninstall_service.subprocess, "run", side_effect=[query_ok, delete_fails]):
            self.assertFalse(uninstall_service.uninstall_windows())


class LinuxUninstallTests(unittest.TestCase):
    def test_no_op_when_nothing_installed(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.object(uninstall_service.Path, "home", return_value=Path(home)):
                self.assertTrue(uninstall_service.uninstall_linux())

    def test_removes_an_existing_unit(self):
        with tempfile.TemporaryDirectory() as home:
            unit_dir = Path(home) / ".config" / "systemd" / "user"
            unit_dir.mkdir(parents=True)
            unit_path = unit_dir / "kenaushorts.service"
            unit_path.write_text("[Unit]\n")

            with patch.object(uninstall_service.Path, "home", return_value=Path(home)), \
                 patch.object(uninstall_service.subprocess, "run") as mock_run:
                self.assertTrue(uninstall_service.uninstall_linux())

            self.assertFalse(unit_path.exists())
            calls = [c.args[0] for c in mock_run.call_args_list]
            self.assertIn(["systemctl", "--user", "disable", "--now", "kenaushorts.service"], calls)


class MainDispatchTests(unittest.TestCase):
    def test_dispatches_to_the_right_platform_function(self):
        with patch.object(uninstall_service.platform, "system", return_value="Darwin"), \
             patch.object(uninstall_service, "uninstall_macos") as mock_fn:
            uninstall_service.main()
        mock_fn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
