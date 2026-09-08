"""
Real ffmpeg smoke tests for the render pipeline: audio passthrough, music
ducking, mascot looping, and story image compositing. Skipped entirely if
ffmpeg/ffprobe aren't on PATH (e.g. a bare CI image) rather than failing.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from core.render import has_audio_stream, probe_duration, render
from core.render_story import render_story

FFMPEG_AVAILABLE = shutil.which("ffmpeg") and shutil.which("ffprobe")


def _run(cmd):
    import subprocess
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@unittest.skipUnless(FFMPEG_AVAILABLE, "ffmpeg/ffprobe not on PATH")
class RenderFixturesMixin:
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)

        cls.clip_with_audio = cls.dir / "clip_with_audio.mp4"
        _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
              "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=3",
              "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
              "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
              str(cls.clip_with_audio)])

        cls.mascot_silent = cls.dir / "mascot_silent.mp4"
        _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
              "-f", "lavfi", "-i", "testsrc2=size=240x135:rate=24:duration=1",
              "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cls.mascot_silent)])

        cls.music = cls.dir / "music.m4a"
        _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
              "-f", "lavfi", "-i", "sine=frequency=220:duration=4",
              "-c:a", "aac", str(cls.music)])

        cls.image1 = cls.dir / "img1.png"
        _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
              "-f", "lavfi", "-i", "color=c=red:size=100x80", "-frames:v", "1", str(cls.image1)])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class HasAudioStreamTests(RenderFixturesMixin, unittest.TestCase):
    def test_detects_audio_track(self):
        self.assertTrue(has_audio_stream(self.clip_with_audio))

    def test_detects_no_audio_track(self):
        self.assertFalse(has_audio_stream(self.mascot_silent))


class RenderAudioTests(RenderFixturesMixin, unittest.TestCase):
    def test_source_audio_passes_through_when_no_music(self):
        out = self.dir / "out_passthrough.mp4"
        render(video=self.clip_with_audio, headline="Test", out=out,
               config={"account": {"name": "T"}}, max_seconds=2.0)
        self.assertTrue(has_audio_stream(out))

    def test_music_mixes_in_when_source_has_audio(self):
        out = self.dir / "out_music_mix.mp4"
        render(video=self.clip_with_audio, headline="Test", out=out,
               config={"account": {"name": "T"}, "audio": {"music_volume": 0.3}},
               max_seconds=2.0, music=self.music)
        self.assertTrue(has_audio_stream(out))

    def test_silent_clip_renders_without_crashing(self):
        out = self.dir / "out_silent.mp4"
        render(video=self.mascot_silent, headline="Test", out=out,
               config={"account": {"name": "T"}}, max_seconds=1.0)
        self.assertTrue(out.exists())


class RenderStoryTests(RenderFixturesMixin, unittest.TestCase):
    def test_missing_mascot_raises_a_clear_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            render_story(headline="x", commentary="", images=[],
                         mascot=self.dir / "does_not_exist.mp4",
                         out=self.dir / "out_missing_mascot.mp4")
        self.assertIn("mascot", str(ctx.exception))

    def test_loops_a_short_mascot_to_fill_target_duration(self):
        out = self.dir / "out_story.mp4"
        dur = render_story(headline="Headline", commentary="Commentary text",
                           images=[self.image1], mascot=self.mascot_silent, out=out,
                           config={"account": {"name": "T"}}, duration=3.0)
        self.assertAlmostEqual(dur, 3.0, delta=0.2)
        self.assertAlmostEqual(probe_duration(out), 3.0, delta=0.2)

    def test_renders_with_zero_images(self):
        out = self.dir / "out_story_no_images.mp4"
        render_story(headline="Headline only", commentary="", images=[],
                     mascot=self.mascot_silent, out=out,
                     config={"account": {"name": "T"}}, duration=1.0)
        self.assertTrue(out.exists())


class ShippedDefaultMascotTests(unittest.TestCase):
    """The repo ships assets/default_mascot.mp4 specifically so a fresh
    install can render a story card with no configuration at all — a config
    with no mascot set previously fell back to a file that never existed."""

    @unittest.skipUnless(FFMPEG_AVAILABLE, "ffmpeg/ffprobe not on PATH")
    def test_default_mascot_asset_exists_and_renders(self):
        default_mascot = Path(__file__).resolve().parent.parent / "assets" / "default_mascot.mp4"
        self.assertTrue(default_mascot.exists(), "assets/default_mascot.mp4 must ship in the repo")
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out.mp4"
            render_story(headline="Out of the box", commentary="", images=[],
                        mascot=default_mascot, out=out, duration=1.0)
            self.assertTrue(out.exists())


if __name__ == "__main__":
    unittest.main()
