#!/usr/bin/env python3
"""Back up album decisions and irreplaceable local photos; verify restoration.

PicPeak-sourced originals, previews and print exports can be rebuilt from the
private PicPeak R2 storage and backed-up database. Local-only originals cannot.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from uuid import UUID


SOURCE = Path(__file__).resolve().parent.parent / "data"
DESTINATION = Path.home() / ".local" / "share" / "album-atelier" / "backups"
NAME = re.compile(r"\d{8}T\d{6}Z\.tar\.zst\Z")
REQUIRED = {"originals", "photos", "projects", "layouts", "proofs"}
REBUILDABLE_DIRS = {"local-previews", "previews", "thumbs", "exports"}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def remote_original_names(source: Path) -> set[str]:
    """Only omit files with an unambiguous PicPeak event/photo source."""
    remote: set[str] = set()
    local: set[str] = set()
    for path in (source / "photos").glob("*.json"):
        photo = json.loads(path.read_text(encoding="utf-8"))
        photo_id = str(UUID(path.stem))
        if photo.get("id") != photo_id:
            raise ValueError(f"Photo ID does not match metadata file: {path.name}")
        name = photo.get("stored_name")
        if not isinstance(name, str) or Path(name).name != name or not name.startswith(photo_id + "."):
            raise ValueError(f"Unsafe original filename in {path.name}")
        event_id, source_id = photo.get("picpeak_event_id"), photo.get("picpeak_photo_id")
        if bool(event_id) != bool(source_id):
            raise ValueError(f"Incomplete PicPeak source in {path.name}")
        if event_id and source_id:
            if any(type(value) is not int or value <= 0 for value in (event_id, source_id)):
                raise ValueError(f"Invalid PicPeak source in {path.name}")
            remote.add(name)
        else:
            original = source / "originals" / name
            if not original.is_file() or original.is_symlink():
                raise ValueError(f"Local-only original missing: {name}")
            local.add(name)
    if remote & local:
        raise ValueError("A local-only photo shares an original filename with a PicPeak photo")
    return remote


def manifest_for(source: Path, *, slim: bool = False) -> dict[str, dict]:
    if not source.is_dir() or source.is_symlink():
        raise ValueError("Album data source must be a real directory")
    if not all((source / name).is_dir() for name in REQUIRED):
        raise ValueError("Album data is missing a required directory")
    remote = remote_original_names(source) if slim else set()
    result = {}
    for base, directories, files in os.walk(source, followlinks=False):
        base_path = Path(base)
        directories[:] = [name for name in directories if not (
            slim and base_path == source and name in REBUILDABLE_DIRS)]
        for name in directories:
            if (base_path / name).is_symlink():
                raise ValueError(f"Symlink in album data: {base_path / name}")
        for name in files:
            path = base_path / name
            if slim and ((base_path == source and name == "studio.log") or
                         (base_path == source / "originals" and name in remote)):
                continue
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError(f"Non-regular album file: {path}")
            result[str(path.relative_to(source))] = {"size": path.stat().st_size, "sha256": digest(path)}
    if not result:
        raise ValueError("Album data has no files to back up")
    return result


def verify(archive: Path, manifest_path: Path, checksum_path: Path) -> int:
    expected = checksum_path.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or digest(archive) != expected:
        raise ValueError("Archive checksum mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="album-restore-") as temp:
        subprocess.run(["tar", "-I", "zstd", "-xf", str(archive), "-C", temp], check=True)
        restored = Path(temp) / "data"
        actual = manifest_for(restored)
        if actual != manifest:
            raise ValueError("Restored files differ from the backup manifest")
    return len(manifest)


def backup(source: Path, destination: Path, *, keep_days: int = 14) -> Path:
    if keep_days < 2:
        raise ValueError("Keep at least two days of backups")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = destination / f"{stamp}.tar.zst"
    if archive.exists():
        raise FileExistsError(f"Backup already exists: {archive}")
    with tempfile.TemporaryDirectory(prefix=".album-backup-", dir=destination) as temp:
        partial = Path(temp) / archive.name
        manifest_path = Path(temp) / f"{stamp}.manifest.json"
        checksum_path = Path(temp) / f"{stamp}.sha256"
        remote = remote_original_names(source)
        manifest = manifest_for(source, slim=True)
        manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        exclusions = Path(temp) / "excluded-paths.txt"
        exclusions.write_text("\n".join([
            *(f"{source.name}/{name}" for name in sorted(REBUILDABLE_DIRS)),
            f"{source.name}/studio.log",
            *(f"{source.name}/originals/{name}" for name in sorted(remote)),
        ]) + "\n", encoding="utf-8")
        subprocess.run(["tar", "-I", "zstd -T1 -3", "-C", str(source.parent),
                        "--exclude-from", str(exclusions), "-cf", str(partial), source.name], check=True)
        checksum_path.write_text(digest(partial) + "\n", encoding="ascii")
        count = verify(partial, manifest_path, checksum_path)
        for path in (partial, manifest_path, checksum_path):
            path.chmod(0o600)
            target = archive if path == partial else destination / path.name
            os.replace(path, target)
    cached_remote = sum((source / "originals" / name).is_file() for name in remote)
    print(f"Album backup verified: {archive} ({count} files; {cached_remote} cached PicPeak originals omitted)")
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    archives = sorted(path for path in destination.iterdir()
                      if NAME.fullmatch(path.name) and path.is_file() and not path.is_symlink())
    if len(archives) >= 2:
        for old in archives[:-2]:
            date = datetime.strptime(old.name[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            if date >= cutoff:
                continue
            for path in (old, old.with_name(old.name.removesuffix(".tar.zst") + ".manifest.json"),
                         old.with_name(old.name.removesuffix(".tar.zst") + ".sha256")):
                if path.is_file() and not path.is_symlink():
                    path.unlink()
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "verify"))
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--destination", type=Path, default=DESTINATION)
    parser.add_argument("--archive", type=Path, help="Archive to verify; defaults to latest")
    args = parser.parse_args()
    if args.action == "backup":
        backup(args.source, args.destination)
    else:
        archives = (sorted(path for path in args.destination.iterdir() if NAME.fullmatch(path.name))
                    if args.archive is None else [])
        archive = args.archive or (archives[-1] if archives else None)
        if archive is None:
            raise ValueError("No album backup to verify")
        base = archive.name.removesuffix(".tar.zst")
        count = verify(archive, archive.with_name(base + ".manifest.json"),
                       archive.with_name(base + ".sha256"))
        print(f"Album restore verified: {archive} ({count} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
