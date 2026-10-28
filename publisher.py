"""Publish sanitized proof snapshots to the Pi over the existing SSH key."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from uuid import UUID
from project_store import project_lock


def public_base() -> str | None:
    return os.environ.get("ALBUM_PUBLIC_REVIEW_BASE", "").rstrip("/") or None


def remote_root() -> str | None:
    return os.environ.get("ALBUM_PROOF_REMOTE", "").rstrip("/") or None


def _rsync(source: Path, destination: str) -> None:
    subprocess.run([
        "rsync", "-a", "--chown=65534:65534", "--chmod=D700,F600",
        "-e", "ssh -o BatchMode=yes -o ConnectTimeout=10",
        str(source), destination,
    ], check=True, timeout=300, capture_output=True)


def publish_proof(root: Path, proof_id: str) -> bool:
    remote = remote_root()
    if not remote:
        return False
    proof_id = str(UUID(proof_id))
    _rsync(root / proof_id, remote + "/")
    return True


def pull_proof(root: Path, proof_id: str) -> bool:
    remote = remote_root()
    if not remote:
        return False
    proof_id = str(UUID(proof_id))
    target = root / proof_id / "proof.json"
    temp = target.with_suffix(".remote.tmp")
    try:
        subprocess.run([
            "rsync", "-a", "--no-owner", "--no-group", "-e",
            "ssh -o BatchMode=yes -o ConnectTimeout=10",
            remote + f"/{proof_id}/proof.json", str(temp),
        ], check=True, timeout=30, capture_output=True)
        with project_lock(target):
            os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return True


def set_active(root: Path, project_id: str, proof_id: str | None) -> None:
    project_id = str(UUID(project_id))
    if proof_id:
        proof_id = str(UUID(proof_id))
    active = root / "active"
    active.mkdir(exist_ok=True)
    target = active / f"{project_id}.txt"
    temp = active / f"{project_id}.tmp"
    temp.write_text(proof_id or "revoked", encoding="ascii")
    os.replace(temp, target)
    remote = remote_root()
    if remote:
        _rsync(target, remote + "/active/")
