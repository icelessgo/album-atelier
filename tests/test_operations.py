"""Operational backup and retention checks using synthetic album files."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backup_album import backup, verify
from scripts.proof_retention import prune
from scripts.rebuild_album_media import rebuild_local, rebuild_picpeak
from scripts import rebuild_album_media


class OperationsTests(TestCase):
    def test_expired_preview_pruning_preserves_payment_and_approval_record(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            now = datetime.now(timezone.utc)
            old_id, recent_id = str(uuid4()), str(uuid4())
            for proof_id, expiry in ((old_id, now - timedelta(days=31)),
                                     (recent_id, now - timedelta(days=2))):
                folder = root / proof_id
                (folder / "media").mkdir(parents=True)
                (folder / "proof.json").write_text(json.dumps({
                    "id": proof_id, "expires_at": expiry.isoformat(),
                    "approved": {"product": "edge"}, "payment": {"status": "paid"},
                }))
                (folder / "media" / f"{uuid4()}.jpg").write_bytes(b"synthetic preview")
            dry_count, _, problems = prune(root, now=now, days=30, apply=False)
            self.assertEqual((dry_count, problems), (1, []))
            self.assertTrue((root / old_id / "media").exists())
            count, size, problems = prune(root, now=now, days=30, apply=True)
            self.assertEqual((count, size, problems), (1, len(b"synthetic preview"), []))
            self.assertFalse((root / old_id / "media").exists())
            self.assertTrue((root / recent_id / "media").exists())
            record = json.loads((root / old_id / "proof.json").read_text())
            self.assertEqual(record["payment"]["status"], "paid")
            self.assertEqual(record["approved"]["product"], "edge")

    def test_retention_refuses_unexpected_media_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            proof_id = str(uuid4())
            media = root / proof_id / "media"
            media.mkdir(parents=True)
            (media / "unexpected.txt").write_text("keep me")
            (media.parent / "proof.json").write_text(json.dumps({
                "id": proof_id,
                "expires_at": (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(),
            }))
            count, _, problems = prune(root, now=datetime.now(timezone.utc), days=30, apply=True)
            self.assertEqual(count, 0)
            self.assertTrue(problems)
            self.assertTrue((media / "unexpected.txt").exists())
            (media / "unexpected.txt").unlink()
            (media / f"{uuid4()}.jpg").symlink_to(media.parent / "proof.json")
            count, _, problems = prune(root, now=datetime.now(timezone.utc), days=30, apply=True)
            self.assertEqual(count, 0)
            self.assertTrue(problems)
            self.assertTrue(media.exists())

    def test_album_backup_restores_and_detects_corruption(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "data"
            for folder in ("originals", "photos", "projects", "layouts", "proofs", "local-previews"):
                (source / folder).mkdir(parents=True)
            (source / "originals" / "image.jpg").write_bytes(b"synthetic original")
            (source / "projects" / "album.json").write_text('{"title":"Test"}')
            (source / "local-previews" / "disposable.jpg").write_bytes(b"exclude")
            (source / "studio.log").write_text("exclude")
            archive = backup(source, base / "backups")
            stamp = archive.name.removesuffix(".tar.zst")
            count = verify(archive, archive.with_name(stamp + ".manifest.json"),
                           archive.with_name(stamp + ".sha256"))
            self.assertEqual(count, 2)
            with archive.open("ab") as stream:
                stream.write(b"tampered")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                verify(archive, archive.with_name(stamp + ".manifest.json"),
                       archive.with_name(stamp + ".sha256"))

    def test_slim_backup_keeps_local_original_and_proof_but_omits_rebuildable_files(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "data"
            for folder in ("originals", "photos", "projects", "layouts", "proofs", "previews",
                           "thumbs", "exports", "local-previews"):
                (source / folder).mkdir(parents=True)
            local_id, remote_id = str(uuid4()), str(uuid4())
            (source / "photos" / f"{local_id}.json").write_text(json.dumps({
                "id": local_id, "stored_name": f"{local_id}.jpg"}))
            (source / "photos" / f"{remote_id}.json").write_text(json.dumps({
                "id": remote_id, "stored_name": f"{remote_id}.jpg",
                "picpeak_event_id": 5, "picpeak_photo_id": 9}))
            (source / "originals" / f"{local_id}.jpg").write_bytes(b"local-only pixels")
            (source / "originals" / f"{remote_id}.jpg").write_bytes(b"R2 pixels")
            (source / "previews" / f"{local_id}.jpg").write_bytes(b"preview")
            (source / "thumbs" / f"{remote_id}.jpg").write_bytes(b"thumbnail")
            (source / "exports" / "print.zip").write_bytes(b"regenerate")
            proof_id = str(uuid4())
            media = source / "proofs" / proof_id / "media"
            media.mkdir(parents=True)
            (media.parent / "proof.json").write_text("{}")
            (media / f"{remote_id}.jpg").write_bytes(b"proof preview")
            archive = backup(source, base / "backups")
            members = subprocess.check_output(["tar", "-I", "zstd", "-tf", str(archive)]).decode()
            self.assertIn(f"data/originals/{local_id}.jpg", members)
            self.assertIn(f"data/photos/{remote_id}.json", members)
            self.assertIn(f"data/proofs/{proof_id}/proof.json", members)
            self.assertIn(f"data/proofs/{proof_id}/media/{remote_id}.jpg", members)
            for omitted in (f"data/originals/{remote_id}.jpg", "data/previews/",
                            "data/thumbs/", "data/exports/"):
                self.assertNotIn(omitted, members)
            (source / "originals" / f"{remote_id}.jpg").unlink()
            self.assertTrue(backup(source, base / "remote-not-cached").is_file())
            (source / "originals" / f"{local_id}.jpg").unlink()
            with self.assertRaisesRegex(ValueError, "Local-only original missing"):
                backup(source, base / "other-backups")

    def test_rebuilds_local_previews_and_picpeak_previews_from_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            for folder in ("originals", "photos", "projects", "previews", "thumbs"):
                (data / folder).mkdir()
            local_id, remote_id = str(uuid4()), str(uuid4())
            Image.new("RGB", (1800, 1200), "teal").save(data / "originals" / f"{local_id}.jpg")
            (data / "photos" / f"{local_id}.json").write_text(json.dumps({
                "id": local_id, "stored_name": f"{local_id}.jpg"}))
            (data / "photos" / f"{remote_id}.json").write_text(json.dumps({
                "id": remote_id, "stored_name": f"{remote_id}.jpg",
                "picpeak_event_id": 7, "picpeak_photo_id": 11}))
            self.assertEqual(rebuild_local(data), 1)
            with Image.open(data / "previews" / f"{local_id}.jpg") as image:
                self.assertLessEqual(max(image.size), 1600)
            self.assertEqual(rebuild_local(data), 0)

            def fake_import(event_id, metadata, previews, thumbs, profile):
                self.assertEqual(event_id, 7)
                Image.new("RGB", (100, 80)).save(previews / f"{remote_id}.jpg")
                Image.new("RGB", (30, 20)).save(thumbs / f"{remote_id}.jpg")

            with patch.object(rebuild_album_media, "import_previews", side_effect=fake_import) as imported:
                self.assertEqual(rebuild_picpeak(data), (1, 0))
                imported.assert_called_once()
