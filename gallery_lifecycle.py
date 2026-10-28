"""Request-driven public gallery availability checks; no database credentials.

The public proof service uses only PicPeak's existing public information route.
Failures close client access temporarily, without deleting approval/payment data.
"""
import json
import os
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPRedirectHandler

_CACHE = {}
_LOCK = threading.Lock()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class GalleryCheckUnavailable(ValueError):
    pass


def gallery_access(proof):
    if proof.get('preview_only'):
        return True
    galleries = proof.get('source_galleries', [])
    if not galleries:
        return True  # Existing snapshots retain their original lifetime.
    base = os.environ.get('ALBUM_PICPEAK_INFO_BASE', '').rstrip('/')
    if not base:
        raise GalleryCheckUnavailable('Gallery access cannot be checked. Please try again later.')
    for gallery in galleries:
        slug = gallery.get('slug', '')
        if not isinstance(slug, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,200}', slug):
            return False
        key = (base, slug)
        with _LOCK:
            cached = _CACHE.get(key)
            if cached and cached[0] > time.monotonic():
                allowed = cached[1]
            else:
                try:
                    request = Request(base + '/' + quote(slug, safe='') + '/info',
                                      headers={'Accept': 'application/json', 'Cache-Control': 'no-cache'})
                    with build_opener(NoRedirect).open(request, timeout=2) as response:
                        raw = response.read(128 * 1024 + 1)
                    if len(raw) > 128 * 1024:
                        raise ValueError('Invalid gallery response')
                    value = json.loads(raw)
                    if not isinstance(value, dict) or 'is_expired' not in value:
                        raise ValueError('Invalid gallery response')
                    allowed = (value.get('event_type') == 'wedding' and
                               value.get('is_active') in (True, 1, '1') and
                               value['is_expired'] is False and not value.get('hidden_until_reveal'))
                except HTTPError as exc:
                    if exc.code not in (404, 410):
                        raise GalleryCheckUnavailable('The gallery is temporarily unavailable. Please try again later.') from exc
                    allowed = False
                except Exception as exc:
                    raise GalleryCheckUnavailable('The gallery is temporarily unavailable. Please try again later.') from exc
                # At most one request per gallery/minute while someone is reviewing.
                if len(_CACHE) >= 2048:
                    _CACHE.clear()
                _CACHE[key] = (time.monotonic() + 60, allowed)
        if not allowed:
            return False
    return True
