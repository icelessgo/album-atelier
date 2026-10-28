"""Saal format boundaries, proof isolation, crop approval and measured print files."""
from io import BytesIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4
import zipfile

from PIL import Image, ImageDraw
from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proofing
import renderer
import saal_export
from saal_profiles import SAAL_PROFILES, cover_crop, saal_offer
import server
from export_records import archive_info
import test_p1_regressions as p1


def spreads(photo_id, count=13):
    return [{"id": str(uuid4()), "layout": "hero", "chapter": "Story",
             "slots": [{"photo_id": photo_id, "focus_x": .5, "focus_y": .5}]} for _ in range(count)]


class SaalProofTests(TestCase):
    setUp = p1.StudioHTTPTests.setUp
    request = p1.StudioHTTPTests.request

    def project(self, design_format="saal_portrait_22x30"):
        project = server.create_project("Saal test", [self.ids[0]], spreads=spreads(self.ids[0]))
        project.update(design_format=design_format, cover_crop={"focus_x": .2, "focus_y": .8})
        server.write_project(project)
        for key in self.ids:
            for folder in (server.THUMBS, server.PREVIEWS, server.ORIGINALS):
                Image.new("RGB", (800, 600), "teal").save(folder / (key + ".jpg"), icc_profile=renderer.SRGB_PROFILE)
        return project

    def test_all_three_offers_are_150_and_have_no_companions_or_checkout(self):
        self.assertEqual(len(SAAL_PROFILES), 3)
        self.assertEqual(self.request("GET", "/api/state")[0], 200)
        for key in SAAL_PROFILES:
            offer = saal_offer(key)
            self.assertEqual((offer["base_eur"], offer["minimum_spreads"], offer["companions"]), (150, 13, {}))
            project = self.project(key)
            proof, _ = proofing.create_proof(server.PROOFS, project, self.photos, server.PREVIEWS, server.THUMBS)
            self.assertEqual(len(proof["offers"]), 1)
            self.assertEqual(proof["cover_crop"], project["cover_crop"])
            self.assertFalse(proofing.public_view(proof)["checkout_available"])
            with self.assertRaises(ValueError):
                proofing.approve_proof(server.PROOFS, proof["id"], "edge")
            with self.assertRaises(ValueError):
                proofing.approve_proof(server.PROOFS, proof["id"], "saal", companion_quantities={"one_mid": 1})
            approved = proofing.approve_proof(server.PROOFS, proof["id"], "saal")
            self.assertEqual(approved["total_eur"], 150)
            self.assertEqual(proofing.load_proof(server.PROOFS, proof["id"])["approved"], approved)
            with patch.object(proofing, "checkout_session") as checkout:
                with self.assertRaises(proofing.PaymentError):
                    proofing.start_checkout(server.PROOFS, proof["id"])
                checkout.assert_not_called()

    def test_minimum_pages_and_cover_changes_require_revised_proof(self):
        project = self.project()
        with self.assertRaisesRegex(ValueError, "13 to 40"):
            proofing.create_proof(server.PROOFS, {**project, "spreads": spreads(self.ids[0], 41)}, self.photos, server.PREVIEWS, server.THUMBS)
        too_short = {**project, "spreads": project["spreads"][:12]}
        with self.assertRaisesRegex(ValueError, "13 to 40"):
            proofing.create_proof(server.PROOFS, too_short, self.photos, server.PREVIEWS, server.THUMBS)
        proof, _ = proofing.create_proof(server.PROOFS, project, self.photos, server.PREVIEWS, server.THUMBS)
        with self.assertRaisesRegex(ValueError, "13 to 40"):
            proofing.update_client_layout(server.PROOFS, proof["id"], 0, project["spreads"][:12])
        # The gallery includes the other visible photo; selecting it resets the
        # cover crop and makes approval wait for photographer review.
        changed = proofing.update_client_layout(server.PROOFS, proof["id"], 0, project["spreads"], self.ids[1])
        self.assertEqual(changed["cover_crop"], cover_crop())
        with self.assertRaisesRegex(ValueError, "photographer review"):
            proofing.approve_proof(server.PROOFS, proof["id"], "saal")
        project["active_proof_id"] = proof["id"]
        server.write_project(project)
        with patch.object(server, "pull_proof"), patch.object(server, "set_active"):
            applied = server.apply_client_layout(project["id"])
        self.assertEqual(applied["cover_crop"], cover_crop())
        self.assertEqual(applied["cover_photo_id"], self.ids[1])

    def test_cover_crop_save_revokes_approval_and_invalid_crop_is_rejected(self):
        project = self.project()
        proof, _ = proofing.create_proof(server.PROOFS, project, self.photos, server.PREVIEWS, server.THUMBS)
        project.update(active_proof_id=proof["id"], approved_proof_id=proof["id"], status="approved", product="saal")
        server.write_project(project)
        saved = server.save_project(project["id"], {**project, "cover_crop": {"focus_x": .7, "focus_y": .8}, "status": "draft"})
        self.assertEqual(saved["status"], "draft")
        self.assertNotIn("active_proof_id", saved)
        self.assertFalse(proofing.is_open(proofing.load_proof(server.PROOFS, proof["id"])))
        with self.assertRaises(ValueError):
            server.save_project(project["id"], {**saved, "cover_crop": {"focus_x": float("nan"), "focus_y": .5}})
        with self.assertRaisesRegex(ValueError, "Companion"):
            server.save_project(project["id"], {**saved, "companion_quantities": {"one_mid": 1}})


