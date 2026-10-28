"""Verify immutable print archives and recover their identity after restarts."""
import hashlib
import json
from pathlib import Path
import zipfile


def archive_info(path: Path, *, allow_legacy=False):
    with zipfile.ZipFile(path) as archive:
        record = archive.getinfo('print-check.json')
        if record.file_size > 2*1024*1024:
            raise ValueError('Print archive report is too large')
        manifest = json.loads(archive.read(record))
        if archive.testzip() is not None:
            raise ValueError('Print archive failed its integrity check')
    if not isinstance(manifest, dict):
        raise ValueError('Print archive report is invalid')
    legacy = 'project_revision' not in manifest
    if legacy:
        if not (allow_legacy and isinstance(manifest.get('title'), str) and
                type(manifest.get('spreads')) is int and manifest['spreads'] > 0 and
                type(manifest.get('printed_sides')) is int and
                isinstance(manifest.get('specifications'), dict)):
            raise ValueError('Print archive has no reliable album revision')
    elif type(manifest.get('project_revision')) is not int:
        raise ValueError('Print archive has no reliable album revision')
    with path.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    stat = path.stat()
    return {'manifest': manifest, 'sha256': checksum, 'size': stat.st_size,
            'mtime_ns': stat.st_mtime_ns, 'project_revision': manifest.get('project_revision'), 'legacy_unversioned': legacy,
            'project_title': manifest.get('title', 'Recovered album'),
            'project_id': manifest.get('project_id')}
