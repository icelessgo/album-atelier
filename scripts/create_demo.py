#!/usr/bin/env python3
"""Create a reproducible synthetic story without reading or publishing real photos."""
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from timestamp_selection import select_by_time


def main():
    marker = server.DATA / 'demo-seed.json'
    if marker.is_file():
        previous = json.loads(marker.read_text())
        if (server.PROJECTS / (previous['project_id'] + '.json')).is_file():
            print('Synthetic story — demo already exists; no data changed.')
            return
    photos = {}
    start = datetime(2026, 1, 1, 9, tzinfo=timezone.utc)
    for index in range(26):
        width, height = (2400, 3600) if index % 3 == 0 else (3600, 2400)
        image = Image.new('RGB', (width, height), '#f3eadd')
        draw = ImageDraw.Draw(image)
        colours = ('#5f7f87', '#bd6955', '#849772', '#58738d')
        colour = colours[index % len(colours)]
        draw.rectangle((0, 0, width, height * .6), fill=colour)
        draw.ellipse((width * .60, height * .12, width * .86, height * .12 + width * .26), fill='#f4d5a1')
        draw.polygon([(0, height * .74), (width * .32, height * .34), (width * .71, height * .74)], fill='#293f4b')
        draw.polygon([(width * .27, height * .76), (width * .7, height * .43), (width, height * .76)], fill='#80918b')
        font = ImageFont.load_default(size=round(width / 19))
        draw.text((width * .07, height * .84), f'SYNTHETIC {index + 1:02}', fill='#293f4b', font=font)
        data = BytesIO()
        image.save(data, 'PNG', icc_profile=server.SRGB_PROFILE)
        image.close()
        photo = server.store_photo(data.getvalue(), f'Synthetic frame {index + 1:02}.png')
        moment = (start + timedelta(minutes=index * 22)).isoformat()
        photo.update(captured_at=moment, captured_at_utc=moment, chapter='Unsorted')
        server.write_json(server.PHOTO_META / (photo['id'] + '.json'), photo)
        photos[photo['id']] = photo
    selected, report = select_by_time(list(photos.values()), 80)
    design_format = 'saal_portrait_22x30'
    spreads = server.build_directed_draft(selected, photos, 13, 2, design_format,
                                          avoid_similar=False, preserve_all=True)
    project = server.create_project('Synthetic story — demo', photo_ids=selected,
                                    shortlist=selected, spreads=spreads)
    project.update(design_format=design_format, cover_photo_id=selected[0],
                   timestamp_draft=report)
    server.write_project(project)
    server.check_project_sources(project)
    server.write_json(marker, {'schema_version': 1, 'project_id': project['id']})
    print(f'Created {len(photos)} geometric images and {len(spreads)} spreads.')
    print('Open Synthetic story — demo in the local Studio. Nothing was published, approved or ordered.')


if __name__ == '__main__':
    main()
