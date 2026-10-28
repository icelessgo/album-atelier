"""Tests for private PicPeak import and Stripe proof-payment boundaries."""

from __future__ import annotations

import base64
from io import BytesIO
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import subprocess
from urllib.parse import parse_qs
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import payments
import picpeak_import
import proofing
import renderer
import server
from renderer import SRGB_PROFILE


class FakeBridge:
    def __new__(cls, data: bytes):
        return subprocess.Popen([sys.executable, '-c',
            'import sys;sys.stdout.buffer.write(bytes.fromhex(sys.argv[1]))', data.hex()],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class IntegrationTests(TestCase):
    def test_local_client_preview_is_private_one_use_and_cannot_checkout(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            for name in ("proofs", "previews", "thumbs"):
                (base / name).mkdir()
            photo_id = str(uuid4())
            photos = {photo_id: {"id": photo_id, "name": "synthetic.jpg", "width": 1200, "height": 800}}
            for name in ("previews", "thumbs"):
                Image.new("RGB", (100, 80), "teal").save(base / name / f"{photo_id}.jpg")
            project = {"id": str(uuid4()), "title": "Local preview", "revision": 1,
                       "design_format": "gobook_portrait_25x35", "photo_ids": [photo_id],
                       "spreads": [{"id": str(uuid4()), "layout": "hero",
                                    "slots": [{"photo_id": photo_id}]} for _ in range(10)]}
            password, token = "private-random-password", "different-one-use-token"
            proof, _ = proofing.create_proof(base / "proofs", project, photos,
                                             base / "previews", base / "thumbs",
                                             password, link_media=True)
            self.assertEqual(os.stat(base / "previews" / f"{photo_id}.jpg").st_ino,
                             os.stat(base / "proofs" / proof["id"] / "media" / f"{photo_id}.jpg").st_ino)
            proof.update(preview_only=True, preview_token=token)
            proofing._save(base / "proofs" / proof["id"] / "proof.json", proof)
            self.assertIsNone(proofing.consume_preview_start(base / "proofs", proof["id"], "wrong-token"))
            self.assertIsNotNone(proofing.consume_preview_start(base / "proofs", proof["id"], token))
            self.assertIsNone(proofing.consume_preview_start(base / "proofs", proof["id"], token))
            current = proofing.load_proof(base / "proofs", proof["id"])
            self.assertNotIn("preview_token", current)
            self.assertFalse(proofing.verify_password(current, token))
            with patch.object(proofing, "stripe_configured", return_value=True):
                public = proofing.public_view(current)
            self.assertFalse(public["checkout_available"])
            self.assertNotIn("preview_token", public)
            with self.assertRaisesRegex(payments.PaymentError, "unavailable"):
                proofing.start_checkout(base / "proofs", proof["id"])

    def test_multiple_companion_sizes_and_stripe_quantities(self):
        offer = proofing.offer_for("portrait_30x40")
        counts = {"one_mid": 3, "two_small": 4}
        self.assertEqual(proofing.companion_total(counts, offer), 1000)
        self.assertEqual([(item["quantity"], item["closed_size"]) for item in
                          renderer.companion_requests({"design_format": "portrait_30x40",
                                                       "companion_quantities": counts})],
                         [(3, "20×30 cm"), (4, "15×20 cm")])
        with self.assertRaisesRegex(ValueError, "pairs"):
            proofing.companion_counts({"one_mid": 1, "two_small": 3})
        proof = {"id": str(uuid4()), "project_revision": 4, "feedback": [],
                 "offers": [offer], "approved": {"product": "edge", "design_format": "portrait_30x40",
                                                "companion_quantities": counts, "total_eur": 1500}}
        captured = {}
        def fake_stripe(request, timeout):
            captured.update({key: value[0] for key, value in parse_qs(request.data.decode()).items()})
            return BytesIO(json.dumps({"id": "cs_test_multi", "url": "https://checkout.stripe.com/c/test",
                                       "expires_at": 9999999999}).encode())
        with (patch.dict(os.environ, {"ALBUM_STRIPE_SECRET_KEY": "rk_test_example",
                                       "ALBUM_STRIPE_WEBHOOK_SECRET": "whsec_example",
                                       "ALBUM_PUBLIC_REVIEW_BASE": "https://gallery.example/review"}),
              patch.object(payments, "urlopen", side_effect=fake_stripe)):
            self.assertEqual(payments.checkout_session(proof)["amount_eur"], 1500)
        self.assertEqual(captured["line_items[1][quantity]"], "3")
        self.assertEqual(captured["line_items[2][quantity]"], "2")
        self.assertEqual(captured["shipping_address_collection[allowed_countries][0]"], "IE")

    def test_approval_records_both_companion_sizes_and_rejects_odd_small_count(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            for name in ("proofs", "previews", "thumbs"):
                (base / name).mkdir()
            photo_id = str(uuid4())
            photo = {photo_id: {"id": photo_id, "name": "synthetic.jpg", "width": 1200, "height": 800}}
            Image.new("RGB", (120, 80), "teal").save(base / "previews" / f"{photo_id}.jpg")
            Image.new("RGB", (30, 20), "teal").save(base / "thumbs" / f"{photo_id}.jpg")
            project = {"id": str(uuid4()), "title": "Test", "revision": 1,
                       "design_format": "portrait_30x40", "photo_ids": [photo_id],
                       "spreads": [{"id": str(uuid4()), "layout": "hero", "slots": [{"photo_id": photo_id}]}
                                   for _ in range(10)]}
            proof, _ = proofing.create_proof(base / "proofs", project, photo,
                                             base / "previews", base / "thumbs")
            with self.assertRaisesRegex(ValueError, "pairs"):
                proofing.approve_proof(base / "proofs", proof["id"], "edge", None, "portrait_30x40",
                                       {"one_mid": 2, "two_small": 3})
            approved = proofing.approve_proof(base / "proofs", proof["id"], "edge", None,
                                              "portrait_30x40", {"one_mid": 2, "two_small": 4})
            self.assertEqual(approved["companion_quantities"], {"one_mid": 2, "two_small": 4})
            self.assertEqual(approved["total_eur"], 1300)

    def test_picpeak_proof_includes_only_one_gallery_and_blocks_unreviewed_edit(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            for name in ("proofs", "previews", "thumbs"):
                (base / name).mkdir()
            ids = [str(uuid4()) for _ in range(4)]
            photos = {}
            for index, photo_id in enumerate(ids):
                photos[photo_id] = {"id": photo_id, "name": f"photo-{index}.jpg", "width": 1200,
                                    "height": 800, "picpeak_event_id": 7 if index < 3 else 8}
                Image.new("RGB", (120, 80), "teal").save(base / "previews" / f"{photo_id}.jpg")
                Image.new("RGB", (30, 20), "teal").save(base / "thumbs" / f"{photo_id}.jpg")
            project = {"id": str(uuid4()), "title": "Wedding", "revision": 1,
                       "design_format": "portrait_30x40", "photo_ids": [ids[0]],
                       "spreads": [{"id": str(uuid4()), "layout": "hero", "slots": [{"photo_id": ids[0]}]}
                                   for _ in range(10)]}
            proof, _ = proofing.create_proof(base / "proofs", project, photos,
                                             base / "previews", base / "thumbs")
            public = proofing.public_view(proof)
            self.assertEqual(public["gallery_photo_count"], 3)
            self.assertEqual({photo["id"] for photo in public["photos"]}, set(ids[:3]))
            self.assertFalse((base / "proofs" / proof["id"] / "media" / f"{ids[3]}.jpg").exists())
            with self.assertRaisesRegex(ValueError, "cover photo from this gallery"):
                proofing.update_client_layout(base / "proofs", proof["id"], 0, proof["spreads"], ids[3])
            with self.assertRaisesRegex(ValueError, "cover photo from this gallery"):
                proofing.update_client_layout(base / "proofs", proof["id"], 0, proof["spreads"], "../object")
            changed = json.loads(json.dumps(proof["spreads"]))
            changed[0]["slots"][0]["photo_id"] = ids[1]
            with self.assertRaisesRegex(ValueError, "gallery"):
                wrong = json.loads(json.dumps(changed))
                wrong[0]["slots"][0]["photo_id"] = ids[3]
                proofing.update_client_layout(base / "proofs", proof["id"], 0, wrong)
            saved = proofing.update_client_layout(base / "proofs", proof["id"], 0, changed)
            self.assertTrue(saved["client_edits_pending"])
            self.assertEqual(saved["layout_revision"], 1)
            with self.assertRaisesRegex(ValueError, "another tab"):
                proofing.update_client_layout(base / "proofs", proof["id"], 0, changed)
            with self.assertRaisesRegex(ValueError, "photographer review"):
                proofing.approve_proof(base / "proofs", proof["id"], "edge")

    def test_photographer_applies_client_layout_before_new_proof(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            for name in ("projects", "proofs", "previews", "thumbs"):
                (base / name).mkdir()
            ids = [str(uuid4()) for _ in range(3)]
            photos = {photo_id: {"id": photo_id, "name": f"{index}.jpg", "width": 1200,
                                 "height": 800, "picpeak_event_id": 7}
                      for index, photo_id in enumerate(ids)}
            for photo_id in ids:
                Image.new("RGB", (120, 80), "teal").save(base / "previews" / f"{photo_id}.jpg")
                Image.new("RGB", (30, 20), "teal").save(base / "thumbs" / f"{photo_id}.jpg")
            with (patch.object(server, "PROJECTS", base / "projects"),
                  patch.object(server, "PROOFS", base / "proofs"),
                  patch.object(server, "load_photos", return_value=photos),
                  patch.object(server, "pull_proof", return_value=True),
                  patch.object(server, "set_active") as set_active):
                project = server.create_project("Wedding", [ids[0]])
                project["design_format"] = "portrait_30x40"
                project["spreads"] = [{"id": str(uuid4()), "layout": "hero",
                                       "slots": [{"photo_id": ids[0], "focus_x": .5, "focus_y": .5}]}
                                      for _ in range(10)]
                proof, _ = proofing.create_proof(base / "proofs", project, photos,
                                                 base / "previews", base / "thumbs")
                project["active_proof_id"] = proof["id"]
                server.write_json(server.id_path(base / "projects", project["id"]), project)
                changed = json.loads(json.dumps(proof["spreads"]))
                changed[0]["slots"][0]["photo_id"] = ids[1]
                proofing.update_client_layout(base / "proofs", proof["id"], 0, changed, ids[2])
                adopted = server.apply_client_layout(project["id"])
                self.assertEqual(adopted["spreads"][0]["slots"][0]["photo_id"], ids[1])
                self.assertIn(ids[1], adopted["photo_ids"])
                self.assertEqual(adopted["cover_photo_id"], ids[2])
                self.assertIn(ids[2], adopted["photo_ids"])
                self.assertNotIn("active_proof_id", adopted)
                self.assertEqual(adopted["client_layout_source"]["revision"], 1)
                self.assertEqual(proofing.load_proof(base / "proofs", proof["id"])["status"], "replaced")
                set_active.assert_called_once_with(base / "proofs", project["id"], None)

    def test_client_can_change_only_the_cover_and_requires_photographer_review(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            for name in ("proofs", "previews", "thumbs"):
                (base / name).mkdir()
            ids = [str(uuid4()), str(uuid4())]
            photos = {photo_id: {"id": photo_id, "name": f"{index}.jpg", "width": 1200,
                                 "height": 800, "picpeak_event_id": 7}
                      for index, photo_id in enumerate(ids)}
            for photo_id in ids:
                Image.new("RGB", (120, 80), "teal").save(base / "previews" / f"{photo_id}.jpg")
                Image.new("RGB", (30, 20), "teal").save(base / "thumbs" / f"{photo_id}.jpg")
            project = {"id": str(uuid4()), "title": "Wedding", "revision": 1,
                       "design_format": "portrait_30x40", "photo_ids": [ids[0]],
                       "spreads": [{"id": str(uuid4()), "layout": "hero", "slots": [{"photo_id": ids[0]}]}
                                   for _ in range(10)]}
            proof, _ = proofing.create_proof(base / "proofs", project, photos,
                                             base / "previews", base / "thumbs")
            saved = proofing.update_client_layout(base / "proofs", proof["id"], 0,
                                                   proof["spreads"], ids[1])
            self.assertEqual(saved["cover_photo_id"], ids[1])
            self.assertEqual(saved["layout_revision"], 1)
            self.assertTrue(saved["client_edits_pending"])
            with self.assertRaisesRegex(ValueError, "photographer review"):
                proofing.approve_proof(base / "proofs", proof["id"], "edge")

    def test_restricted_stripe_test_key_is_accepted(self):
        with patch.dict(os.environ, {"ALBUM_STRIPE_SECRET_KEY": "rk_test_example",
                                    "ALBUM_STRIPE_WEBHOOK_SECRET": "whsec_example",
                                    "ALBUM_PUBLIC_REVIEW_BASE": "https://gallery.example/review"}):
            self.assertTrue(payments.configured())

    def test_picpeak_reimport_keeps_manual_curation_and_only_creates_previews(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("meta", "previews", "thumbs", "originals"):
                (root / name).mkdir()
            image = BytesIO()
            Image.new("RGB", (1200, 800), "teal").save(image, "JPEG")
            row = {"photo": {"event_id": 12, "photo_id": 51, "name": "wedding.jpg",
                             "width": 6000, "height": 4000, "captured_at": "2025-07-01T12:30:00Z"},
                   "preview": base64.b64encode(image.getvalue()).decode()}
            data = (json.dumps(row) + "\n").encode()
            with patch.object(picpeak_import, "_bridge", side_effect=lambda *args: FakeBridge(data)):
                ids = picpeak_import.import_previews(12, root / "meta", root / "previews",
                                                      root / "thumbs", SRGB_PROFILE)
                path = root / "meta" / f"{ids[0]}.json"
                value = json.loads(path.read_text())
                value["chapter"] = "Ceremony"
                value["favorite"] = True
                path.write_text(json.dumps(value))
                again = picpeak_import.import_previews(12, root / "meta", root / "previews",
                                                       root / "thumbs", SRGB_PROFILE)
            self.assertEqual(ids, again)
            self.assertEqual(json.loads(path.read_text())["chapter"], "Ceremony")
            self.assertTrue(json.loads(path.read_text())["favorite"])
            self.assertFalse(list((root / "originals").iterdir()))
            self.assertTrue((root / "previews" / f"{ids[0]}.jpg").is_file())

    def test_stripe_signature_rejects_modified_and_expired_events(self):
        body = b'{"id":"evt_test","livemode":false}'
        now = 1_800_000_000
        signature = hmac.new(b"whsec_test", str(now).encode() + b"." + body,
                             hashlib.sha256).hexdigest()
        header = f"t={now},v1={signature}"
        self.assertEqual(payments.verify_webhook(body, header, "whsec_test", now)["id"], "evt_test")
        with self.assertRaisesRegex(ValueError, "signature"):
            payments.verify_webhook(body + b" ", header, "whsec_test", now)
        with self.assertRaisesRegex(ValueError, "Expired"):
            payments.verify_webhook(body, header, "whsec_test", now + 301)

    def test_checkout_record_requires_exact_approved_price_and_session(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            photo_id = str(uuid4())
            previews, thumbs = root / "previews", root / "thumbs"
            previews.mkdir(); thumbs.mkdir()
            Image.new("RGB", (100, 80), "teal").save(previews / f"{photo_id}.jpg")
            Image.new("RGB", (40, 32), "teal").save(thumbs / f"{photo_id}.jpg")
            project = {"id": str(uuid4()), "title": "Test", "revision": 1,
                       "design_format": "portrait_30x40", "photo_ids": [photo_id],
                       "spreads": [{"id": str(uuid4()), "layout": "hero", "slots": [{"photo_id": photo_id}]}
                                   for _ in range(10)]}
            proof, _ = proofing.create_proof(root, project,
                                             {photo_id: {"id": photo_id, "name": "test.jpg", "width": 100, "height": 80}},
                                             previews, thumbs)
            proofing.approve_proof(root, proof["id"], "gobook", "one_mid", "gobook_portrait_25x35")
            with patch.object(proofing, "checkout_session", return_value={
                "id": "cs_test_123", "url": "https://checkout.stripe.com/c/test", "amount_eur": 500,
                "expires_at": 9_999_999_999}):
                self.assertEqual(proofing.start_checkout(root, proof["id"])["id"], "cs_test_123")
                self.assertEqual(proofing.start_checkout(root, proof["id"])["id"], "cs_test_123")
            event = {"id": "evt_123", "type": "checkout.session.completed", "livemode": False,
                     "data": {"object": {"id": "cs_test_123", "client_reference_id": proof["id"],
                                         "currency": "eur", "amount_total": 50000, "payment_status": "paid",
                                         "collected_information": {"shipping_details": {"address": {"country": "IE"},
                                                                                         "name": "Test Client"}},
                                         "metadata": {"proof_id": proof["id"],
                                                      "design_format": "gobook_portrait_25x35",
                                                      "project_revision": "1"}}}}
            wrong = json.loads(json.dumps(event))
            wrong["data"]["object"]["amount_total"] = 30000
            with self.assertRaisesRegex(ValueError, "does not match"):
                proofing.record_stripe_event(root, wrong)
            wrong_country = json.loads(json.dumps(event))
            wrong_country["data"]["object"]["collected_information"]["shipping_details"]["address"]["country"] = "GB"
            with self.assertRaisesRegex(ValueError, "Irish delivery"):
                proofing.record_stripe_event(root, wrong_country)
            self.assertTrue(proofing.record_stripe_event(root, event))
            self.assertFalse(proofing.record_stripe_event(root, event))
            public = proofing.public_view(proofing.load_proof(root, proof["id"]))
            self.assertEqual(public["payment"]["status"], "paid")
            self.assertNotIn("session_id", public["payment"])
