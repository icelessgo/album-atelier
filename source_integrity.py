"""On-demand source verification and immutable render inputs, on the desktop."""
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

from PIL import Image, ImageOps
from picpeak_import import fetch_original, source_versions, visual_hash
from project_store import project_lock, write_json


def used_ids(project):
    ids = {slot['photo_id'] for spread in project.get('spreads', []) for slot in spread['slots']}
    if project.get('cover_photo_id'):
        ids.add(project['cover_photo_id'])
    return sorted(ids)


def file_sha(path):
    with path.open('rb') as stream:
        return sha256_stream(stream)


def sha256_stream(stream):
    value = sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        value.update(chunk)
    return value.hexdigest()


def original_path(photo, originals):
    path = originals / photo.get('stored_name', '')
    if path.resolve().parent != originals.resolve() or path.suffix.lower() not in ('.jpg', '.png') or not path.is_file():
        raise ValueError(f"Fetch the print original for {photo['name']} before continuing")
    return path


def refresh_previews(photo, path, previews, thumbs, srgb):
    # Retouched originals supersede PicPeak's potentially older derived preview.
    from renderer import _srgb
    with Image.open(path) as raw:
        image = _srgb(ImageOps.exif_transpose(raw))
        try:
            photo.update(width=image.width, height=image.height, source_icc=bool(raw.info.get('icc_profile')),
                         visual_hash=visual_hash(image))
            for size, folder in ((1600, previews), (390, thumbs)):
                target = image.copy()
                temporary = folder / (photo['id'] + '.' + uuid4().hex + '.tmp')
                try:
                    target.thumbnail((size, size), Image.Resampling.LANCZOS)
                    target.save(temporary, 'JPEG', quality=85, icc_profile=srgb)
                    os.replace(temporary, folder / (photo['id'] + '.jpg'))
                finally:
                    target.close()
                    temporary.unlink(missing_ok=True)
        finally:
            image.close()


def verify_sources(project, photos, originals, metadata, previews, thumbs, srgb, *, cancel=None, progress=None):
    """Check only used photos. Any unknown/unavailable source fails closed.

    Returns hashes and gallery lifecycle context, without exposing R2 keys.
    The caller compares these against its frozen proof/approval before printing.
    """
    ids = used_ids(project)
    if not ids:
        raise ValueError('Build an album before checking its photos')
    groups = {}
    for key in ids:
        photo = photos[key]
        if photo.get('picpeak_photo_id'):
            groups.setdefault(photo['picpeak_event_id'], []).append(photo['picpeak_photo_id'])
    versions, galleries = {}, []
    for event, source_ids in groups.items():
        result = source_versions(event, source_ids, cancel=cancel)
        if not result['available']:
            raise ValueError('The source gallery is expired, hidden, archived or unavailable. Review sharing before continuing.')
        galleries.append({key: result.get(key) for key in ('event_id', 'slug', 'expires_at')})
        for row in result['photos']:
            if not row.get('available') or not row.get('version'):
                raise ValueError('An album photo is removed, hidden or unavailable in PicPeak. Refresh the gallery and review the album.')
            versions[(event, row['photo_id'])] = row['version']
    manifest, changed = {}, []
    for index, key in enumerate(ids, 1):
        path = metadata / f'{key}.json'
        with project_lock(path):
            photo = json.loads(path.read_text(encoding='utf-8'))
            before = photo.get('original_sha256')
            if photo.get('picpeak_photo_id'):
                version = versions[(photo['picpeak_event_id'], photo['picpeak_photo_id'])]
                image_path = fetch_original(photo, originals, cancel=cancel, source_version=version)
                photo.update(stored_name=image_path.name, original_source_version=version)
            else:
                image_path = original_path(photo, originals)
            content_sha = file_sha(image_path)
            if content_sha != before or photo.get('preview_original_sha256') != content_sha:
                refresh_previews(photo, image_path, previews, thumbs, srgb)
                photo['preview_original_sha256'] = content_sha
                if before and content_sha != before:
                    changed.append(key)
            photo.update(original_sha256=content_sha, original_checked_at=datetime.now(timezone.utc).isoformat())
            write_json(path, photo)
            photos[key] = photo
            manifest[key] = content_sha
        if progress:
            progress(index)
    return {'photos': manifest, 'galleries': galleries, 'changed': changed,
            'checked_at': datetime.now(timezone.utc).isoformat()}


def assert_sources(expected, actual):
    if not expected or expected != actual:
        raise ValueError('The original photos changed or were not verified for this proof. Publish a fresh proof and request approval again.')


@contextmanager
def render_snapshot(project, photos, originals, expected):
    """Copy inputs, then hash the copies: subsequent replacements cannot change a ZIP."""
    with tempfile.TemporaryDirectory(prefix='album-render-') as folder:
        root = Path(folder)
        for key in used_ids(project):
            source = original_path(photos[key], originals)
            target = root / source.name
            shutil.copyfile(source, target)
            if file_sha(target) != expected.get(key):
                raise ValueError('An original changed while preparing print files. Check sources and approve a fresh proof.')
        yield root
