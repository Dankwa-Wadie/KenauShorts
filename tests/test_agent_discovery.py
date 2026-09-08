"""
Discovery, licence verification, and editorial selection — the parts of
core/agent.py that decide what gets posted and whether it's legal to repost.
All network calls are mocked; nothing here hits a real API.
"""
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from core.agent import (
    Candidate,
    _iso8601_seconds,
    _video_id,
    discover_youtube,
    is_free_image_url,
    pick_stories,
    verify_youtube_licenses,
)


class VideoIdAndDurationTests(unittest.TestCase):
    def test_video_id_extracts_from_watch_url(self):
        self.assertEqual(_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ"), "dQw4w9WgXcQ")

    def test_video_id_empty_for_non_youtube_url(self):
        self.assertEqual(_video_id("https://v.redd.it/abc123"), "")

    def test_iso8601_seconds(self):
        self.assertEqual(_iso8601_seconds("PT1M30S"), 90)
        self.assertEqual(_iso8601_seconds("PT45S"), 45)
        self.assertEqual(_iso8601_seconds(""), 0)


class FreeImageUrlTests(unittest.TestCase):
    def test_commons_path_is_free(self):
        self.assertTrue(is_free_image_url("https://upload.wikimedia.org/wikipedia/commons/a/b/foo.jpg"))

    def test_local_wiki_path_is_not_free(self):
        self.assertFalse(is_free_image_url("https://upload.wikimedia.org/wikipedia/en/a/b/poster.jpg"))


class VerifyYoutubeLicensesTests(unittest.TestCase):
    def test_non_youtube_candidates_pass_through_untouched(self):
        reddit_clip = Candidate(kind="clip", key="reddit_xyz", title="t", url="https://v.redd.it/abc", source="reddit")
        kept = verify_youtube_licenses([reddit_clip], api_key="")
        self.assertEqual([c.key for c in kept], ["reddit_xyz"])

    def test_no_api_key_keeps_only_pre_declared_trusted_channels(self):
        trusted = Candidate(kind="clip", key="yt_nasa", title="t", url="https://www.youtube.com/watch?v=nasa1234567",
                             source="youtube", licence="public-domain")
        untrusted = Candidate(kind="clip", key="yt_rand", title="t", url="https://www.youtube.com/watch?v=rand1234567",
                               source="youtube", licence="unknown")
        kept = verify_youtube_licenses([trusted, untrusted], api_key="")
        self.assertEqual([c.key for c in kept], ["yt_nasa"])

    @patch("core.agent.requests.get")
    def test_drops_live_and_all_rights_reserved_keeps_cc(self, mock_get):
        cand_cc = Candidate(kind="clip", key="yt_cc", title="t", url="https://www.youtube.com/watch?v=cc123456789", source="youtube")
        cand_norights = Candidate(kind="clip", key="yt_nr", title="t", url="https://www.youtube.com/watch?v=nr123456789", source="youtube")
        cand_live = Candidate(kind="clip", key="yt_live", title="t", url="https://www.youtube.com/watch?v=li123456789", source="youtube")

        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"items": [
            {"id": "cc123456789", "status": {"privacyStatus": "public", "license": "creativeCommon"},
             "contentDetails": {"duration": "PT20S"}, "snippet": {"liveBroadcastContent": "none"}},
            {"id": "nr123456789", "status": {"privacyStatus": "public", "license": "youtube"},
             "contentDetails": {"duration": "PT20S"}, "snippet": {"liveBroadcastContent": "none"}},
            {"id": "li123456789", "status": {"privacyStatus": "public", "license": "creativeCommon"},
             "contentDetails": {"duration": "PT20S"}, "snippet": {"liveBroadcastContent": "live"}},
        ]}
        mock_get.return_value = resp

        kept = verify_youtube_licenses([cand_cc, cand_norights, cand_live], api_key="FAKEKEY", max_seconds=35)
        self.assertEqual([c.key for c in kept], ["yt_cc"])

    @patch("core.agent.requests.get")
    def test_drops_clips_longer_than_max_seconds(self, mock_get):
        cand = Candidate(kind="clip", key="yt_long", title="t", url="https://www.youtube.com/watch?v=lo123456789", source="youtube")
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"items": [
            {"id": "lo123456789", "status": {"privacyStatus": "public", "license": "creativeCommon"},
             "contentDetails": {"duration": "PT2M"}, "snippet": {"liveBroadcastContent": "none"}},
        ]}
        mock_get.return_value = resp
        kept = verify_youtube_licenses([cand], api_key="FAKEKEY", max_seconds=35)
        self.assertEqual(kept, [])


