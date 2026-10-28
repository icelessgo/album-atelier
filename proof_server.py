#!/usr/bin/env python3
"""Client review service with no access to the Studio API or original photographs."""

from __future__ import annotations

from collections import defaultdict, deque
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID

from proofing import (add_feedback, approve_proof, is_open, load_proof, public_view,
                      session_cookie, verify_password, verify_session,
                      start_checkout, record_stripe_event, update_client_layout,
                      consume_preview_start)
from payments import configured as stripe_configured, verify_webhook
from gallery_lifecycle import gallery_access, GalleryCheckUnavailable
from gallery_showcase import find_showcase, card_view


ROOT = Path(__file__).resolve().parent
PROOFS = Path(os.environ.get("ALBUM_PROOF_DATA", ROOT / "data" / "proofs"))
REQUIRE_ACTIVE = os.environ.get("ALBUM_REQUIRE_ACTIVE_POINTER") == "1"
LOCAL_PREVIEW = False
STATIC = ROOT / "static"
ATTEMPTS: dict[str, deque] = defaultdict(deque)
ATTEMPTS_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "AlbumProof/1"

    def log_message(self, format: str, *args: object) -> None:
        # Proof URLs contain unguessable identifiers; omit paths and cookies from logs.
        print(f"{self.client_address[0]} {self.command} {self.response_code if hasattr(self, 'response_code') else '-'}", flush=True)

    def _send(self, status: int, body: bytes, mime: str, cookie: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; form-action 'self'; frame-ancestors 'none'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: dict, cookie: str | None = None) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8", cookie)

    def _body(self) -> dict:
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            raise ValueError("Expected JSON")
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 65536:
            raise ValueError("Request too large")
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("Expected an object")
        return value

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return False
        parsed = urlsplit(origin)
        return parsed.scheme in ("https", "http") and parsed.netloc == self.headers.get("Host") and not parsed.path

    def _authorized(self, proof: dict) -> bool:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            item = cookie.get("album_review")
            return bool(item and verify_session(proof, item.value))
        except Exception:
            return False

    def _route(self, method: str) -> None:
        path = urlsplit(self.path).path
        # Deliberately outside /review/: nginx does not expose this internal
        # surface. PicPeak's authenticated gallery route calls it.
        card_match = re.fullmatch(r'/internal/gallery/([1-9][0-9]{0,9})/album(?:/(cover))?', path)
        if method == 'GET' and card_match:
            try:
                card, proof = find_showcase(PROOFS, int(card_match.group(1)))
            except GalleryCheckUnavailable as exc:
                return self._json(503, {'error': str(exc)})
            if card is None:
                raise FileNotFoundError('No published gallery album')
            if card_match.group(2):
                cover_id = str(UUID(card['cover_photo_id']))
                filename = PROOFS / proof['id'] / 'media' / f'{cover_id}.jpg'
                if not filename.is_file() or filename.stat().st_size > 4 * 1024 * 1024:
                    raise FileNotFoundError('Cover preview unavailable')
                return self._send(200, filename.read_bytes(), 'image/jpeg')
            return self._json(200, card_view(card, proof))
        if method == "POST" and path == "/review/stripe/webhook":
            length = int(self.headers.get("Content-Length", "0"))
            if not stripe_configured() or not 0 < length <= 65536:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid webhook"})
            event = verify_webhook(self.rfile.read(length), self.headers.get("Stripe-Signature", ""))
            recorded = record_stripe_event(PROOFS, event)
            return self._json(200, {"received": True, "recorded": recorded})
        if method == "GET" and path in ("/review-app.js", "/review-app.css"):
            filename = STATIC / path.lstrip("/")
            return self._send(200, filename.read_bytes(),
                              "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8")
        if method == "GET" and path.startswith("/review/brand/"):
            asset = path.removeprefix("/review/brand/")
            mime = {"album-atelier.svg": "image/svg+xml", "dm-serif-display.woff2": "font/woff2",
                    "outfit.woff2": "font/woff2", "caveat.woff2": "font/woff2"}.get(asset)
            if mime is None:
                raise FileNotFoundError("Brand asset not found")
            return self._send(200, (STATIC / "brand" / asset).read_bytes(), mime)
        match = re.fullmatch(r"/review/([0-9a-f-]{36})(?:/(.*))?", path)
        if not match:
            raise FileNotFoundError("Page not found")
        proof_id = str(UUID(match.group(1)))
        rest = match.group(2) or ""
        proof = load_proof(PROOFS, proof_id)
        if REQUIRE_ACTIVE:
            pointer = PROOFS / "active" / f"{proof['project_id']}.txt"
            if not pointer.is_file() or pointer.read_text(encoding="ascii").strip() != proof_id:
                return self._json(HTTPStatus.GONE, {"error": "This review has been replaced"})
        if not is_open(proof):
            return self._json(HTTPStatus.GONE, {"error": "This review has expired or been replaced"})
        try:
            if not gallery_access(proof):
                return self._json(HTTPStatus.GONE, {"error": "The source gallery is no longer available. Please contact your photographer."})
        except GalleryCheckUnavailable as exc:
            return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
        if method == "GET" and not rest:
            return self._send(200, (STATIC / "review.html").read_bytes(), "text/html; charset=utf-8")
        if LOCAL_PREVIEW and proof.get("preview_only") and method == "GET" and rest.startswith("preview-start/"):
            token = rest.removeprefix("preview-start/")
            proof = consume_preview_start(PROOFS, proof_id, token)
            if not proof:
                return self._json(HTTPStatus.FORBIDDEN, {"error": "This local preview link has been used"})
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", f"/review/{proof_id}")
            self.send_header("Set-Cookie", f"album_review={session_cookie(proof)}; HttpOnly; SameSite=Strict; Path=/review/{proof_id}; Max-Age=43200")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method in ("POST", "PUT") and not self._origin_ok():
            return self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid request origin"})
        if method == "POST" and rest == "unlock":
            key = f"{proof_id}:{self.client_address[0]}"
            with ATTEMPTS_LOCK:
                attempts = ATTEMPTS[key]
                while attempts and attempts[0] < time.time() - 300:
                    attempts.popleft()
                if len(attempts) >= 5:
                    return self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Try again in a few minutes"})
                attempts.append(time.time())
            if not verify_password(proof, str(self._body().get("password", ""))):
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": "Incorrect review password"})
            with ATTEMPTS_LOCK:
                ATTEMPTS.pop(key, None)
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "http") == "https" else ""
            cookie = (f"album_review={session_cookie(proof)}; HttpOnly; SameSite=Strict; "
                      f"Path=/review/{proof_id}; Max-Age=43200{secure}")
            return self._json(200, {"ok": True}, cookie)
        if not self._authorized(proof):
            return self._json(HTTPStatus.UNAUTHORIZED, {"error": "Enter the review password"})
        if method == "GET" and rest == "api":
            return self._json(200, public_view(proof))
        if method == "GET" and rest == "mockup.js":
            return self._send(200, (STATIC / "mockup.js").read_bytes(), "text/javascript; charset=utf-8")
        if method == "PUT" and rest == "layout":
            body = self._body()
            return self._json(200, update_client_layout(PROOFS, proof_id,
                                                        body.get("revision"), body.get("spreads"),
                                                        body.get("cover_photo_id"),
                                                        selected_format=body.get('design_format'),
                                                        from_format=body.get('from_format')))
        if method == "GET" and re.fullmatch(r"media/[0-9a-f-]{36}(?:-thumb)?\.jpg", rest):
            filename = PROOFS / proof_id / rest
            if not filename.is_file():
                raise FileNotFoundError("Preview not found")
            return self._send(200, filename.read_bytes(), "image/jpeg")
        if method == "POST" and rest == "feedback":
            body = self._body()
            entry = add_feedback(PROOFS, proof_id, body.get("kind"), body.get("spread_id"),
                                 body.get("message", ""), body.get("photo_id"))
            return self._json(201, entry)
        if method == "POST" and rest == "approve":
            body = self._body()
            approved = approve_proof(PROOFS, proof_id, body.get("product"), body.get("companion_option"),
                                     body.get("design_format"), body.get("companion_quantities"))
            return self._json(200, approved)
        if method == "POST" and rest == "checkout":
            if proof.get("preview_only"):
                return self._json(HTTPStatus.FORBIDDEN, {"error": "Checkout is unavailable in a local preview"})
            if not stripe_configured():
                return self._json(HTTPStatus.SERVICE_UNAVAILABLE,
                                  {"error": "Stripe test checkout is not configured yet"})
            self._body()
            return self._json(200, start_checkout(PROOFS, proof_id))
        raise FileNotFoundError("Page not found")

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def _handle(self, method: str) -> None:
        try:
            self._route(method)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            self._json(404 if isinstance(exc, FileNotFoundError) else 400, {"error": str(exc)})
        except Exception:
            self._json(500, {"error": "Unexpected review error"})


if __name__ == "__main__":
    port = int(os.environ.get("ALBUM_PROOF_PORT", "8776"))
    host = os.environ.get("ALBUM_PROOF_BIND", "127.0.0.1")
    print(f"Protected review service listening on {host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
