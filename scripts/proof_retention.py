#!/usr/bin/env python3
"""Prune preview JPEGs from expired album proofs, retaining their audit records.

Dry-run by default. Designed for the Pi host, outside the public proof container.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID


PHOTO_NAME = re.compile(r"[0-9a-f-]{36}(?:-thumb)?\.jpg\Z")


def prune(root: Path, *, now: datetime, days: int, apply: bool) -> tuple[int, int, list[str]]:
    if days < 7:
        raise ValueError("Keep at least seven days after proof expiry")
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"Proof root must be a real directory: {root}")
    count = bytes_total = 0
    problems: list[str] = []
    for folder in sorted(root.iterdir()):
        try:
            if folder.name == "active":
                continue
            if str(UUID(folder.name)) != folder.name or not folder.is_dir() or folder.is_symlink():
                raise ValueError("Unexpected proof directory")
            record = folder / "proof.json"
            if not stat.S_ISREG(record.lstat().st_mode):
                raise ValueError("Missing or unsafe proof record")
            proof = json.loads(record.read_text(encoding="utf-8"))
            if proof.get("id") != folder.name:
                raise ValueError("Proof ID does not match directory")
            expiry = datetime.fromisoformat(proof["expires_at"])
            if expiry.tzinfo is None:
                raise ValueError("Proof expiry has no timezone")
            if expiry + timedelta(days=days) >= now:
                continue
            media = folder / "media"
            if not media.exists() and not media.is_symlink():
                continue
            if not media.is_dir() or media.is_symlink():
                raise ValueError("Unsafe media directory")
            files = list(media.iterdir())
            if any(not PHOTO_NAME.fullmatch(p.name) or not stat.S_ISREG(p.lstat().st_mode)
                   or str(UUID(p.name[:36])) != p.name[:36] for p in files):
                raise ValueError("Unexpected file in media directory")
            total = sum(p.stat().st_size for p in files)
            if apply:
                for path in files:
                    path.unlink()
                media.rmdir()
            count += 1
            bytes_total += total
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            problems.append(f"{folder.name}: {exc}")
    return count, bytes_total, problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Explicit path to the proof data directory")
    parser.add_argument("--days", type=int, default=30, help="Days to keep previews after proof expiry")
    parser.add_argument("--apply", action="store_true", help="Remove eligible preview JPEGs")
    args = parser.parse_args()
    count, size, problems = prune(args.root, now=datetime.now(timezone.utc), days=args.days,
                                  apply=args.apply)
    verb = "Removed" if args.apply else "Would remove"
    print(f"{verb} preview media for {count} expired proofs ({size} bytes); proof records retained")
    for problem in problems:
        print(f"Skipped {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