class DiscoverYoutubeTests(unittest.TestCase):
    def test_no_api_key_returns_nothing(self):
        self.assertEqual(discover_youtube(["some query"], api_key=""), [])

    def test_no_queries_returns_nothing_even_with_key(self):
        self.assertEqual(discover_youtube([], api_key="FAKEKEY"), [])

    @patch("core.agent.requests.get")
    def test_search_builds_clip_candidates(self, mock_get):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"items": [
            {"id": {"videoId": "abc12345678"},
             "snippet": {"title": "Cool clip", "channelTitle": "SomeChannel",
                         "description": "desc", "publishedAt": "2026-01-01T00:00:00Z"}},
        ]}
        mock_get.return_value = resp

        cands = discover_youtube(["test query"], api_key="FAKEKEY", cc_only=True)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].key, "yt_abc12345678")
        self.assertEqual(cands[0].kind, "clip")
        self.assertEqual(mock_get.call_args.kwargs["params"]["videoLicense"], "creativeCommon")


class PickStoriesTests(unittest.TestCase):
    def setUp(self):
        self.candidates = [
            Candidate(kind="clip", key="a", title="Story A", url="https://x/a", source="reddit", channel="r/x", text="details a"),
            Candidate(kind="clip", key="b", title="Story B", url="https://x/b", source="reddit", channel="r/x", text="details b"),
            Candidate(kind="clip", key="c", title="Story C", url="https://x/c", source="reddit", channel="r/x", text="details c"),
        ]
        for var in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            os.environ.pop(var, None)

    def tearDown(self):
        for var in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            os.environ.pop(var, None)

    def test_empty_candidates_returns_no_picks(self):
        self.assertEqual(pick_stories([], {}, n=3), [])

    def test_falls_back_to_heuristic_when_no_provider_key(self):
        picks = pick_stories(self.candidates, {}, n=3)
        self.assertEqual([p.candidate.key for p in picks], ["a", "b", "c"])

    def test_multi_pick_response_is_ordered_best_first(self):
        os.environ["GEMINI_API_KEY"] = "fake"
        mock_response = json.dumps({"picks": [
            {"pick_index": 2, "headline": "BEST", "caption": "c", "title": "t", "description": "d", "hashtags": ["x"]},
            {"pick_index": 0, "headline": "BACKUP", "caption": "c2", "title": "t2", "description": "d2", "hashtags": ["y"]},
        ]})
        with patch("core.agent._call_gemini", return_value=mock_response):
            picks = pick_stories(self.candidates, {}, n=3)
        self.assertEqual([(p.candidate.key, p.headline) for p in picks], [("c", "BEST"), ("a", "BACKUP")])

    def test_malformed_json_falls_back_to_heuristic(self):
        os.environ["GEMINI_API_KEY"] = "fake"
        with patch("core.agent._call_gemini", return_value="not json at all"):
            picks = pick_stories(self.candidates, {}, n=2)
        self.assertEqual([p.candidate.key for p in picks], ["a", "b"])

    def test_out_of_range_pick_index_is_dropped_not_fatal(self):
        os.environ["GEMINI_API_KEY"] = "fake"
        mock_response = json.dumps({"picks": [
            {"pick_index": 99, "headline": "BAD", "caption": "", "title": "", "description": "", "hashtags": []},
            {"pick_index": 1, "headline": "GOOD", "caption": "", "title": "", "description": "", "hashtags": []},
        ]})
        with patch("core.agent._call_gemini", return_value=mock_response):
            picks = pick_stories(self.candidates, {}, n=3)
        self.assertEqual([(p.candidate.key, p.headline) for p in picks], [("b", "GOOD")])


if __name__ == "__main__":
    unittest.main()
