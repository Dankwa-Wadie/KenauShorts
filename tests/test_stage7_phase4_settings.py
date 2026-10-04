"""
tests/test_stage7_phase4_settings.py — Tests for Stage 7 Phase 4 Milestone 1:
Configuration Integrity, Cross-Process Locking & Atomic Settings Persistence.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from studio import settings
from studio.settings import (
    DEFAULT_SETTINGS,
    ConfigurationCorruptError,
    SettingsError,
    SettingsLockError,
    _settings_lock_guard,
    load_settings,
    update_settings,
    validate_settings_schema,
)
import studio.server as server


class SettingsSchemaTests(unittest.TestCase):
    """Unit tests for schema validation, type checking, and unknown field preservation."""

    def test_valid_settings_accepted(self):
        valid = {
            "enabled": True,
            "interval_hours": 4,
            "mode": "publish",
            "auto_discovery_publish_opt_in": False,
            "next_run": 1000.5,
            "consecutive_failures": 0,
        }
        res = validate_settings_schema(valid)
        self.assertEqual(res["enabled"], True)
        self.assertEqual(res["interval_hours"], 4)
        self.assertEqual(res["mode"], "publish")

    def test_top_level_non_dict_rejected(self):
        for invalid in ([], "string", 123, True, None):
            with self.assertRaises(ConfigurationCorruptError):
                validate_settings_schema(invalid)  # type: ignore

    def test_rejects_bool_for_interval_hours(self):
        with self.assertRaises(ConfigurationCorruptError):
            validate_settings_schema({"interval_hours": True})
        with self.assertRaises(ConfigurationCorruptError):
            validate_settings_schema({"interval_hours": False})

    def test_rejects_bool_for_next_run(self):
        with self.assertRaises(ConfigurationCorruptError):
            validate_settings_schema({"next_run": True})

    def test_rejects_bool_for_consecutive_failures(self):
        with self.assertRaises(ConfigurationCorruptError):
            validate_settings_schema({"consecutive_failures": True})

    def test_rejects_out_of_range_interval_hours(self):
        for out_of_range in (0, 0.5, -1, 169, 200):
            with self.assertRaises(ConfigurationCorruptError):
                validate_settings_schema({"interval_hours": out_of_range})

    def test_rejects_negative_next_run_and_failures(self):
        with self.assertRaises(ConfigurationCorruptError):
            validate_settings_schema({"next_run": -0.1})
        with self.assertRaises(ConfigurationCorruptError):
            validate_settings_schema({"consecutive_failures": -1})

    def test_rejects_invalid_mode_enum(self):
        for invalid_mode in ("unapproved", "draft", "", 123, True, None):
            with self.assertRaises(ConfigurationCorruptError):
                validate_settings_schema({"mode": invalid_mode})

    def test_normalizes_publish_approved_mode(self):
        res = validate_settings_schema({"mode": "publish_approved"})
        self.assertEqual(res["mode"], "publish")

    def test_preserves_unknown_fields_verbatim(self):
        doc = {
            "enabled": True,
            "custom_debug_flag": 1234,
            "telemetry_endpoint": "https://example.com/log",
            "nested_custom": {"a": 1, "b": [2, 3]},
        }
        res = validate_settings_schema(doc)
        self.assertEqual(res["custom_debug_flag"], 1234)
        self.assertEqual(res["telemetry_endpoint"], "https://example.com/log")
        self.assertEqual(res["nested_custom"], {"a": 1, "b": [2, 3]})

    def test_rejects_non_finite_numbers(self):
        for bad_val in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ConfigurationCorruptError):
                validate_settings_schema({"interval_hours": bad_val})
            with self.assertRaises(ConfigurationCorruptError):
                validate_settings_schema({"next_run": bad_val})

    def test_default_settings_keys_milestone1_scope(self):
        expected_keys = {"enabled", "interval_hours", "mode", "next_run", "auto_discovery_publish_opt_in"}
        self.assertEqual(set(DEFAULT_SETTINGS.keys()), expected_keys)
        self.assertEqual(set(server.DEFAULT_AUTOMATION.keys()), expected_keys)
        # Ensure premature Milestone 3 fields are absent
        self.assertNotIn("quota_suspended", DEFAULT_SETTINGS)
        self.assertNotIn("consecutive_failures", DEFAULT_SETTINGS)



class SettingsFilePersistenceTests(unittest.TestCase):
    """Tests for file reading, atomic writing, and corruption handling."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings_file = self.root / "studio-settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_settings_missing_file_returns_defaults(self):
        res = load_settings(settings_path=self.settings_file)
        self.assertEqual(res["enabled"], False)
        self.assertEqual(res["interval_hours"], 5)
        self.assertEqual(res["mode"], "preview")
        self.assertEqual(res["auto_discovery_publish_opt_in"], False)

    def test_load_settings_corrupt_json_fails_closed(self):
        self.settings_file.write_text("{ this is not json }", encoding="utf-8")
        with self.assertRaises(ConfigurationCorruptError):
            load_settings(settings_path=self.settings_file)

    def test_load_settings_non_object_json_fails_closed(self):
        self.settings_file.write_text('["not", "an", "object"]', encoding="utf-8")
        with self.assertRaises(ConfigurationCorruptError):
            load_settings(settings_path=self.settings_file)

    def test_update_settings_creates_file_if_missing(self):
        res = update_settings({"enabled": True, "interval_hours": 3}, settings_path=self.settings_file)
        self.assertTrue(self.settings_file.exists())
        self.assertEqual(res["enabled"], True)
        self.assertEqual(res["interval_hours"], 3)
        self.assertEqual(res["mode"], "preview")  # default preserved

    def test_update_settings_preserves_unknown_fields(self):
        self.settings_file.write_text(json.dumps({
            "enabled": False,
            "interval_hours": 4,
            "mode": "preview",
            "unknown_extra_flag": "keep_me_safe",
            "worker_threads_custom": 8,
        }), encoding="utf-8")

        res = update_settings({"enabled": True}, settings_path=self.settings_file)
        self.assertEqual(res["enabled"], True)
        self.assertEqual(res["interval_hours"], 4)
        self.assertEqual(res["unknown_extra_flag"], "keep_me_safe")
        self.assertEqual(res["worker_threads_custom"], 8)

        # Verify disk contents directly
        disk_data = json.loads(self.settings_file.read_text(encoding="utf-8"))
        self.assertEqual(disk_data["unknown_extra_flag"], "keep_me_safe")
        self.assertEqual(disk_data["worker_threads_custom"], 8)

    def test_update_settings_corrupt_existing_file_fails_closed(self):
        self.settings_file.write_text("CORRUPT DATA", encoding="utf-8")
        with self.assertRaises(ConfigurationCorruptError):
            update_settings({"enabled": True}, settings_path=self.settings_file)
        # Verify original corrupt file was not silently wiped or overwritten
        self.assertEqual(self.settings_file.read_text(encoding="utf-8"), "CORRUPT DATA")

    def test_update_settings_simulated_write_failure_cleans_tmp_and_preserves_original(self):
        initial_data = {"enabled": False, "interval_hours": 2, "mode": "preview"}
        self.settings_file.write_text(json.dumps(initial_data), encoding="utf-8")

        with patch("os.replace", side_effect=OSError("Disk replacement simulated error")):
            with self.assertRaises(SettingsError):
                update_settings({"enabled": True}, settings_path=self.settings_file)

        # Original file is preserved completely
        disk_data = json.loads(self.settings_file.read_text(encoding="utf-8"))
        self.assertEqual(disk_data["enabled"], False)
        self.assertEqual(disk_data["interval_hours"], 2)

        # Ensure no leftover temporary files in directory
        tmps = list(self.root.glob("*.tmp"))
        self.assertEqual(len(tmps), 0)


