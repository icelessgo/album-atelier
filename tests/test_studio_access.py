"""Owner-scoped HTTPS access, strict origins and same-origin phone previews."""
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
import json
import sys
import threading
from unittest import TestCase
from unittest.mock import patch
from tempfile import TemporaryDirectory
from uuid import uuid4

from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
import proof_server
from studio_access import load_access, request_origin
import test_p1_regressions as p1

ACCESS = {"origin": "https://studio.example.ts.net", "operator_login": "owner@example.com"}


class AccessTests(TestCase):
    def test_only_exact_https_configuration_is_accepted(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "access.json"
            self.assertIsNone(load_access(path))
            path.write_text(json.dumps(ACCESS))
            self.assertEqual(load_access(path), ACCESS)
            for origin in ("http://studio.example.ts.net", "https://studio.example.ts.net/",
                           "https://attacker.invalid", "https://studio.example.ts.net:443",
                           "https://u@studio.example.ts.net", "https://studio.example.ts.net?extra=1"):
                path.write_text(json.dumps({**ACCESS, "origin": origin}))
                with self.assertRaises(ValueError):
                    load_access(path)

    def test_proxy_identity_is_never_trusted_on_a_network_listener_or_from_a_different_owner(self):
        headers = {"Host": "studio.example.ts.net", "Tailscale-User-Login": "owner@example.com"}
        self.assertEqual(request_origin(headers, "127.0.0.1", 8765, ACCESS), ACCESS["origin"])
        self.assertIsNone(request_origin(headers, "100.1.2.3", 8765, ACCESS))
        self.assertIsNone(request_origin({**headers, "Tailscale-User-Login": "friend@example.com"}, "127.0.0.1", 8765, ACCESS))
        self.assertIsNone(request_origin({"Host": headers["Host"]}, "127.0.0.1", 8765, ACCESS))
        # A proxy rewriting Host must still authenticate; Funnel has no user identity.
        self.assertIsNone(request_origin({"Host": "127.0.0.1:8765", "X-Forwarded-For": "100.1.2.3"}, "127.0.0.1", 8765, ACCESS))
        self.assertIsNone(request_origin({**headers, "Host": "127.0.0.1:8765", "Tailscale-User-Login": "friend@example.com"}, "127.0.0.1", 8765, ACCESS))
        self.assertEqual(request_origin({"Host": "127.0.0.1:8765"}, "127.0.0.1", 8765, ACCESS), "http://127.0.0.1:8765")


class PrivateStudioHTTPTests(TestCase):
    setUp = p1.StudioHTTPTests.setUp

    def remote(self, method, path, body=None, headers=None):
        connection = HTTPConnection(self.host, timeout=8)
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        values = {"Host": "studio.example.ts.net", "Origin": ACCESS["origin"],
                  "Tailscale-User-Login": ACCESS["operator_login"], "Content-Type": "application/json"}
        values.update(headers or {})
        connection.request(method, path, data, values)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_read_and_save_require_operator_identity_and_exact_origin(self):
        with patch.object(server, "PRIVATE_ACCESS", ACCESS):
            for method, headers in (("GET", {"Tailscale-User-Login": ""}),
                                    ("GET", {"Tailscale-User-Login": "friend@example.com"}),
                                    ("GET", {"Host": "attacker.invalid"}),
                                    ("GET", {"Sec-Fetch-Site": "cross-site"}),
                                    ("POST", {"Origin": "https://other.example.ts.net"}),
                                    ("POST", {"Origin": ""})):
                self.assertEqual(self.remote(method, "/api/projects" if method=="POST" else "/api/state", {}, headers)[0], 403)
            status, _, raw = self.remote("POST", "/api/projects", {"title": "Phone draft"})
            self.assertEqual(status, 201)
            project = json.loads(raw)
            status, _, raw = self.remote("PUT", "/api/projects/" + project["id"], {**project, "title": "Saved on phone"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)["revision"], 2)
        # Configuring an endpoint is opt-in; a default Studio rejects it.
        self.assertEqual(self.remote("GET", "/api/state")[0], 403)

    def test_phone_preview_uses_https_links_secure_cookie_and_protected_proxy_for_layouts(self):
        root = server.PROJECTS.parent / "local-preview"
        root.mkdir()
        self.stack.enter_context(patch.object(server, "LOCAL_PREVIEW_PROOFS", root))
        self.stack.enter_context(patch.object(server, "PRIVATE_ACCESS", ACCESS))
        self.stack.enter_context(patch.object(proof_server, "PROOFS", root))
        self.stack.enter_context(patch.object(proof_server, "LOCAL_PREVIEW", True))
        self.stack.enter_context(patch.object(proof_server, "REQUIRE_ACTIVE", True))
        sidecar = ThreadingHTTPServer(("127.0.0.1", 0), proof_server.Handler)
        self.addCleanup(sidecar.server_close)
        self.addCleanup(sidecar.shutdown)
        threading.Thread(target=sidecar.serve_forever, daemon=True).start()
        self.stack.enter_context(patch.object(server, "LOCAL_REVIEW_PORT", sidecar.server_address[1]))
        key = self.ids[0]
        for folder in (server.PREVIEWS, server.THUMBS):
            for photo_id in self.ids:
                Image.new("RGB", (80, 60), "teal").save(folder / (photo_id + ".jpg"))
        project = server.create_project("Phone preview", [key], spreads=[{"id": str(uuid4()), "layout": "hero",
                                       "slots": [{"photo_id": key, "focus_x": .5, "focus_y": .5}]} for _ in range(13)])
        project["design_format"] = "saal_portrait_22x30"
        server.write_project(project)
        status, _, raw = self.remote("POST", "/api/projects/" + project["id"] + "/client-preview", {})
        self.assertEqual(status, 201)
        url = json.loads(raw)["url"]
        self.assertTrue(url.startswith(ACCESS["origin"] + "/review/"))
        status, headers, _ = self.remote("GET", url.removeprefix(ACCESS["origin"]))
        self.assertEqual(status, 303)
        self.assertIn("; Secure", headers["Set-Cookie"])
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        path = headers["Location"]
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, raw = self.remote("GET", path + "/api", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        proof = json.loads(raw)
        self.assertEqual(proof["preview_studio_url"], ACCESS["origin"] + "/studio")
        self.assertFalse(proof["checkout_available"])
        self.assertEqual(self.remote("PUT", path + "/layout", {"revision": 0, "spreads": proof["spreads"],
                         "cover_photo_id": proof["cover_photo_id"]}, {"Cookie": cookie})[0], 200)
        self.assertEqual(self.remote("GET", path + "/api", headers={"Cookie": cookie, "Tailscale-User-Login": ""})[0], 403)
        self.assertEqual(self.remote("PUT", path + "/layout", {}, {"Cookie": cookie, "Origin": "https://attacker.invalid"})[0], 403)
        self.assertEqual(self.remote("PUT", path + "/layout", b"x" * 65537, {"Cookie": cookie})[0], 413)
        self.assertEqual(self.remote("GET", "/review-app.js")[0], 200)
