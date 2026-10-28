"""Current PicPeak membership is separate from historical album references."""
import json
from pathlib import Path


def manifest_path(metadata: Path, event_id: int):
    return metadata.parent / 'picpeak-galleries' / f'{int(event_id)}.json'


def current_manifest(metadata: Path, event_id: int):
    path = manifest_path(metadata, event_id)
    return json.loads(path.read_text()) if path.is_file() else None


def photo_allowed(photo, manifests):
    event = photo.get('picpeak_event_id')
    if not event:
        return True
    manifest = manifests.get(event)
    if manifest is None:
        # Legacy images are not silently granted new public access. Refresh
        # their source before publishing; existing proof snapshots stay intact.
        return False
    row = manifest['photos'].get(photo['id'])
    return bool(row and row.get('visibility') == 'visible' and row.get('readable') and
                photo.get('picpeak_visibility', 'visible') == 'visible' and photo.get('picpeak_current', True))


def annotate_photos(photos, metadata):
    manifests = {event: current_manifest(metadata, event)
                 for event in {photo.get('picpeak_event_id') for photo in photos.values()} if event}
    for photo in photos.values():
        event = photo.get('picpeak_event_id')
        if not event:
            continue
        manifest = manifests.get(event)
        row = manifest['photos'].get(photo['id']) if manifest else None
        photo['source_available'] = photo_allowed(photo, manifests)
        photo['source_issue'] = (None if photo['source_available'] else
            'Refresh this PicPeak gallery before sharing' if manifest is None else
            'Removed from PicPeak' if row is None else
            'Hidden in PicPeak' if row.get('visibility') != 'visible' else
            'Preview unavailable in the latest import')
    return photos


def sharing_issues(project, photos):
    used = {slot['photo_id'] for spread in project.get('spreads', []) for slot in spread['slots']}
    if project.get('cover_photo_id'):
        used.add(project['cover_photo_id'])
    return [{'id': key, 'name': photos.get(key, {}).get('name', key),
             'reason': photos.get(key, {}).get('source_issue', 'Photo is unavailable')}
            for key in used if key not in photos or photos[key].get('source_available') is False]