class SettingsThreadConcurrencyTests(unittest.TestCase):
    """Tests multi-thread concurrent read-modify-write safety."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings_file = self.root / "studio-settings.json"
        # Seed initial file
        self.settings_file.write_text(json.dumps({
            "enabled": True,
            "interval_hours": 1,
            "mode": "publish",
            "thread_counter": 0,
            "unknown_metadata": "thread_safe",
        }), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_thread_increments_no_lost_updates(self):
        num_threads = 10
        increments_per_thread = 10
        barrier = threading.Barrier(num_threads)
        errors = []

        def worker():
            try:
                barrier.wait()
                for _ in range(increments_per_thread):
                    # Atomic read-modify-write under lock
                    lock_file = self.root / "studio-settings.lock"
                    with _settings_lock_guard(lock_path=lock_file, timeout=15.0):
                        cur = load_settings(settings_path=self.settings_file, timeout=15.0)
                        new_cnt = cur.get("thread_counter", 0) + 1
                        update_settings({"thread_counter": new_cnt}, settings_path=self.settings_file, timeout=15.0)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Thread errors encountered: {errors}")
        final_data = json.loads(self.settings_file.read_text(encoding="utf-8"))
        expected_total = num_threads * increments_per_thread
        self.assertEqual(final_data["thread_counter"], expected_total)
        self.assertEqual(final_data["unknown_metadata"], "thread_safe")

    def test_lock_reentrancy_in_same_thread(self):
        lock_file = self.root / "studio-settings.lock"
        with _settings_lock_guard(lock_path=lock_file, timeout=5.0):
            # Nested call to load_settings (which also acquires lock_file)
            loaded = load_settings(settings_path=self.settings_file, timeout=5.0)
            self.assertEqual(loaded["thread_counter"], 0)
            # Nested call to update_settings
            updated = update_settings({"thread_counter": 42}, settings_path=self.settings_file, timeout=5.0)
            self.assertEqual(updated["thread_counter"], 42)
            # Double-nested explicit lock guard
            with _settings_lock_guard(lock_path=lock_file, timeout=5.0):
                loaded2 = load_settings(settings_path=self.settings_file, timeout=5.0)
                self.assertEqual(loaded2["thread_counter"], 42)
        # Verify outside lock
        final = load_settings(settings_path=self.settings_file, timeout=5.0)
        self.assertEqual(final["thread_counter"], 42)



class SettingsMultiProcessConcurrencyTests(unittest.TestCase):
    """Tests multi-process concurrent read-modify-write safety using independent Python processes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings_file = self.root / "studio-settings.json"
        self.settings_file.write_text(json.dumps({
            "enabled": True,
            "interval_hours": 3,
            "mode": "publish",
            "global_counter": 0,
            "preserved_meta": "multi_process",
        }), encoding="utf-8")

    def tearDown(self):
        time.sleep(0.1)
        try:
            self.tmp.cleanup()
        except Exception:
            time.sleep(0.5)
            try:
                self.tmp.cleanup()
            except Exception:
                pass

    def test_multiprocess_distinct_keys_and_counter_stress(self):
        """
        Spawn 5 independent subprocesses updating distinct process keys
        and incrementing a shared counter under OS file lock.
        """
        script = """
import sys
import time
from pathlib import Path
from studio.settings import update_settings, load_settings, _settings_lock_guard

settings_path = Path(sys.argv[1])
proc_id = sys.argv[2]
iterations = int(sys.argv[3])
lock_path = settings_path.parent / "studio-settings.lock"

for i in range(iterations):
    with _settings_lock_guard(lock_path=lock_path, timeout=15.0):
        data = load_settings(settings_path=settings_path, timeout=15.0)
        cnt = data.get("global_counter", 0) + 1
        update_settings({
            "global_counter": cnt,
            f"proc_{proc_id}_last": i + 1,
        }, settings_path=settings_path, timeout=15.0)
    time.sleep(0.01)
"""
        num_procs = 5
        iterations = 6
        processes = []

        repo_root = str(Path(__file__).resolve().parent.parent)
        env = dict(os.environ)
        env["PYTHONPATH"] = repo_root

        for p_id in range(num_procs):
            cmd = [
                sys.executable,
                "-c", script,
                str(self.settings_file),
                str(p_id),
                str(iterations),
            ]
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            processes.append(proc)

        for p_id, proc in enumerate(processes):
            stdout, stderr = proc.communicate(timeout=30)
            self.assertEqual(
                proc.returncode, 0,
                f"Subprocess {p_id} failed with exit code {proc.returncode}.\nStderr: {stderr.decode('utf-8', errors='ignore')}"
            )

        final_data = json.loads(self.settings_file.read_text(encoding="utf-8"))
        expected_counter = num_procs * iterations
        self.assertEqual(final_data["global_counter"], expected_counter)
        self.assertEqual(final_data["preserved_meta"], "multi_process")
        for p_id in range(num_procs):
            self.assertEqual(final_data.get(f"proc_{p_id}_last"), iterations)

    def test_multiprocess_lock_timeout_fails_closed(self):
        """
        One subprocess holds the file lock for 2 seconds while another subprocess
        attempts to acquire it with a 0.5-second timeout and fails closed.
        """
        holder_script = """
import sys, time
from pathlib import Path
from studio.settings import _settings_lock_guard

settings_path = Path(sys.argv[1])
lock_path = settings_path.parent / "studio-settings.lock"
with _settings_lock_guard(lock_path=lock_path, timeout=5.0):
    time.sleep(2.0)
"""
        repo_root = str(Path(__file__).resolve().parent.parent)
        env = dict(os.environ)
        env["PYTHONPATH"] = repo_root

        # Start holder process in background
        holder_proc = subprocess.Popen(
            [sys.executable, "-c", holder_script, str(self.settings_file)],
            env=env,
        )

        try:
            # Wait briefly to ensure holder acquires lock
            time.sleep(0.3)
            start = time.time()
            lock_path = self.settings_file.parent / "studio-settings.lock"
            with self.assertRaises(SettingsLockError):
                with _settings_lock_guard(lock_path=lock_path, timeout=0.5):
                    pass
            elapsed = time.time() - start
            # Verify timeout was respected (between 0.4s and 1.5s)
            self.assertGreaterEqual(elapsed, 0.45)
            self.assertLess(elapsed, 1.8)
        finally:
            holder_proc.wait(timeout=5)

    def test_multiprocess_kill_releases_os_lock(self):
        """
        On Windows/POSIX, abruptly terminating a subprocess holding the file lock
        automatically releases the OS handle, allowing subsequent acquisition.
        """
        holder_script = """
import sys, time
from pathlib import Path
from studio.settings import _settings_lock_guard

settings_path = Path(sys.argv[1])
lock_path = settings_path.parent / "studio-settings.lock"
with _settings_lock_guard(lock_path=lock_path, timeout=5.0):
    sys.stdout.write("LOCKED\\n")
    sys.stdout.flush()
    time.sleep(30.0)
"""
        repo_root = str(Path(__file__).resolve().parent.parent)
        env = dict(os.environ)
        env["PYTHONPATH"] = repo_root

        holder_proc = subprocess.Popen(
            [sys.executable, "-c", holder_script, str(self.settings_file)],
            env=env,
            stdout=subprocess.PIPE,
        )

        try:
            # Wait until process confirms it acquired the lock
            line = holder_proc.stdout.readline().decode().strip()
            self.assertEqual(line, "LOCKED")

            # Terminate the holder process abruptly (simulate crash/kill)
            holder_proc.kill()
            holder_proc.wait(timeout=5)

            # A subsequent acquisition must now succeed immediately
            lock_path = self.settings_file.parent / "studio-settings.lock"
            acquired = False
            with _settings_lock_guard(lock_path=lock_path, timeout=2.0):
                acquired = True
            self.assertTrue(acquired)
        finally:
            if holder_proc.stdout:
                try:
                    holder_proc.stdout.close()
                except Exception:
                    pass
            if holder_proc.poll() is None:
                holder_proc.kill()
            try:
                holder_proc.wait(timeout=5)
            except Exception:
                pass


