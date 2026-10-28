from unittest import TestCase
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
import test_p1_regressions as p1
from pilot_preview import matches

ACCESS = {"origin": "https://studio.example.ts.net", "operator_login": "owner@example.com"}


class PilotRoutes(TestCase):
    def test_native_pilot_paths_cannot_capture_studio_editing_or_approval(self):
        for path in ("/pilot", "/assets/file.js", "/api/gallery/sample/shop", "/api/admin/print-shop/orders", "/print-orders/example"):
            self.assertTrue(matches(path))
        for path in ("/", "/api/state", "/api/projects", "/api/catalogue", "/api/picpeak/galleries", "/api/jobs", "/review/example", "/review-app.js", "/static/app.js"):
            self.assertFalse(matches(path))


class PilotHTTP(TestCase):
    setUp = p1.StudioHTTPTests.setUp

    def test_proxy_is_opt_in_and_keeps_owner_and_origin_guards(self):
        def remote(method="GET", headers=None):
            from http.client import HTTPConnection
            c=HTTPConnection(self.host,timeout=8)
            values={"Host":"studio.example.ts.net","Tailscale-User-Login":"owner@example.com","Origin":ACCESS["origin"],"Content-Type":"application/json"}
            values.update(headers or {})
            c.request(method,"/pilot",b'{}' if method=="POST" else None,values)
            r=c.getresponse();result=r.status,r.read();c.close();return result
        with patch.object(server,"PRIVATE_ACCESS",ACCESS):
            self.assertEqual(remote()[0],404)
            with patch.dict(server.os.environ,{"ALBUM_PRINT_PILOT_PREVIEW":"true"}):
                self.assertEqual(remote(headers={"Tailscale-User-Login":"other@example.com"})[0],403)
                self.assertEqual(remote("POST",{"Origin":"https://other.invalid"})[0],403)
                with patch("pilot_preview.forward", side_effect=lambda handler, method: handler._json(200, {"pilot": True})) as relay:
                    remote()
                    self.assertEqual(relay.call_count,1)
