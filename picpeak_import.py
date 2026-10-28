"""Read-only PicPeak/R2 bridge for the private desktop Studio.

The existing desktop processor owns the database and R2 credentials. This
module never sends those credentials to a browser or the public proof service.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import time
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import NAMESPACE_URL, uuid5, uuid4

from PIL import Image, ImageOps
from bridge_stream import stream, json_rows, BridgeCancelled
from project_store import write_json, project_lock
from gallery_sources import manifest_path


BRIDGE = Path(__file__).with_name("picpeak_bridge.cjs")
IMPORT_TIMEOUT = min(7100, max(1, float(os.environ.get('ALBUM_PICPEAK_IMPORT_TIMEOUT', '3600'))))
STALL_TIMEOUT = min(300, max(.1, float(os.environ.get('ALBUM_PICPEAK_STALL_TIMEOUT', '60'))))
CONTAINER = os.environ.get("ALBUM_PICPEAK_PROCESSOR", "picpeak-processor")


def _command(mode: str, *ids: int) -> list[str]:
    if mode not in ("galleries", "previews", "original", "sources") or any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in ids
    ):
        raise ValueError("Invalid PicPeak import request")
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", CONTAINER):
        raise ValueError("Invalid processor container name")
    return ["docker", "exec", "-i", CONTAINER, "node", "-", mode, *(str(value) for value in ids)]


def _bridge(mode: str, *ids: int, source_version=None) -> subprocess.Popen:
    tag = '--album-job=' + uuid4().hex
    deadline = 35 if mode == 'galleries' else IMPORT_TIMEOUT + 30 if mode == 'previews' else 180
    command = _command(mode, *ids) + [tag, '--album-deadline=' + str(deadline)]
    if source_version is not None:
        if not re.fullmatch('[a-f0-9]{64}', source_version):
            raise ValueError('Invalid original source version')
        command.append('--source-version=' + source_version)
    with BRIDGE.open('rb') as source:
        process = subprocess.Popen(command, stdin=source, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
    process.album_tag, process.album_container = tag, CONTAINER
    process.album_new_session = True
    return process


def galleries(*, cancel=None, total=30, stall=20) -> list[dict]:
    output = bytearray()
    with stream(_bridge('galleries'), total=total, stall=stall, cancel=cancel) as chunks:
        for chunk in chunks:
            output.extend(chunk)
            if len(output) > 1024 * 1024:
                raise ValueError('PicPeak gallery response is too large')
    value = json.loads(output)
    if not isinstance(value, list):
        raise RuntimeError('Invalid PicPeak gallery response')
    return value


def _capture_time(value: object) -> str:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y:%m:%d %H:%M:%S")
    except (ValueError, TypeError):
        return ""


def photo_id(event_id: int, source_id: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"picpeak-photo:{event_id}:{source_id}"))


def visual_hash(image: Image.Image) -> str:
    sample = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = sample.load()
    bits = 0
    for row in range(8):
        for column in range(8):
            bits = (bits << 1) | (pixels[column, row] > pixels[column + 1, row])
    sample.close()
    return f"{bits:016x}"


def import_previews(event_id, metadata, previews, thumbs, sRGB_profile, progress=None, *, cancel=None, total=IMPORT_TIMEOUT, stall=STALL_TIMEOUT):
    path = manifest_path(metadata, event_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with path.with_suffix('.lock').open('a') as lock:
        while True:
            if cancel is not None and cancel.is_set():
                raise BridgeCancelled('PicPeak import cancelled while waiting')
            if time.monotonic() - started >= total:
                raise TimeoutError('Another import of this gallery is still running')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(.1)
        try:
            return _import_previews(event_id, metadata, previews, thumbs, sRGB_profile, progress,
                                    cancel=cancel, total=total-(time.monotonic()-started), stall=stall)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _save_imported_photo(path, value):
    with project_lock(path):
        current = json.loads(path.read_text()) if path.is_file() else {}
        preserved = {key: current[key] for key in ('chapter','favorite','excluded','added_at','stored_name','source_icc') if key in current}
        write_json(path, {**current, **value, **preserved})


class ImportedPhotos(list):
    skipped = 0
    eligible_ids = None
    visible_ids = None


def _import_previews(event_id: int, metadata: Path, previews: Path, thumbs: Path,
                    sRGB_profile: bytes, progress=None, *, cancel=None, total=IMPORT_TIMEOUT, stall=STALL_TIMEOUT) -> list[str]:
    """Import preview-only images; safe to repeat without overwriting curation."""
    process = _bridge('previews', event_id)
    imported = ImportedPhotos()
    membership = None
    terminal_error = None
    try:
        with stream(process, total=total, stall=stall, cancel=cancel) as chunks:
            for row in json_rows(chunks):
                if 'summary' in row:
                    imported.skipped = int(row['summary']['skipped'])
                    membership = row['summary'].get('photos')
                    if membership is not None and (row['summary'].get('event_id') != event_id or not row['summary'].get('listing_complete')):
                        raise ValueError('Incomplete PicPeak gallery membership response')
                    continue
                source = row["photo"]
                source_id = int(source["photo_id"])
                if int(source["event_id"]) != event_id or source_id <= 0:
                    raise RuntimeError("PicPeak photo does not belong to this gallery")
                local_id = photo_id(event_id, source_id)
                raw = base64.b64decode(row["preview"], validate=True)
                if len(raw) > 10 * 1024 * 1024:
                    raise RuntimeError("PicPeak preview is too large")
                with Image.open(BytesIO(raw)) as source_image:
                    image = ImageOps.exif_transpose(source_image).convert("RGB")
                    image_hash = visual_hash(image)
                    for size, folder in ((1600, previews), (390, thumbs)):
                        target = image.copy()
                        target.thumbnail((size, size), Image.Resampling.LANCZOS)
                        temporary = folder / f"{local_id}.{uuid4().hex}.tmp"
                        try:
                            target.save(temporary, "JPEG", quality=85, icc_profile=sRGB_profile)
                            os.replace(temporary, folder / f"{local_id}.jpg")
                        finally:
                            temporary.unlink(missing_ok=True)
                            target.close()
                    image.close()
                path = metadata / f"{local_id}.json"
                existing = json.loads(path.read_text()) if path.is_file() else {}
                value = {
                    **existing, "id": local_id, "name": Path(str(source.get("name") or "Photo")).name[:160],
                    "width": int(source.get("width") or 0), "height": int(source.get("height") or 0),
                    "captured_at": _capture_time(source.get("captured_at")),
                    "stored_name": existing.get("stored_name") or f"{local_id}.jpg",
                    "source_icc": existing.get("source_icc", False),
                    "visual_hash": image_hash,
                    "preview_original_sha256": None,
                    "added_at": existing.get("added_at") or datetime.now(timezone.utc).isoformat(),
                    "chapter": existing.get("chapter", "Unsorted"),
                    "favorite": existing.get("favorite", False), "excluded": existing.get("excluded", False),
                    "picpeak_event_id": event_id, "picpeak_photo_id": source_id,
                    "picpeak_visibility": source.get("visibility", "visible"),
                    "captured_at_utc": source.get("captured_at"),
                    "picpeak_favorites": int(source.get("favorite_count") or 0),
                    "picpeak_rating": float(source.get("average_rating") or 0),
                    "preview_url": f"/media/preview/{local_id}.jpg",
                    "thumb_url": f"/media/thumb/{local_id}.jpg",
                }
                _save_imported_photo(path, value)
                imported.append(local_id)
                if progress:
                    progress(len(imported))
    except RuntimeError as exc:
        if membership is None:
            raise
        terminal_error = exc
    if cancel is not None and cancel.is_set():
        raise BridgeCancelled('PicPeak import cancelled. You can try again.')
    # Commit only a complete source listing. A completed empty/unreadable
    # listing still records removals, while a partial or cancelled one cannot.
    if membership is None:
        membership = [{'photo_id': int(json.loads((metadata / (key+'.json')).read_text())['picpeak_photo_id']),
                       'visibility': json.loads((metadata / (key+'.json')).read_text()).get('picpeak_visibility', 'visible')}
                      for key in imported]
    rows = {}
    for entry in membership:
        local_id = photo_id(event_id, int(entry['photo_id']))
        rows[local_id] = {'source_id': int(entry['photo_id']), 'visibility': entry.get('visibility', 'hidden'),
                          'readable': local_id in imported}
    imported.eligible_ids = list(rows)
    imported.visible_ids = [key for key, row in rows.items() if row['visibility']=='visible' and row['readable']]
    path = manifest_path(metadata, event_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    for cached in metadata.glob('*.json'):
        with project_lock(cached):
            value = json.loads(cached.read_text())
            if value.get('picpeak_event_id') != event_id:
                continue
            row = rows.get(value['id'])
            value['picpeak_current'] = row is not None
            if row:
                value['picpeak_visibility'] = row['visibility']
            write_json(cached, value)
    write_json(path, {'event_id': event_id, 'updated_at': datetime.now(timezone.utc).isoformat(), 'photos': rows})
    if terminal_error:
        raise terminal_error
    if not imported:
        raise ValueError('No readable photos found in this PicPeak gallery')
    return imported


def source_versions(event_id, ids, *, cancel=None):
    output = bytearray()
    with stream(_bridge('sources', int(event_id), *(int(value) for value in ids)),
                total=180, stall=60, cancel=cancel) as chunks:
        for chunk in chunks:
            output.extend(chunk)
            if len(output) > 1024 * 1024:
                raise ValueError('Source response is too large')
    value = json.loads(output)
    if (value.get('event_id') != int(event_id) or type(value.get('available')) is not bool or
            not isinstance(value.get('photos'), list)):
        raise ValueError('Invalid PicPeak source response')
    if value['available'] and {row.get('photo_id') for row in value['photos']} != set(map(int, ids)):
        raise ValueError('Incomplete PicPeak source response')
    return value


def fetch_original(photo: dict, originals: Path, *, cancel=None, total=150, stall=60,
                   source_version=None) -> Path:
    if cancel is not None and cancel.is_set():
        raise BridgeCancelled('PicPeak original fetch cancelled')
    event_id, source_id = photo.get("picpeak_event_id"), photo.get("picpeak_photo_id")
    if not event_id or not source_id:
        raise ValueError("This is not a PicPeak photo")
    for suffix in ("jpg", "png"):
        existing = originals / f"{photo['id']}.{suffix}"
        if existing.is_file():
            try:
                with Image.open(existing) as image:
                    image.verify()
                cache_intact = True
                if photo.get('original_sha256'):
                    with existing.open('rb') as cached:
                        cache_intact = hashlib.file_digest(cached, 'sha256').hexdigest() == photo['original_sha256']
                if cache_intact and (source_version is None or source_version == photo.get('original_source_version')):
                    return existing
            except (OSError, ValueError):
                os.replace(existing, existing.with_suffix('.corrupt-' + uuid4().hex))
    target = originals / f"{photo['id']}.jpg"
    process = _bridge("original", int(event_id), int(source_id), **({'source_version': source_version} if source_version else {}))
    temporary = target.with_suffix("." + uuid4().hex + ".download")
    size = 0
    try:
        with stream(process, total=total, stall=stall, cancel=cancel) as chunks:
            with temporary.open('wb') as output:
                for chunk in chunks:
                    size += len(chunk)
                    if size > 80 * 1024 * 1024:
                        raise ValueError('PicPeak original exceeds 80 MB')
                    output.write(chunk)
        with Image.open(temporary) as image:
            if image.format not in ("JPEG", "PNG"):
                raise ValueError("Only JPEG and PNG originals are supported")
            image.verify()
        with Image.open(temporary) as image:
            if image.format == "PNG":
                target = originals / f"{photo['id']}.png"
        os.replace(temporary, target)
        for suffix in ('jpg', 'png'):
            other = originals / f"{photo['id']}.{suffix}"
            if other != target:
                other.unlink(missing_ok=True)
        return target
    finally:
        temporary.unlink(missing_ok=True)