class SaalPrintTests(TestCase):
    def test_measured_geometry_and_pdf_boxes_for_every_format(self):
        expected = {"saal_square_30x30": ([594, 300], [291, 306]),
                    "saal_landscape_30x21": ([594, 210], [291, 216]),
                    "saal_portrait_22x30": ([444, 300], [216, 306])}
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            jpeg = folder / "tiny.jpg"
            Image.new("RGB", (100, 100), "red").save(jpeg, icc_profile=renderer.SRGB_PROFILE)
            for key, profile in SAAL_PROFILES.items():
                for index, (kind, dims, dpi) in enumerate((("interiors", profile["trim_mm"], 300), ("cover", profile["cover_trim_mm"], 400))):
                    geometry = saal_export.sheet_geometry(dims, dpi, cover=kind=="cover")
                    self.assertEqual(geometry["output_mm"], expected[key][index])
                    output = folder / f"{key}-{kind}.pdf"
                    saal_export.write_pdf([jpeg] * (40 if kind=="interiors" else 1), output, geometry, renderer.SRGB_PROFILE)
                    reader = PdfReader(output)
                    self.assertEqual(len(reader.pages), 40 if kind=="interiors" else 1)
                    page = reader.pages[0]
                    for measured, wanted in zip((page.mediabox.width, page.mediabox.height), geometry["output_mm"]):
                        self.assertAlmostEqual(float(measured) * 25.4 / 72, wanted, places=3)
                    self.assertAlmostEqual(float(page.trimbox.left) * 25.4 / 72, 20 if kind=="cover" else 3, places=3)
                    colour = page["/Resources"]["/ColorSpace"]["/DefaultRGB"]
                    self.assertEqual(colour[0], "/ICCBased")
                    self.assertEqual(colour[1].get_object().get_data(), renderer.SRGB_PROFILE)

    def test_real_13_spread_export_is_srgb_versioned_and_has_jpeg_fallback(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            key = str(uuid4())
            image = Image.new("RGB", (2400, 1600), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 799, 1599), fill="red")
            draw.rectangle((1600, 0, 2399, 1599), fill="blue")
            image.save(folder / "source.jpg", icc_profile=renderer.SRGB_PROFILE)
            image.close()
            photos = {key: {"id": key, "stored_name": "source.jpg", "name": "source.jpg", "width": 2400, "height": 1600}}
            project = {"id": str(uuid4()), "revision": 7, "title": "Synthetic Saal portrait", "photo_ids": [key],
                       "design_format": "saal_portrait_22x30", "spreads": spreads(key), "cover_crop": {"focus_x": 0, "focus_y": .5}}
            output = folder / "album.zip"
            manifest = renderer.export_album(project, photos, folder, output)
            self.assertEqual(archive_info(output)["project_revision"], 7)
            self.assertEqual(manifest["printed_sides"], 26)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(len(PdfReader(BytesIO(archive.read("saal/interiors.pdf"))).pages), 13)
                self.assertEqual(len(PdfReader(BytesIO(archive.read("saal/cover.pdf"))).pages), 1)
                with Image.open(BytesIO(archive.read("saal/interiors/001.jpg"))) as sheet:
                    self.assertEqual(sheet.size, (5244, 3543))
                    self.assertEqual(sheet.info["icc_profile"], renderer.SRGB_PROFILE)
                with Image.open(BytesIO(archive.read("saal/cover.jpg"))) as cover:
                    self.assertEqual(cover.size, (3402, 4819))
                    self.assertGreater(cover.getpixel((400, 2400))[0], 200)
                self.assertIn("First left", archive.read("SAAL-HANDOFF.txt").decode())
            # Force the large-PDF branch without allocating an oversized file.
            with patch.object(saal_export, "PDF_BUDGET", 0):
                fallback = renderer.export_album(project, photos, folder, folder / "fallback.zip")
            self.assertEqual(fallback["pdf_omissions"], ["interiors", "cover"])
            with zipfile.ZipFile(folder / "fallback.zip") as archive:
                self.assertIn("saal/interiors/013.jpg", archive.namelist())
                self.assertNotIn("saal/interiors.pdf", archive.namelist())
