"""
End-to-end run_pipeline behavior with the network/render/upload layers
mocked out: does a failed pick actually fall through to its backup, does a
story candidate's images actually get downloaded before rendering, and does
state.json end up correct either way.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.agent as agent_mod
from core.agent import Candidate, Pick, run_pipeline
from core.render import RenderResult


class FakeStore:
    """Minimal stand-in for studio.store, recording what run_pipeline does with it."""
    calls: list

    @classmethod
    def reset(cls):
        cls.calls = []

    @classmethod
    def draft(cls, **kwargs):
        cls.calls.append(("draft", kwargs["headline"]))
        return {"id": "video-id", "status": "ready"}

    @staticmethod
    def put(*a, **kw):
        pass

    @staticmethod
    def now():
        return "2026-01-01T00:00:00"


class RunPipelineFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps({
            "posting": {"max_clip_seconds": 10},
            "editorial": {"max_candidates": 3},
        }))
        FakeStore.reset()

    def tearDown(self):
        self.tmp.cleanup()

    def test_failed_first_pick_falls_through_to_backup(self):
        cand_bad = Candidate(kind="clip", key="bad1", title="Bad clip", url="https://x/bad", source="reddit", channel="r/x")
        cand_good = Candidate(kind="clip", key="good1", title="Good clip", url="https://x/good", source="reddit", channel="r/x")
        picks = [
            Pick(candidate=cand_bad, headline="BAD", caption="", title="T", description="D", hashtags=[]),
            Pick(candidate=cand_good, headline="GOOD", caption="", title="T2", description="D2", hashtags=[]),
        ]

        def fake_download_clip(url, out_path, max_seconds=35):
            if "bad" in url:
                return False
            out_path.write_bytes(b"fake video bytes")
            return True

        def fake_render(video, headline, out, config=None, max_seconds=None, poster=None, music=None, account=None):
            out.write_bytes(b"fake rendered mp4")
            return RenderResult(output=out, overlay=out, video_box=(0, 0, 0, 0), duration=5.0, poster=poster)

        with patch.object(agent_mod, "discover_all_candidates", return_value=[cand_bad, cand_good]), \
             patch.object(agent_mod, "pick_stories", return_value=picks), \
             patch.object(agent_mod, "download_clip", fake_download_clip), \
             patch.object(agent_mod, "render", fake_render), \
             patch.object(agent_mod, "store", FakeStore), \
             patch.object(agent_mod, "ROOT", self.root):
            run_pipeline(self.config_path, dry_run=True)

        self.assertEqual(FakeStore.calls, [("draft", "GOOD")])

        state_data = json.loads((self.root / "state.json").read_text())
        self.assertIn("bad1", state_data.get("failed", {}))
        self.assertIn("good1", state_data.get("seen", {}))

    def test_all_picks_failing_reports_failure_without_crashing(self):
        cand_bad = Candidate(kind="clip", key="bad1", title="Bad", url="https://x/bad", source="reddit", channel="r/x")
        picks = [Pick(candidate=cand_bad, headline="BAD", caption="", title="T", description="D", hashtags=[])]

        with patch.object(agent_mod, "discover_all_candidates", return_value=[cand_bad]), \
             patch.object(agent_mod, "pick_stories", return_value=picks), \
             patch.object(agent_mod, "download_clip", return_value=False), \
             patch.object(agent_mod, "store", FakeStore), \
             patch.object(agent_mod, "ROOT", self.root):
            run_pipeline(self.config_path, dry_run=True)  # must not raise

        self.assertEqual(FakeStore.calls, [])  # nothing was ever drafted

    def test_render_exception_falls_through_to_next_backup(self):
        """A render crash (bad ffmpeg input, missing asset, ...) must not take
        down the whole run when a backup pick is available."""
        cand_crashes = Candidate(kind="clip", key="crash1", title="Crashes", url="https://x/crash", source="reddit", channel="r/x")
        cand_good = Candidate(kind="clip", key="good1", title="Good", url="https://x/good", source="reddit", channel="r/x")
        picks = [
            Pick(candidate=cand_crashes, headline="CRASH", caption="", title="T", description="D", hashtags=[]),
            Pick(candidate=cand_good, headline="GOOD", caption="", title="T2", description="D2", hashtags=[]),
        ]

        def fake_download_clip(url, out_path, max_seconds=35):
            out_path.write_bytes(b"fake")
            return True

        def fake_render(video, headline, out, config=None, max_seconds=None, poster=None, music=None, account=None):
            if headline == "CRASH":
                raise RuntimeError("ffmpeg failed: simulated crash")
            out.write_bytes(b"fake rendered mp4")
            return RenderResult(output=out, overlay=out, video_box=(0, 0, 0, 0), duration=5.0, poster=poster)

        with patch.object(agent_mod, "discover_all_candidates", return_value=[cand_crashes, cand_good]), \
             patch.object(agent_mod, "pick_stories", return_value=picks), \
             patch.object(agent_mod, "download_clip", fake_download_clip), \
             patch.object(agent_mod, "render", fake_render), \
             patch.object(agent_mod, "store", FakeStore), \
             patch.object(agent_mod, "ROOT", self.root):
            run_pipeline(self.config_path, dry_run=True)  # must not raise

        self.assertEqual(FakeStore.calls, [("draft", "GOOD")])

    def test_story_pick_downloads_images_before_rendering(self):
        cand_story = Candidate(kind="story", key="story1", title="A story", url="https://x/story",
                                source="wikimedia", channel="Wikimedia Commons",
                                images=["https://example.com/a.jpg", "https://example.com/b.jpg"])
        picks = [Pick(candidate=cand_story, headline="STORY", caption="cap", title="T", description="D", hashtags=[])]
        render_story_images: list = []

        def fake_download_images(urls, dest_dir, stem):
            p = dest_dir / f"{stem}_img0.jpg"
            p.write_bytes(b"fake image bytes")
            return [p]

        def fake_render_story(headline, commentary, images, mascot, out, poster=None, config=None, duration=25.0, music=None):
            render_story_images.extend(images)
            out.write_bytes(b"fake story mp4")
            return duration

        with patch.object(agent_mod, "discover_all_candidates", return_value=[cand_story]), \
             patch.object(agent_mod, "pick_stories", return_value=picks), \
             patch.object(agent_mod, "download_images", fake_download_images), \
             patch.object(agent_mod, "render_story", fake_render_story), \
             patch.object(agent_mod, "store", FakeStore), \
             patch.object(agent_mod, "ROOT", self.root):
            run_pipeline(self.config_path, dry_run=True)

        # Every image handed to render_story must be a real local file that
        # exists on disk — not the raw remote URL wrapped in a Path().
        self.assertEqual(len(render_story_images), 1)
        for img in render_story_images:
            self.assertIsInstance(img, Path)
            self.assertTrue(img.exists())


if __name__ == "__main__":
    unittest.main()
