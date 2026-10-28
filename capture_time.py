"""Capture-time parsing shared by local imports, chronology and burst filtering."""
from datetime import datetime, timedelta, timezone
import re


def parse_time(value):
    if not value:
        return None
    try:
        text = str(value).strip().strip('\x00')
        if re.match(r'^\d{4}:\d\d:\d\d ', text):
            text = text[:10].replace(':', '-') + 'T' + text[11:]
        return datetime.fromisoformat(text.replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


def exif_capture(exif):
    try:
        nested = exif.get_ifd(34665)
    except (AttributeError, KeyError, TypeError, ValueError, OSError):
        nested = {}
    for date_tag, offset_tag, subsecond_tag, source in (
        (36867, 36881, 37521, 'original'),
        (36868, 36882, 37522, 'digitized'),
        (306, 36880, 37520, 'modified'),
    ):
        raw = nested.get(date_tag) or exif.get(date_tag)
        moment = parse_time(raw)
        if moment is None:
            continue
        offset = str(nested.get(offset_tag) or exif.get(offset_tag) or '').strip().strip('\x00')
        if re.fullmatch(r'[+-](?:0\d|1[0-4]):[0-5]\d', offset):
            minutes = int(offset[1:3]) * 60 + int(offset[4:6])
            moment = moment.replace(tzinfo=timezone(timedelta(minutes=minutes if offset[0]=='+' else -minutes)))
        subsecond = str(nested.get(subsecond_tag) or exif.get(subsecond_tag) or '').strip().strip('\x00')
        if re.fullmatch(r'\d{1,9}', subsecond):
            moment = moment.replace(microsecond=int((subsecond + '000000')[:6]))
        return {'captured_at': moment.strftime('%Y:%m:%d %H:%M:%S'),
                'captured_at_utc': moment.astimezone(timezone.utc).isoformat() if moment.tzinfo else None,
                'capture_offset': offset if moment.tzinfo else None,
                'capture_time_source': source}
    return {'captured_at': '', 'captured_at_utc': None, 'capture_offset': None,
            'capture_time_source': 'filename'}


def photo_moment(photo):
    return parse_time(photo.get('captured_at_utc') or photo.get('captured_at'))


def photo_sort_key(photo):
    moment = photo_moment(photo)
    if moment:
        # Unknown offsets stay on their recorded wall clock; never guess the
        # desktop timezone. Known offsets compare as UTC instants.
        moment = moment.astimezone(timezone.utc) if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
        value = moment.isoformat()
    else:
        value = photo.get('name', '')
    return value, photo.get('name', '')
