"""studio/store.py: the sqlite record store, cross-process lock, and legacy migration."""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import studio.store as store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stack = patch.multiple(store, ROOT=self.root, DB=self.root / "test.sqlite3")
        self.stack.start()

    def tearDown(self):
        self.stack.stop()
        self.tmp.cleanup()

    def test_put_get_round_trip(self):
        store.put("videos", "abc", {"id": "abc", "status": "ready"})
        self.assertEqual(store.get("videos", "abc")["status"], "ready")

    def test_get_missing_returns_none(self):
        self.assertIsNone(store.get("videos", "does-not-exist"))

    def test_records_orders_newest_first(self):
        store.put("videos", "first", {"id": "first"})
        store.put("videos", "second", {"id": "second"})
        ids = [r["id"] for r in store.records("videos")]
        self.assertEqual(ids, ["second", "first"])

    def test_draft_creates_a_ready_record(self):
        record = store.draft(
            video_stem="short_1_abc", video_path=self.root / "out" / "short_1_abc.mp4",
            poster_path=self.root / "out" / "short_1_abc.png", headline="H", title="T",
            description="D", config={}, candidate_data={"key": "abc"},
        )
        self.assertEqual(record["status"], "ready")
        self.assertEqual(store.get("videos", "short_1_abc")["headline"], "H")

    def test_pipeline_lock_blocks_a_second_process(self):
        with store.pipeline_lock():
            self.assertTrue(store.busy())
            code = "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"
            proc = subprocess.run([sys.executable, "-c", code, str(self.root / ".pipeline.lock")],
                                  capture_output=True)
            self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(store.busy())


class ImportLegacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "out").mkdir()
        self.stack = patch.multiple(store, ROOT=self.root, DB=self.root / "test.sqlite3")
        self.stack.start()

    def tearDown(self):
        self.stack.stop()
        self.tmp.cleanup()

    def _write_state(self, posted):
        (self.root / "state.json").write_text(json.dumps({"posted": posted}))

    def test_backfills_uploaded_and_dry_run_videos(self):
        video1 = self.root / "out" / "short_111_abc.mp4"
        video1.write_bytes(b"fake")
        (self.root / "out" / "short_111_abc.png").write_bytes(b"fake")
        video2 = self.root / "out" / "short_222_def.mp4"
        video2.write_bytes(b"fake")

        self._write_state([
            {"key": "abc123", "headline": "Headline A", "title": "Title A",
             "video_path": str(video1), "youtube_id": "yt123", "dry_run": False, "at": time.time()},
            {"key": "def456", "headline": "Headline B", "title": "Title B",
             "video_path": str(video2), "youtube_id": "", "dry_run": True, "at": time.time()},
        ])

        store.import_legacy()
        records = {r["id"]: r for r in store.records("videos")}
        self.assertEqual(len(records), 2)
        self.assertEqual(records["short_111_abc"]["status"], "uploaded")
        self.assertEqual(records["short_111_abc"]["youtube_id"], "yt123")
        self.assertEqual(records["short_222_def"]["status"], "ready")
        self.assertEqual(records["short_222_def"]["youtube_id"], "")

    def test_skips_edit_renders_and_already_indexed(self):
        (self.root / "out" / "short_111_abc.mp4").write_bytes(b"fake")
        (self.root / "out" / "short_111_abc_edit_ff00aa.mp4").write_bytes(b"fake")
        self._write_state([])
        store.import_legacy()
        self.assertEqual(len(store.records("videos")), 1)

    def test_is_idempotent(self):
        (self.root / "out" / "short_111_abc.mp4").write_bytes(b"fake")
        self._write_state([])
        store.import_legacy()
        store.import_legacy()
        self.assertEqual(len(store.records("videos")), 1)

    def test_no_state_file_is_a_silent_no_op(self):
        (self.root / "out" / "short_111_abc.mp4").write_bytes(b"fake")
        store.import_legacy()  # must not raise
        self.assertEqual(store.records("videos"), [])


if __name__ == "__main__":
    unittest.main()
