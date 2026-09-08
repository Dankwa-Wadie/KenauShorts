"""
Dedup is the mechanism that stops the same story being posted twice — a bug
here means the pipeline silently reposts. See core/state.py.
"""
import tempfile
import unittest
from pathlib import Path

from core.state import State


class StateDedupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_mark_posted_marks_the_candidate_seen(self):
        state = State(self.path)
        self.assertFalse(state.is_seen("abc123"))
        state.mark_posted({
            "key": "abc123", "headline": "h", "title": "t",
            "video_path": "v.mp4", "youtube_id": "yid", "dry_run": False, "at": 1.0,
        })
        self.assertTrue(state.is_seen("abc123"))

    def test_mark_posted_survives_a_reload(self):
        state = State(self.path)
        state.mark_posted({"key": "abc123", "video_path": "v.mp4"})
        reloaded = State(self.path)
        self.assertTrue(reloaded.is_seen("abc123"))

    def test_failed_candidate_is_retried_until_the_attempt_cap(self):
        state = State(self.path)
        for _ in range(2):
            state.mark_failed("bad-key", "download error")
            self.assertFalse(state.is_seen("bad-key", max_failed_attempts=3))
        state.mark_failed("bad-key", "download error")
        self.assertTrue(state.is_seen("bad-key", max_failed_attempts=3))

    def test_mark_seen_directly(self):
        state = State(self.path)
        state.mark_seen("direct-key")
        self.assertTrue(state.is_seen("direct-key"))

    def test_reddit_cursor_round_trip(self):
        state = State(self.path)
        self.assertEqual(state.get_reddit_cursor("Tech"), 0)
        state.set_reddit_cursor("Tech", 5)
        self.assertEqual(state.get_reddit_cursor("Tech"), 5)
        reloaded = State(self.path)
        self.assertEqual(reloaded.get_reddit_cursor("Tech"), 5)


if __name__ == "__main__":
    unittest.main()