class ServerSettingsIntegrationTests(unittest.TestCase):
    """Tests server.py integration with studio.settings."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings_file = self.root / "studio-settings.json"

        # Patch server ROOT to temporary directory
        self.server_root_patch = patch.object(server, "ROOT", self.root)
        self.server_root_patch.start()

    def tearDown(self):
        self.server_root_patch.stop()
        self.tmp.cleanup()

    def test_get_automation_settings_returns_defaults_when_file_missing(self):
        auto = server.get_automation_settings()
        self.assertEqual(auto["enabled"], False)
        self.assertEqual(auto["interval_hours"], 5)
        self.assertEqual(auto["mode"], "preview")
        self.assertEqual(auto["auto_discovery_publish_opt_in"], False)

    def test_get_automation_settings_loads_configured_file(self):
        self.settings_file.write_text(json.dumps({
            "enabled": True,
            "interval_hours": 3,
            "mode": "publish",
            "next_run": 12345.0,
            "extra_custom_key": 999,
        }), encoding="utf-8")

        auto = server.get_automation_settings()
        self.assertEqual(auto["enabled"], True)
        self.assertEqual(auto["interval_hours"], 3)
        self.assertEqual(auto["mode"], "publish")
        self.assertEqual(auto["next_run"], 12345.0)
        self.assertEqual(auto["extra_custom_key"], 999)

    def test_server_post_settings_persists_safely_and_preserves_unknown(self):
        self.settings_file.write_text(json.dumps({
            "enabled": False,
            "interval_hours": 6,
            "mode": "preview",
            "server_custom_telemetry": True,
        }), encoding="utf-8")

        # Simulate StudioHandler post to /api/settings
        class DummyHandler:
            pass

        data = {
            "automation": {
                "enabled": True,
                "interval_hours": 2,
                "mode": "publish",
            }
        }

        # Emulate the /api/settings handler logic in server.py
        auto = data.get("automation")
        server.studio_settings.update_settings(auto, settings_path=server.ROOT / "studio-settings.json")

        saved = server.get_automation_settings()
        self.assertEqual(saved["enabled"], True)
        self.assertEqual(saved["interval_hours"], 2)
        self.assertEqual(saved["mode"], "publish")
        self.assertEqual(saved["server_custom_telemetry"], True)

    def test_server_settings_error_sanitization_corrupt_file(self):
        # Corrupt the settings file
        self.settings_file.write_text("{ corrupt json", encoding="utf-8")
        handler = server.StudioHandler.__new__(server.StudioHandler)
        with self.assertRaises(ValueError) as ctx:
            handler.mutate("/api/settings", {"automation": {"enabled": True, "interval_hours": 2, "mode": "preview"}})
        self.assertEqual(str(ctx.exception), "Settings file or payload is invalid or corrupt.")
        self.assertNotIn(str(self.root), str(ctx.exception))

    def test_server_settings_error_sanitization_lock_timeout(self):
        handler = server.StudioHandler.__new__(server.StudioHandler)
        with patch.object(server.studio_settings, "update_settings", side_effect=server.studio_settings.SettingsLockError("Lock timed out on /some/secret/path")):
            with self.assertRaises(ValueError) as ctx:
                handler.mutate("/api/settings", {"automation": {"enabled": True, "interval_hours": 2, "mode": "preview"}})
            self.assertEqual(str(ctx.exception), "Settings are currently locked by another operation. Try again shortly.")
            self.assertNotIn("path", str(ctx.exception))

    def test_server_settings_error_sanitization_generic_settings_error(self):
        handler = server.StudioHandler.__new__(server.StudioHandler)
        with patch.object(server.studio_settings, "update_settings", side_effect=server.studio_settings.SettingsError("Generic atomic write error on /some/secret/path")):
            with self.assertRaises(ValueError) as ctx:
                handler.mutate("/api/settings", {"automation": {"enabled": True, "interval_hours": 2, "mode": "preview"}})
            self.assertEqual(str(ctx.exception), "Failed to save settings.")
            self.assertNotIn("path", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

