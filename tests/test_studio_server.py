"""
studio/server.py security surface: Host-header (DNS rebinding) checking,
CSRF + Origin enforcement on mutating requests, and path-traversal
containment on /media/ and static file serving. These were the findings from
the original security review of the release, and this locks the fixes in.
"""
import http.client
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import studio.server as server
import studio.store as store
from http.server import ThreadingHTTPServer


class StudioServerSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        (cls.root / "out").mkdir()
        (cls.root / "assets").mkdir()
        web_dir = cls.root / "studio" / "web"
        web_dir.mkdir(parents=True)
        (web_dir / "index.html").write_text("<html>ok</html>")

        cls.stack = patch.multiple(server, ROOT=cls.root, WEB_DIR=web_dir)
        cls.stack.start()
        cls.store_stack = patch.multiple(store, ROOT=cls.root, DB=cls.root / "test.sqlite3")
        cls.store_stack.start()

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.StudioHandler)
        cls.port = cls.httpd.server_address[1]
        cls.port_stack = patch.object(server, "PORT", cls.port)
        cls.port_stack.start()
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join()
        cls.port_stack.stop()
        cls.store_stack.stop()
        cls.stack.stop()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body, headers or {})
        resp = conn.getresponse()
        data = resp.read()
        status = resp.status
        conn.close()
        return status, data

    def test_correct_host_is_allowed(self):
        status, data = self.request("GET", "/api/status", headers={"Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)
        self.assertIn(b"csrf", data)

    def test_wrong_host_is_rejected(self):
        status, _ = self.request("GET", "/api/status", headers={"Host": "attacker.example"})
        self.assertEqual(status, 403)

    def test_get_job_by_id_returns_its_recorded_result(self):
        # This is what the Connections page polls after starting a
        # youtube_connect job — without it, a job that fails after the
        # initial "started" response (missing dependency, port conflict,
        # bad client_secret.json) has no way to surface its real error.
        store.put("jobs", "job123", {
            "id": "job123", "status": "failed",
            "log": "google-auth-oauthlib not installed",
        })
        status, data = self.request("GET", "/api/job?id=job123", headers={"Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)
        body = json.loads(data)
        self.assertEqual(body["status"], "failed")
        self.assertIn("google-auth-oauthlib", body["log"])

    def test_get_job_missing_id_returns_404(self):
        status, data = self.request("GET", "/api/job?id=does-not-exist", headers={"Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(status, 404)

    def test_post_without_csrf_or_origin_is_rejected(self):
        status, _ = self.request(
            "POST", "/api/job", json.dumps({"action": "preview"}),
            {"Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)

    def test_post_with_wrong_csrf_token_is_rejected(self):
        status, _ = self.request(
            "POST", "/api/settings", json.dumps({}),
            {"Host": f"127.0.0.1:{self.port}", "Origin": f"http://127.0.0.1:{self.port}",
             "X-Studio-Token": "wrong-token", "Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)

    def test_post_with_correct_host_origin_and_csrf_succeeds(self):
        status, data = self.request(
            "POST", "/api/settings", json.dumps({}),
            {"Host": f"127.0.0.1:{self.port}", "Origin": f"http://127.0.0.1:{self.port}",
             "X-Studio-Token": server.CSRF, "Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["status"], "updated")

    def test_media_path_traversal_is_blocked(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        # putrequest with skip_host lets us send a raw, unnormalised path —
        # a plain conn.request() would collapse the ".." before it's sent.
        conn.putrequest("GET", "/media/out/../../../../../../etc/passwd", skip_host=True)
        conn.putheader("Host", f"127.0.0.1:{self.port}")
        conn.endheaders()
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 404)

    def test_legitimate_media_file_is_served(self):
        media_file = server.ROOT / "out" / "clip.mp4"
        media_file.write_bytes(b"0123456789")
        status, data = self.request("GET", "/media/out/clip.mp4", headers={"Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)
        self.assertEqual(data, b"0123456789")

    def test_range_request_returns_partial_content(self):
        media_file = server.ROOT / "out" / "clip2.mp4"
        media_file.write_bytes(b"0123456789")
        status, data = self.request(
            "GET", "/media/out/clip2.mp4",
            headers={"Host": f"127.0.0.1:{self.port}", "Range": "bytes=2-5"},
        )
        self.assertEqual(status, 206)
        self.assertEqual(data, b"2345")

    def test_config_validator_rejects_malicious_style_payload(self):
        status, data = self.request(
            "POST", "/api/settings",
            json.dumps({"config": {"layout": {"border_color": "<script>alert(1)</script>"}}}),
            {"Host": f"127.0.0.1:{self.port}", "Origin": f"http://127.0.0.1:{self.port}",
             "X-Studio-Token": server.CSRF, "Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertIn("Border color", json.loads(data)["error"])


if __name__ == "__main__":
    unittest.main()
