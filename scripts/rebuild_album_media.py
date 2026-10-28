#!/usr/bin/env python3
"""Rebuild Studio previews from backed-up local originals and PicPeak/R2."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from picpeak_import import fetch_original, import_previews
from renderer import SRGB_PROFILE, _srgb


DEFAULT_DATA = Path(__file__).resolve().parents[1] / "data"


def rebuild_local(data: Path) -> int:
    (data / "previews").mkdir(exist_ok=True)
    (data / "thumbs").mkdir(exist_ok=True)
    restored = 0
    for metadata in sorted((data / "photos").glob("*.json")):
        photo = json.loads(metadata.read_text(encoding="utf-8"))
        if photo.get("picpeak_event_id") and photo.get("picpeak_photo_id"):
            continue
        original = data / "originals" / photo["stored_name"]
        if not original.is_file():
            raise FileNotFoundError(f"Local-only photo is missing: {photo['stored_name']}")
        missing = [(size, path) for size, path in (
            (1600, data / "previews" / f"{photo['id']}.jpg"),
            (390, data / "thumbs" / f"{photo['id']}.jpg")) if not path.is_file()]
        if not missing:
            continue
        with Image.open(original) as image:
            full = _srgb(image)
        try:
            for size, target in missing:
                preview = full.copy()
                try:
                    preview.thumbnail((size, size), Image.Resampling.LANCZOS)
                    temporary = target.with_suffix(".tmp")
                    preview.save(temporary, "JPEG", quality=85, icc_profile=SRGB_PROFILE)
                    os.replace(temporary, target)
                finally:
                    preview.close()
            restored += 1
        finally:
            full.close()
    return restored


def rebuild_picpeak(data: Path, *, fetch_selected: bool = False) -> tuple[int, int]:
    photos = {}
    event_ids = set()
    for path in (data / "photos").glob("*.json"):
        photo = json.loads(path.read_text(encoding="utf-8"))
        photos[photo["id"]] = photo
        if photo.get("picpeak_event_id") and photo.get("picpeak_photo_id"):
            if not (data / "previews" / f"{photo['id']}.jpg").is_file() or not (
                    data / "thumbs" / f"{photo['id']}.jpg").is_file():
                event_ids.add(int(photo["picpeak_event_id"]))
    for event_id in sorted(event_ids):
        import_previews(event_id, data / "photos", data / "previews", data / "thumbs", SRGB_PROFILE)
    missing = [photo["id"] for photo in photos.values()
               if photo.get("picpeak_event_id") and photo.get("picpeak_photo_id") and
               (not (data / "previews" / f"{photo['id']}.jpg").is_file() or
                not (data / "thumbs" / f"{photo['id']}.jpg").is_file())]
    if missing:
        raise RuntimeError(f"Could not rebuild {len(missing)} PicPeak previews; check the gallery and R2 backup")
    fetched = 0
    if fetch_selected:
        used = set()
        for path in (data / "projects").glob("*.json"):
            project = json.loads(path.read_text(encoding="utf-8"))
            used.update(slot["photo_id"] for spread in project.get("spreads", [])
                        for slot in spread.get("slots", []))
            if project.get("cover_photo_id"):
                used.add(project["cover_photo_id"])
        for photo_id in sorted(used):
            photo = photos.get(photo_id)
            if not photo:
                raise ValueError(f"Project references an unknown photo: {photo_id}")
            if not photo.get("picpeak_photo_id"):
                continue
            original = fetch_original(photo, data / "originals")
            with Image.open(original) as image:
                photo["source_icc"] = bool(image.info.get("icc_profile"))
            photo["stored_name"] = original.name
            path = data / "photos" / f"{photo_id}.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(photo, indent=2), encoding="utf-8")
            os.replace(temporary, path)
            fetched += 1
    return len(event_ids), fetched


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--local-only", action="store_true", help="Do not contact PicPeak/R2")
    parser.add_argument("--fetch-selected-originals", action="store_true",
                        help="Download only photos used on project spreads and covers")
    args = parser.parse_args()
    if args.local_only and args.fetch_selected_originals:
        parser.error("Cannot fetch PicPeak originals in local-only mode")
    local = rebuild_local(args.data)
    events = fetched = 0
    if not args.local_only:
        events, fetched = rebuild_picpeak(args.data, fetch_selected=args.fetch_selected_originals)
    print(f"Rebuilt {local} local photo previews; refreshed {events} PicPeak galleries; "
          f"fetched {fetched} selected originals")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
