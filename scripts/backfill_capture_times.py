#!/usr/bin/env python3
"""Repair capture-time metadata from existing local originals; dry-run by default."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from capture_time import exif_capture
from project_store import write_json, project_lock


def backfill(data, apply=False):
    changed = failed = 0
    for path in (data / 'photos').glob('*.json'):
        with project_lock(path):
            photo = json.loads(path.read_text())
            if photo.get('picpeak_event_id') or photo.get('capture_time_source') == 'manual':
                continue
            name = photo.get('stored_name', '')
            original = data / 'originals' / name
            if Path(name).name != name or not original.is_file():
                failed += 1
                continue
            try:
                with Image.open(original) as image:
                    capture = exif_capture(image.getexif())
            except (OSError, ValueError):
                failed += 1
                continue
            if not capture['captured_at'] or all(photo.get(key)==value for key,value in capture.items()):
                continue
            changed += 1
            if apply:
                write_json(path, {**photo, **capture})
    return {'updated' if apply else 'would_update': changed, 'unavailable': failed}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=Path(__file__).resolve().parents[1] / 'data')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(backfill(args.data, args.apply)))
