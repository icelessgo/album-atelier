"""Tests for the local album proof and Graphistudio print export."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import json
import sys
import zipfile
from uuid import uuid4

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import renderer
import server
import proofing


class AlbumTests(TestCase):
    def test_custom_layout_persists_and_renders_without_changing_existing_templates(self) -> None:
        with TemporaryDirectory() as folder:
            base = Path(folder)
            layouts_dir, projects_dir, originals = (base / "layouts", base / "projects", base / "originals")
            for path in (layouts_dir, projects_dir, originals):
                path.mkdir()
            builtins = {name: rects for name, rects in server.LAYOUTS.items()
                        if name not in server.CUSTOM_LAYOUTS}
            rects = [[0.15, 0.09, 0.20, 0.82], [0.65, 0.09, 0.20, 0.82]]
            with (patch.object(server, "CUSTOM_LAYOUTS_DIR", layouts_dir),
                  patch.object(server, "PROJECTS", projects_dir),
                  patch.object(server, "CUSTOM_LAYOUTS", {}),
                  patch.dict(server.LAYOUTS, builtins, clear=True)):
                record = server.create_custom_layout({"name": "My portraits", "rects": rects,
                                                      "auto_build": False})
                self.assertTrue(server.custom_layout_path(record["id"]).is_file())
                self.assertEqual(server.LAYOUTS[record["id"]], rects)
                self.assertEqual(server.best_layout(
                    ["a", "b"], {"a": {"width": 800, "height": 1200},
                                 "b": {"width": 800, "height": 1200}})[0], "portrait_pair")

                server.LAYOUTS.pop(record["id"])
                server.CUSTOM_LAYOUTS.clear()
                server.load_custom_layouts()
                self.assertEqual(server.LAYOUTS[record["id"]], rects)
                server.CUSTOM_LAYOUTS[record["id"]]["auto_build"] = True
                self.assertEqual(server.best_layout(
                    ["a", "b"], {"a": {"width": 800, "height": 1200},
                                 "b": {"width": 800, "height": 1200}})[0], record["id"])

                Image.new("RGB", (800, 1200), (210, 30, 40)).save(originals / "a.png", icc_profile=renderer.SRGB_PROFILE)
                Image.new("RGB", (800, 1200), (40, 30, 210)).save(originals / "b.png", icc_profile=renderer.SRGB_PROFILE)
                photos = {"a": {"name": "a.png", "stored_name": "a.png"},
                          "b": {"name": "b.png", "stored_name": "b.png"}}
                spread = {"layout": record["id"], "slots": [{"photo_id": "a"}, {"photo_id": "b"}]}
                self.assertEqual(renderer.validate_project({"spreads": [spread]}, photos), [])
                preview, _ = renderer.render_spread(spread, renderer.PrintSpec("test", 6.93, 2.54, 100, 0, 0),
                                                    photos, originals)
                self.assertEqual(preview.getpixel((68, 50))[0], 210)
                self.assertEqual(preview.getpixel((205, 50))[2], 210)
                preview.close()

                project = server.create_project("Uses custom layout")
                project["spreads"] = [spread]
                server.write_json(server.id_path(projects_dir, project["id"]), project)
                with self.assertRaisesRegex(ValueError, "used by an album"):
                    server.delete_custom_layout(record["id"])
                project["spreads"] = []
                server.write_json(server.id_path(projects_dir, project["id"]), project)
                server.delete_custom_layout(record["id"])
                self.assertNotIn(record["id"], server.LAYOUTS)

    def test_custom_layout_rejects_bad_geometry(self) -> None:
        with self.assertRaisesRegex(ValueError, "overlap"):
            server.validate_layout_rects([[0.05, 0.1, 0.5, 0.8], [0.3, 0.2, 0.5, 0.6]])
        with self.assertRaisesRegex(ValueError, "beyond the spread"):
            server.validate_layout_rects([[0.8, 0.1, 0.3, 0.7]])
        with self.assertRaisesRegex(ValueError, "one to four"):
            server.validate_layout_rects([])

    def test_builder_matches_image_orientation_to_frames(self) -> None:
        def photos_for(shapes: list[str]) -> tuple[list[str], dict[str, dict]]:
            photos = {}
            for index, shape in enumerate(shapes):
                width, height = (800, 1200) if shape == "portrait" else (1200, 800)
                photos[str(index)] = {"id": str(index), "name": f"{index}.jpg",
                                      "width": width, "height": height}
            return list(photos), photos

        builtins = {name: rects for name, rects in server.LAYOUTS.items()
                    if name not in server.CUSTOM_LAYOUTS}
        for shapes, expected in [
            (["portrait"], "portrait_solo"),
            (["portrait", "portrait"], "portrait_pair"),
            (["portrait", "portrait", "portrait"], "portrait_trio"),
            (["portrait", "landscape", "landscape"], "portrait_story"),
            (["portrait"] * 4, "portrait_four"),
            (["portrait", "portrait", "landscape", "landscape"], "mosaic_four"),
        ]:
            ids, photos = photos_for(shapes)
            with patch.object(server, "CUSTOM_LAYOUTS", {}), patch.dict(server.LAYOUTS, builtins, clear=True):
                name, ordered = server.best_layout(ids, photos)
            self.assertEqual(name, expected)
            self.assertCountEqual(ordered, ids)

        ids, photos = photos_for(["landscape", "portrait"])
        with patch.object(server, "CUSTOM_LAYOUTS", {}), patch.dict(server.LAYOUTS, builtins, clear=True):
            name, ordered = server.best_layout(ids, photos)
        self.assertEqual(name, "portrait_focus")
        self.assertEqual(ordered, ["1", "0"])

    def test_portrait_book_changes_frame_choice_for_portrait_photos(self) -> None:
        photos = {str(index): {"width": 800, "height": 1200} for index in range(2)}
        landscape, _ = server.best_layout(list(photos), photos,
                                          design_format="landscape_40x30")
        portrait, _ = server.best_layout(list(photos), photos,
                                         design_format="portrait_30x40")
        self.assertEqual(landscape, "portrait_pair")
        self.assertEqual(portrait, "pair")
        self.assertEqual(server.design_ratio("landscape_40x30"), 82 / 30)
        self.assertEqual(server.design_ratio("portrait_30x40"), 60 / 41)
        self.assertAlmostEqual(server.design_ratio("gobook_landscape_35x25"), 69.7 / 25.5)
        self.assertAlmostEqual(server.design_ratio("gobook_portrait_25x35"), 50.8 / 34.7)
        gobook_layout, _ = server.best_layout(list(photos), photos,
                                              design_format="gobook_portrait_25x35")
        self.assertEqual(gobook_layout, "pair")
        options = server.ranked_layouts(list(photos), photos,
                                        design_format="gobook_portrait_25x35")
        self.assertEqual(options[0]["layout"], gobook_layout)
        self.assertEqual(len({item["layout"] for item in options}), len(options))
        self.assertTrue(all(len(item["photo_ids"]) == 2 for item in options))

    def test_companion_choice_persists_and_rejects_unknown_values(self) -> None:
        with TemporaryDirectory() as folder:
            project_dir = Path(folder) / "projects"
            project_dir.mkdir()
            with patch.object(server, "PROJECTS", project_dir), patch.object(server, "load_photos", return_value={}):
                project = server.create_project("Companion preview")
                project["design_format"] = "gobook_portrait_25x35"
                project["companion_option"] = "two_small"
                saved = server.save_project(project["id"], project)
                self.assertEqual(server.load_project(project["id"])["companion_option"], "two_small")
                saved["companion_option"] = "one_mid"
                updated = server.save_project(project["id"], saved)
                self.assertEqual(updated["companion_option"], "one_mid")
                updated["companion_option"] = "other"
                with self.assertRaisesRegex(ValueError, "valid companion"):
                    server.save_project(project["id"], updated)

    def test_unknown_format_cannot_use_legacy_print_or_client_proof(self) -> None:
        project = {"design_format": "unknown_format", "spreads": [{"layout": "hero",
                   "slots": [{"photo_id": "photo"}]}]}
        with TemporaryDirectory() as folder:
            base = Path(folder)
            with self.assertRaisesRegex(ValueError, "supported Print & Bind format"):
                renderer.export_album(project, {}, base, base / "album.zip")
            with self.assertRaisesRegex(ValueError, "supported print format"):
                proofing.create_proof(base, project, {}, base, base)

    def test_gobook_portrait_print_profile_and_sample_export(self) -> None:
        spec = renderer.print_specs({"design_format": "gobook_portrait_25x35"})["gobook"]
        self.assertEqual(spec.pixels, (6000, 4098))
        self.assertEqual((spec.bleed_mm, spec.safe_mm), (5, 18))
        with TemporaryDirectory() as folder:
            base = Path(folder)
            Image.new("RGB", (180, 120), "teal").save(base / "photo.jpg", icc_profile=renderer.SRGB_PROFILE)
            photo = {"name": "photo.jpg", "stored_name": "photo.jpg", "width": 180, "height": 120}
            project = {"title": "Portrait GoBook", "design_format": "gobook_portrait_25x35",
                       "spreads": [{"layout": "hero", "slots": [{"photo_id": "photo"}]}],
                       "companion_option": "two_small"}
            miniature = renderer.PrintSpec("Test portrait", 5.08, 3.47, 100, 5, 18)
            with patch.dict(renderer.PRINT_FORMAT_SPECS,
                            {"gobook_portrait_25x35": {"gobook": miniature}}):
                manifest = renderer.export_album(project, {"photo": photo}, base, base / "sample.zip")
            with zipfile.ZipFile(base / "sample.zip") as archive:
                self.assertIn("gobook/002-003.jpg", archive.namelist())
                self.assertEqual(len([name for name in archive.namelist() if name.endswith(".jpg")]), 1)
                self.assertEqual(manifest["companion_option"], "two_small")
                self.assertEqual(manifest["companion_request"]["quantity"], 2)
                self.assertEqual(manifest["companion_request"]["closed_size"], "15×20 cm")
                self.assertEqual(manifest["design_format"], "gobook_portrait_25x35")
                with Image.open(BytesIO(archive.read("gobook/002-003.jpg"))) as output:
                    self.assertEqual(output.size, miniature.pixels)

    def test_edge_print_specs_export_and_embedded_profile(self) -> None:
        portrait = renderer.print_specs({"design_format": "portrait_30x40"})["edge"]
        landscape = renderer.print_specs({"design_format": "landscape_40x30"})["edge"]
        self.assertEqual((portrait.pixels, portrait.bleed_mm, portrait.safe_mm), ((5906, 4035), 6, 20))
        self.assertEqual((landscape.pixels, landscape.bleed_mm, landscape.safe_mm), ((8071, 2953), 6, 20))
        with TemporaryDirectory() as folder:
            base = Path(folder)
            Image.new("RGB", (180, 120), "teal").save(base / "photo.jpg", icc_profile=renderer.SRGB_PROFILE)
            photo = {"name": "photo.jpg", "stored_name": "photo.jpg", "width": 180, "height": 120}
            project = {"title": "EDGE sample", "design_format": "portrait_30x40",
                       "spreads": [{"layout": "hero", "slots": [{"photo_id": "photo"}]}],
                       "companion_option": "two_small"}
            miniature = renderer.PrintSpec("Test EDGE", 6, 4.1, 100, 6, 20)
            with patch.dict(renderer.PRINT_FORMAT_SPECS, {"portrait_30x40": {"edge": miniature}}):
                manifest = renderer.export_album(project, {"photo": photo}, base, base / "edge.zip")
            self.assertIn("general Print & Bind", manifest["specification_source"])
            self.assertEqual((manifest["companion_request"]["quantity"],
                              manifest["companion_request"]["closed_size"]), (2, "15×20 cm"))
            with zipfile.ZipFile(base / "edge.zip") as archive:
                self.assertIn("edge/002-003.jpg", archive.namelist())
                with Image.open(BytesIO(archive.read("edge/002-003.jpg"))) as output:
                    self.assertEqual(output.size, miniature.pixels)
                    self.assertEqual(output.info["icc_profile"], renderer.SRGB_PROFILE)

    def test_edge_landscape_companions_keep_landscape_sizes(self) -> None:
        one = renderer.companion_request({"design_format": "landscape_40x30", "companion_option": "one_mid"})
        two = renderer.companion_request({"design_format": "landscape_40x30", "companion_option": "two_small"})
        self.assertEqual((one["quantity"], one["closed_size"]), (1, "30×20 cm"))
        self.assertEqual((two["quantity"], two["closed_size"]), (2, "20×15 cm"))
        self.assertEqual(proofing.REVIEW_FORMATS["landscape_40x30"]["companions"]["two_small"],
                         "Two 20×15 cm companions")

    def test_products_share_layout_ratio(self) -> None:
        gobook = renderer.SPECS["gobook"]
        wedding = renderer.SPECS["wedding_book"]
        self.assertAlmostEqual(gobook.width_cm / gobook.height_cm,
                               wedding.width_cm / wedding.height_cm)
        self.assertEqual(gobook.pixels, (8232, 3012))
        self.assertEqual(wedding.pixels, (9281, 3396))

    def test_evenly_samples_large_selection(self) -> None:
        photos = {
            str(index): {"id": str(index), "name": f"{index:03}.jpg",
                         "captured_at": f"2026:01:01 12:{index // 60:02}:{index % 60:02}"}
            for index in range(500)
        }
        spreads = server.build_draft(list(photos), photos)
        used = [slot["photo_id"] for spread in spreads for slot in spread["slots"]]
        self.assertEqual(len(used), 96)
        self.assertEqual(used[0], "0")
        self.assertEqual(used[-1], "499")
        self.assertLessEqual(len(spreads), 40)

    def test_approval_requires_minimum_and_resets_on_change(self) -> None:
        with TemporaryDirectory() as folder:
            project_dir = Path(folder) / "projects"
            project_dir.mkdir()
            photo = {"id": "photo-1", "name": "one.jpg"}
            photos = {photo["id"]: photo}
            spread = {"id": "spread-1", "layout": "hero",
                      "slots": [{"photo_id": photo["id"], "focus_x": 0.5, "focus_y": 0.5}]}
            with patch.object(server, "PROJECTS", project_dir), patch.object(server, "load_photos", return_value=photos):
                project = server.create_project("Test")
                project.update(spreads=[spread], product="gobook", status="approved",
                               design_format="legacy")
                with self.assertRaisesRegex(ValueError, "20 printed sides"):
                    server.save_project(project["id"], project)

                project.update(spreads=[spread for _ in range(10)], status="draft")
                saved = server.save_project(project["id"], project)
                saved["product"] = "gobook"
                saved = server.save_project(project["id"], saved)
                saved["status"] = "approved"
                approved = server.save_project(project["id"], saved)
                self.assertEqual(approved["status"], "approved")
                self.assertIn("approved_at", approved)

                approved["product"] = "wedding_book"
                changed = server.save_project(project["id"], approved)
                self.assertEqual(changed["status"], "draft")
                self.assertNotIn("approved_at", changed)

    def test_full_resolution_export_has_correct_dimensions_and_profile(self) -> None:
        with TemporaryDirectory() as folder:
            base = Path(folder)
            originals = base / "originals"
            originals.mkdir()
            source = Image.new("RGB", (1800, 1200), (123, 45, 67))
            source.save(originals / "photo.jpg", format="JPEG", quality=94, icc_profile=renderer.SRGB_PROFILE)
            source.close()
            photo_id = "photo-1"
            photos = {photo_id: {"name": "photo.jpg", "stored_name": "photo.jpg"}}
            project = {"title": "Test album", "spreads": [{"id": "spread-1", "layout": "hero",
                       "slots": [{"photo_id": photo_id, "focus_x": 0.5, "focus_y": 0.5}]}]}
            output = base / "album.zip"
            manifest = renderer.export_album(project, photos, originals, output)
            self.assertTrue(output.is_file())
            self.assertEqual(manifest["printed_sides"], 2)
            self.assertTrue(any("20 printed sides" in warning for warning in manifest["warnings"]))
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()), {
                    "gobook/002-003.jpg", "wedding_book/002-003.jpg", "print-check.json",
                    "GRAPHISTUDIO-HANDOFF.txt"})
                for product, spec in renderer.SPECS.items():
                    with Image.open(BytesIO(archive.read(f"{product}/002-003.jpg"))) as image:
                        self.assertEqual(image.size, spec.pixels)
                        self.assertEqual(image.info["dpi"], (spec.dpi, spec.dpi))
                        self.assertIn("icc_profile", image.info)
                        self.assertEqual(image.getpixel((0, 0))[0], 123)
                self.assertEqual(json.loads(archive.read("print-check.json"))["spreads"], 1)
                self.assertIn(b"PRINT & BIND", archive.read("GRAPHISTUDIO-HANDOFF.txt"))
            project.update(status="approved", product="wedding_book", approved_proof_id=str(uuid4()))
            approved_output = base / "approved.zip"
            renderer.export_album(project, photos, originals, approved_output)
            with zipfile.ZipFile(approved_output) as archive:
                image_files = [name for name in archive.namelist() if name.endswith(".jpg")]
                self.assertEqual(image_files, ["wedding_book/002-003.jpg"])

    def test_preflight_reports_missing_original_and_low_print_resolution(self) -> None:
        with TemporaryDirectory() as folder:
            base = Path(folder)
            photo = {"name": "small.jpg", "stored_name": "small.jpg", "width": 100, "height": 70,
                     "source_icc": False}
            Image.new("RGB", (100, 70), "red").save(base / "small.jpg")
            project = {"spreads": [{"layout": "hero", "slots": [{"photo_id": "one"}]} for _ in range(10)]}
            check = renderer.preflight_project(project, {"one": photo}, base)
            self.assertTrue(any("no embedded colour profile" in error for error in check["errors"]))
            self.assertIn("gobook", {issue["product"] for issue in check["issues"]})
            self.assertIn("wedding_book", {issue["product"] for issue in check["issues"]})
            with self.assertRaisesRegex(ValueError, "no embedded colour profile"):
                renderer.export_album(project, {"one": photo}, base, base / "untagged.zip")
            Image.new("RGB", (100, 70), "red").save(base / "small.jpg", icc_profile=b"invalid ICC")
            self.assertTrue(any("unreadable" in error for error in
                                renderer.preflight_project(project, {"one": photo}, base)["errors"]))
            with self.assertRaisesRegex(ValueError, "unreadable"):
                renderer.export_album(project, {"one": photo}, base, base / "invalid-profile.zip")
            Image.new("RGB", (100, 70), "red").save(base / "small.jpg", icc_profile=renderer.SRGB_PROFILE)
            self.assertFalse(renderer.preflight_project(project, {"one": photo}, base)["errors"])
            (base / "small.jpg").unlink()
            missing = renderer.preflight_project(project, {"one": photo}, base)
            self.assertTrue(any("original file missing" in error for error in missing["errors"]))

    def test_directed_builder_preserves_chapters_and_must_use_favorites(self) -> None:
        photos = {}
        for chapter in ("Getting ready", "Ceremony", "Portraits", "Reception"):
            for index in range(20):
                photo_id = f"{chapter}-{index}"
                photos[photo_id] = {"id": photo_id, "name": f"{index:02d}.jpg",
                                    "chapter": chapter, "favorite": index in (4, 17),
                                    "captured_at": f"2026:01:01 12:{index:02d}:00",
                                    "width": 1200, "height": 800}
        photos["Reception-10"]["excluded"] = True
        spreads = server.build_directed_draft(list(photos), photos, 12, 3)
        self.assertEqual(len(spreads), 12)
        used = [slot["photo_id"] for spread in spreads for slot in spread["slots"]]
        self.assertNotIn("Reception-10", used)
        self.assertTrue(all(photo["id"] in used for photo in photos.values() if photo.get("favorite")))
        self.assertTrue(all(1 <= len(spread["slots"]) <= 3 for spread in spreads))
        self.assertEqual([spread["chapter"] for spread in spreads[:3]], ["Getting ready"] * 3)

    def test_directed_builder_skips_similar_bursts_but_keeps_favorites_and_later_moments(self) -> None:
        photos = {}
        for name, time, hash_value, favorite in [
            ("first", "12:00:00", "0000000000000000", False),
            ("duplicate", "12:00:05", "0000000000000001", False),
            ("favorite", "12:00:10", "ffffffffffffffff", True),
            ("favorite-duplicate", "12:00:15", "fffffffffffffffe", False),
            ("later", "12:10:00", "0000000000000000", False),
        ]:
            photos[name] = {"id": name, "name": f"{name}.jpg", "chapter": "Ceremony",
                            "captured_at": f"2026:01:01 {time}", "visual_hash": hash_value,
                            "favorite": favorite, "width": 1200, "height": 800}
        report = {}
        spreads = server.build_directed_draft(list(photos), photos, 2, 2, report=report)
        used = {slot["photo_id"] for spread in spreads for slot in spread["slots"]}
        self.assertEqual(used, {"first", "favorite", "later"})
        self.assertEqual(report["similar_skipped"], 2)
        unfiltered = server.build_directed_draft(list(photos), photos, 2, 2, avoid_similar=False)
        self.assertEqual(sum(len(spread["slots"]) for spread in unfiltered), 4)

    def test_old_imports_get_visual_hash_from_local_thumbnail(self) -> None:
        with TemporaryDirectory() as folder:
            base = Path(folder)
            thumbs, metadata = base / "thumbs", base / "photos"
            thumbs.mkdir()
            metadata.mkdir()
            photo_id = str(uuid4())
            Image.new("RGB", (120, 80), "navy").save(thumbs / f"{photo_id}.jpg")
            photo = {"id": photo_id, "name": "old.jpg"}
            with patch.object(server, "THUMBS", thumbs), patch.object(server, "PHOTO_META", metadata):
                server.ensure_visual_hashes({photo_id: photo}, [photo_id])
            self.assertEqual(len(photo["visual_hash"]), 16)
            self.assertEqual(json.loads((metadata / f"{photo_id}.json").read_text())["visual_hash"], photo["visual_hash"])

    def test_proof_snapshot_password_scoping_and_approval(self) -> None:
        with TemporaryDirectory() as folder:
            base = Path(folder)
            for name in ("proofs", "previews", "thumbs"):
                (base / name).mkdir()
            photo_id = str(uuid4())
            Image.new("RGB", (100, 80), "teal").save(base / "previews" / f"{photo_id}.jpg")
            Image.new("RGB", (40, 32), "teal").save(base / "thumbs" / f"{photo_id}.jpg")
            photo = {"id": photo_id, "name": "portrait.jpg", "width": 100, "height": 80}
            spreads = [{"id": str(uuid4()), "layout": "hero", "slots": [{"photo_id": photo_id}]} for _ in range(10)]
            project = {"id": str(uuid4()), "title": "Private", "revision": 3,
                       "photo_ids": [photo_id], "spreads": spreads}
            proof, password = proofing.create_proof(base / "proofs", project, {photo_id: photo},
                                                    base / "previews", base / "thumbs", layouts=renderer.LAYOUTS)
            self.assertTrue(proofing.verify_password(proof, password))
            self.assertFalse(proofing.verify_password(proof, "wrong password"))
            cookie = proofing.session_cookie(proof)
            self.assertTrue(proofing.verify_session(proof, cookie))
            self.assertFalse(proofing.verify_session(proof, cookie[:-1] + ("a" if cookie[-1] != "a" else "b")))
            self.assertNotIn("password_digest", proofing.public_view(proof))
            self.assertNotIn("session_secret", proofing.public_view(proof))
            with self.assertRaisesRegex(ValueError, "photo in this album"):
                proofing.add_feedback(base / "proofs", proof["id"], "swap", spreads[0]["id"], "Use this", str(uuid4()))
            proofing.add_feedback(base / "proofs", proof["id"], "comment", spreads[0]["id"], "Please adjust")
            with self.assertRaisesRegex(ValueError, "wait for the revised proof"):
                proofing.approve_proof(base / "proofs", proof["id"], "gobook")
            proofing.revoke_proof(base / "proofs", proof["id"])
            with self.assertRaisesRegex(ValueError, "no longer open"):
                proofing.add_feedback(base / "proofs", proof["id"], "comment", spreads[0]["id"], "Late")
            final, _ = proofing.create_proof(base / "proofs", project, {photo_id: photo},
                                             base / "previews", base / "thumbs", layouts=renderer.LAYOUTS)
            self.assertEqual(proofing.approve_proof(base / "proofs", final["id"], "gobook")["product"], "gobook")
            with self.assertRaisesRegex(ValueError, "already approved"):
                proofing.approve_proof(base / "proofs", final["id"], "wedding_book")
            with self.assertRaisesRegex(ValueError, "contact your photographer"):
                proofing.add_feedback(base / "proofs", final["id"], "comment", spreads[0]["id"], "Late")

    def test_cover_photo_is_frozen_into_the_password_protected_proof(self) -> None:
        with TemporaryDirectory() as folder:
            base = Path(folder)
            for name in ("proofs", "previews", "thumbs"):
                (base / name).mkdir()
            inside, cover = str(uuid4()), str(uuid4())
            photos = {photo_id: {"id": photo_id, "name": f"{photo_id}.jpg", "width": 1200, "height": 800}
                      for photo_id in (inside, cover)}
            for photo_id in photos:
                Image.new("RGB", (120, 80), "teal").save(base / "previews" / f"{photo_id}.jpg")
                Image.new("RGB", (30, 20), "teal").save(base / "thumbs" / f"{photo_id}.jpg")
            project = {"id": str(uuid4()), "title": "Cover proof", "revision": 1,
                       "design_format": "portrait_30x40", "photo_ids": [inside, cover],
                       "cover_photo_id": cover, "spreads": [
                           {"id": str(uuid4()), "layout": "hero", "slots": [{"photo_id": inside}]}
                           for _ in range(10)]}
            proof, _ = proofing.create_proof(base / "proofs", project, photos,
                                             base / "previews", base / "thumbs")
            self.assertEqual(proofing.public_view(proof)["cover_photo_id"], cover)
            self.assertTrue((base / "proofs" / proof["id"] / "media" / f"{cover}.jpg").is_file())
            project["cover_photo_id"] = str(uuid4())
            with self.assertRaisesRegex(ValueError, "Cover photo must be in this album"):
                proofing.create_proof(base / "proofs", project, photos, base / "previews", base / "thumbs")

    def test_portrait_gobook_proof_scopes_companion_approval(self) -> None:
        with TemporaryDirectory() as folder:
            base = Path(folder)
            for name in ("proofs", "previews", "thumbs"):
                (base / name).mkdir()
            photo_id = str(uuid4())
            Image.new("RGB", (100, 80), "teal").save(base / "previews" / f"{photo_id}.jpg")
            Image.new("RGB", (40, 32), "teal").save(base / "thumbs" / f"{photo_id}.jpg")
            photo = {"id": photo_id, "name": "photo.jpg", "width": 100, "height": 80}
            project = {"id": str(uuid4()), "title": "GoBook proof", "revision": 1,
                       "design_format": "gobook_portrait_25x35", "companion_option": None,
                       "photo_ids": [photo_id], "spreads": [
                           {"id": str(uuid4()), "layout": "hero", "slots": [{"photo_id": photo_id}]}
                           for _ in range(10)]}
            proof, _ = proofing.create_proof(base / "proofs", project, {photo_id: photo},
                                             base / "previews", base / "thumbs", layouts=renderer.LAYOUTS)
            public = proofing.public_view(proof)
            self.assertEqual(public["offer"]["closed_size"], "25×35 cm")
            self.assertEqual(public["offer"]["companions"]["two_small"], "Two 15×20 cm companions")
            with self.assertRaisesRegex(ValueError, "matching book"):
                proofing.approve_proof(base / "proofs", proof["id"], "wedding_book")
            with self.assertRaisesRegex(ValueError, "matching book"):
                proofing.approve_proof(base / "proofs", proof["id"], "gobook", "unknown")
            approved = proofing.approve_proof(base / "proofs", proof["id"], "gobook", "two_small")
            self.assertEqual((approved["companion_option"], approved["total_eur"]), ("two_small", 500))
            with self.assertRaisesRegex(ValueError, "already approved"):
                proofing.approve_proof(base / "proofs", proof["id"], "gobook", "one_mid")

    def test_edge_proof_compares_both_real_book_sizes_and_scopes_approval(self) -> None:
        with TemporaryDirectory() as folder:
            base = Path(folder)
            for name in ("proofs", "previews", "thumbs"):
                (base / name).mkdir()
            photo_id = str(uuid4())
            Image.new("RGB", (100, 80), "teal").save(base / "previews" / f"{photo_id}.jpg")
            Image.new("RGB", (40, 32), "teal").save(base / "thumbs" / f"{photo_id}.jpg")
            photo = {"id": photo_id, "name": "photo.jpg", "width": 100, "height": 80}
            project = {"id": str(uuid4()), "title": "EDGE proof", "revision": 1,
                       "design_format": "portrait_30x40", "companion_option": None,
                       "photo_ids": [photo_id], "spreads": [
                           {"id": str(uuid4()), "layout": "hero", "slots": [{"photo_id": photo_id}]}
                           for _ in range(10)]}
            proof, _ = proofing.create_proof(base / "proofs", project, {photo_id: photo},
                                             base / "previews", base / "thumbs", layouts=renderer.LAYOUTS)
            public = proofing.public_view(proof)
            self.assertEqual((public["offer"]["product"], public["offer"]["closed_size"]),
                             ("edge", "30×40 cm"))
            self.assertAlmostEqual(public["offer"]["spread_ratio"], 60 / 41)
            self.assertEqual(public["offer"]["companions"]["one_mid"], "One 20×30 cm companion")
            self.assertEqual([(item["product"], item["closed_size"]) for item in public["offers"]],
                             [("gobook", "25×35 cm"), ("edge", "30×40 cm")])
            self.assertEqual([(item["open_width_cm"], item["open_height_cm"]) for item in public["offers"]],
                             [(50.8, 34.7), (60, 41)])
            with self.assertRaisesRegex(ValueError, "matching book"):
                proofing.approve_proof(base / "proofs", proof["id"], "gobook")
            with self.assertRaisesRegex(ValueError, "matching book"):
                proofing.approve_proof(base / "proofs", proof["id"], "edge", "unknown")
            proof_path = base / "proofs" / proof["id"] / "proof.json"
            frozen = json.loads(proof_path.read_text())
            frozen["offers"][1]["companions"] = {}
            proof_path.write_text(json.dumps(frozen))
            with self.assertRaisesRegex(ValueError, "matching book"):
                proofing.approve_proof(base / "proofs", proof["id"], "edge", "two_small")
            frozen["offers"][1]["companions"] = public["offer"]["companions"]
            proof_path.write_text(json.dumps(frozen))
            approved = proofing.approve_proof(base / "proofs", proof["id"], "edge", "two_small")
            self.assertEqual((approved["product"], approved["total_eur"]), ("edge", 700))
            alternative, _ = proofing.create_proof(base / "proofs", project, {photo_id: photo},
                                                   base / "previews", base / "thumbs")
            cheaper = proofing.approve_proof(base / "proofs", alternative["id"], "gobook", None,
                                            "gobook_portrait_25x35")
            self.assertEqual((cheaper["product"], cheaper["design_format"], cheaper["total_eur"]),
                             ("gobook", "gobook_portrait_25x35", 300))

    def test_invalid_or_missing_photo_is_rejected(self) -> None:
        project = {"spreads": [{"layout": "hero", "slots": [{"photo_id": "missing"}]}]}
        self.assertIn("missing photo", " ".join(renderer.validate_project(project, {})))
        with TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                renderer.export_album(project, {}, Path(folder), Path(folder) / "output.zip")

    def test_repositioned_crop_changes_print_output(self) -> None:
        with TemporaryDirectory() as folder:
            source = Image.new("RGB", (400, 100))
            for x in range(400):
                for y in range(100):
                    source.putpixel((x, y), (round(x * 255 / 399), 20, 20))
            source.save(Path(folder) / "wide.png", icc_profile=renderer.SRGB_PROFILE)
            source.close()
            photos = {"one": {"name": "wide.png", "stored_name": "wide.png"}}
            spec = renderer.PrintSpec("test", 6.93, 2.54, 100, 0, 0)
            spread = {"layout": "hero", "slots": [{"photo_id": "one", "focus_x": 0, "focus_y": 0.5}]}
            left, _ = renderer.render_spread(spread, spec, photos, Path(folder))
            spread["slots"][0]["focus_x"] = 1
            right, _ = renderer.render_spread(spread, spec, photos, Path(folder))
            self.assertGreater(right.getpixel((136, 50))[0] - left.getpixel((136, 50))[0], 60)
            left.close()
            right.close()


if __name__ == "__main__":
    import unittest
    unittest.main()
