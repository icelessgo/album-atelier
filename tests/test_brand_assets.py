"""Only the intended public brand assets may bypass review authentication."""

from pathlib import Path
import sys
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proof_server


class _Request:
    def __init__(self, path: str):
        self.path = path
        self.sent = None

    def _send(self, status: int, body: bytes, mime: str):
        self.sent = (status, body, mime)


class BrandAssetTests(TestCase):
    def test_logo_and_fonts_are_readable_without_a_proof_session(self):
        for name, mime in (("album-atelier.svg", "image/svg+xml"),
                           ("dm-serif-display.woff2", "font/woff2"),
                           ("outfit.woff2", "font/woff2"),
                           ("caveat.woff2", "font/woff2")):
            with self.subTest(name=name):
                request = _Request(f"/review/brand/{name}")
                proof_server.Handler._route(request, "GET")
                self.assertEqual(request.sent[0], 200)
                self.assertGreater(len(request.sent[1]), 100)
                self.assertEqual(request.sent[2], mime)

    def test_arbitrary_files_and_path_traversal_are_rejected(self):
        for path in ("/review/brand/README.md", "/review/brand/../proofs/key",
                     "/review/brand/album-atelier.svg/extra"):
            with self.subTest(path=path):
                with self.assertRaises(FileNotFoundError):
                    proof_server.Handler._route(_Request(path), "GET")
