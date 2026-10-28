#!/usr/bin/env python3
"""Loopback-only development server for the Graphistudio album prototype."""

from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPConnection
from io import BytesIO
from itertools import permutations
import json
import hashlib
from functools import wraps
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import threading
from urllib.parse import parse_qs, unquote, urlparse
from uuid import UUID, uuid4

from PIL import Image, ImageCms, ImageDraw, ImageOps

from renderer import (LAYOUTS, SPECS, SRGB_PROFILE, PRINT_FORMAT_SPECS, _srgb,
                      export_album, preflight_project, spec_dict, validate_project)
from proofing import COMPARISON_FORMATS, companion_counts, create_proof, is_open, load_proof, revoke_proof, public_view, _save
from publisher import public_base, publish_proof, pull_proof, set_active
from project_store import ProjectConflict, project_lock, write_json
from gallery_sources import annotate_photos, current_manifest, sharing_issues
from export_records import archive_info
from bridge_stream import BridgeCancelled
from capture_time import exif_capture, photo_moment, photo_sort_key
from timestamp_selection import select_by_time
from gallery_showcase import prepare_showcase
from picpeak_import import galleries as picpeak_galleries, import_previews, fetch_original
from saal_profiles import SAAL_PROFILES, is_saal, minimum_spreads, cover_crop, saal_offer
from studio_access import load_access, request_origin
from product_catalogue import load_catalogue, save_catalogue, offer_for as catalogue_offer, order_details as catalogue_details
from approval_record import validate_agreement, summary as agreement_summary, design_identity
from source_integrity import verify_sources, assert_sources, used_ids, render_snapshot
import crm_handoff


ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("ALBUM_DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
PRIVATE_ACCESS = load_access(DATA / "private-access.json")
ORIGINALS = DATA / "originals"
PHOTO_META = DATA / "photos"
PREVIEWS = DATA / "previews"
THUMBS = DATA / "thumbs"
PROJECTS = DATA / "projects"
EXPORTS = DATA / "exports"
JOB_RECORDS = DATA / "jobs"
CUSTOM_LAYOUTS_DIR = DATA / "layouts"
PROOFS = DATA / "proofs"
LOCAL_PREVIEW_PROOFS = DATA / "local-previews"
LOCAL_REVIEW_PORT: int | None = None
LOCAL_PREVIEW_LOCK = threading.Lock()
STATIC = ROOT / "static"
MAX_UPLOAD = 80 * 1024 * 1024
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.RLock()
JOB_CANCEL = {}
EXPORT_SLOTS = threading.BoundedSemaphore(1)
IMPORT_SLOTS = threading.BoundedSemaphore(1)

for folder in (ORIGINALS, PHOTO_META, PREVIEWS, THUMBS, PROJECTS, EXPORTS,
               CUSTOM_LAYOUTS_DIR, PROOFS, LOCAL_PREVIEW_PROOFS, JOB_RECORDS):
    folder.mkdir(parents=True, exist_ok=True)

CUSTOM_LAYOUTS: dict[str, dict] = {}

# GoBook dimensions come from its product-specific guide. EDGE interiors use
# the general Print & Bind guide, as chosen by the user. The legacy ratio
# preserves albums created before format selection existed.
DESIGN_FORMATS = {
    "legacy": {"label": "Existing wide draft", "spread_width_cm": 69.7, "spread_height_cm": 25.5},
    "landscape_40x30": {"label": "EDGE · 40×30 landscape", "spread_width_cm": 82, "spread_height_cm": 30},
    "portrait_30x40": {"label": "EDGE · 30×40 portrait", "spread_width_cm": 60, "spread_height_cm": 41},
    "gobook_landscape_35x25": {"label": "GoBook · 35×25 landscape", "spread_width_cm": 69.7, "spread_height_cm": 25.5},
    "gobook_portrait_25x35": {"label": "GoBook · 25×35 portrait", "spread_width_cm": 50.8, "spread_height_cm": 34.7},
}

for _key, _profile in SAAL_PROFILES.items():
    _w, _h = _profile['trim_mm']
    DESIGN_FORMATS[_key] = {'label': _profile['label'] + ' · €150', 'spread_width_cm': _w / 10, 'spread_height_cm': _h / 10}


def design_ratio(design_format: str) -> float:
    if design_format not in DESIGN_FORMATS:
        raise ValueError("Choose a valid album format")
    value = DESIGN_FORMATS[design_format]
    return value["spread_width_cm"] / value["spread_height_cm"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def locked_project(function):
    @wraps(function)
    def wrapped(value, *args, **kwargs):
        with project_lock(id_path(PROJECTS, value)):
            return function(value, *args, **kwargs)
    return wrapped


def locked_photo(function):
    @wraps(function)
    def wrapped(value, *args, **kwargs):
        with project_lock(id_path(PHOTO_META, value)):
            return function(value, *args, **kwargs)
    return wrapped


def check_revision(project, incoming):
    if incoming.get("revision") != project["revision"]:
        raise ProjectConflict("This album changed in another tab. Your draft is kept on this PC; review it before continuing.")


def project_source(project, photos=None):
    """Infer old single-gallery albums without restricting intentionally mixed albums."""
    if "source" in project:
        return project["source"]
    photos = photos if photos is not None else load_photos()
    used = set(project.get("photo_ids", []))
    used.update(slot["photo_id"] for spread in project.get("spreads", []) for slot in spread["slots"])
    if project.get("cover_photo_id"):
        used.add(project["cover_photo_id"])
    events = {photos.get(key, {}).get("picpeak_event_id") for key in used}
    if used and len(events) == 1 and None not in events:
        event = events.pop()
        return {"kind": "picpeak", "event_id": event,
                "photo_ids": [key for key, photo in photos.items() if photo.get("picpeak_event_id") == event]}
    return None


def validate_photo_selection(ids, photos, source=None, allow_empty=True):
    if (not isinstance(ids, list) or len(ids) > 10000 or
            any(not isinstance(key, str) or key not in photos for key in ids)):
        raise ValueError("Choose available album photos")
    if not allow_empty and not ids:
        raise ValueError("Select at least one photo before building a draft")
    if source and any(key not in source["photo_ids"] for key in ids):
        raise ValueError("Choose photos from this album’s PicPeak gallery")


def custom_layout_path(layout_id: str) -> Path:
    if not re.fullmatch(r"custom_[0-9a-f]{32}", layout_id):
        raise ValueError("Invalid custom layout identifier")
    return CUSTOM_LAYOUTS_DIR / f"{layout_id[7:]}.json"


def validate_layout_rects(value: object) -> list[list[float]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 4:
        raise ValueError("A layout needs one to four photo frames")
    rects = []
    for index, rect in enumerate(value, 1):
        if (not isinstance(rect, (list, tuple)) or len(rect) != 4 or
                any(isinstance(part, bool) or not isinstance(part, (int, float)) or
                    not math.isfinite(part) for part in rect)):
            raise ValueError(f"Frame {index} has invalid coordinates")
        x, y, width, height = [float(part) for part in rect]
        if (x < 0 or y < 0 or width < 0.06 or height < 0.12 or
                x + width > 1.000001 or y + height > 1.000001):
            raise ValueError(f"Frame {index} is too small or extends beyond the spread")
        rects.append([round(x, 4), round(y, 4), round(width, 4), round(height, 4)])
    for index, (x, y, width, height) in enumerate(rects, 1):
        if x + width > 1 or y + height > 1:
            raise ValueError(f"Frame {index} extends beyond the spread")
    for first in range(len(rects)):
        for second in range(first + 1, len(rects)):
            ax, ay, aw, ah = rects[first]
            bx, by, bw, bh = rects[second]
            if min(ax + aw, bx + bw) - max(ax, bx) > 0.0001 and min(ay + ah, by + bh) - max(ay, by) > 0.0001:
                raise ValueError(f"Frames {first + 1} and {second + 1} overlap")
    return rects


def load_custom_layouts() -> None:
    for path in CUSTOM_LAYOUTS_DIR.glob("*.json"):
        try:
            record = json_file(path)
            layout_id = record["id"]
            if custom_layout_path(layout_id) != path:
                raise ValueError("Layout filename does not match its identifier")
            record["rects"] = validate_layout_rects(record["rects"])
            LAYOUTS[layout_id] = record["rects"]
            CUSTOM_LAYOUTS[layout_id] = record
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"Ignoring invalid custom layout {path.name}: {exc}", file=sys.stderr)


def create_custom_layout(incoming: dict) -> dict:
    name = str(incoming.get("name", "")).strip()
    if not name or len(name) > 50:
        raise ValueError("Give the layout a name of up to 50 characters")
    if any(record["name"].casefold() == name.casefold() for record in CUSTOM_LAYOUTS.values()):
        raise ValueError("A layout with that name already exists")
    if len(CUSTOM_LAYOUTS) >= 50:
        raise ValueError("The prototype allows up to 50 saved layouts")
    rects = validate_layout_rects(incoming.get("rects"))
    if not isinstance(incoming.get("auto_build", False), bool):
        raise ValueError("Invalid automatic-builder setting")
    layout_id = f"custom_{uuid4().hex}"
    record = {"id": layout_id, "name": name, "rects": rects,
              "auto_build": incoming.get("auto_build", False), "created_at": now_iso()}
    write_json(custom_layout_path(layout_id), record)
    LAYOUTS[layout_id] = rects
    CUSTOM_LAYOUTS[layout_id] = record
    return record


def delete_custom_layout(layout_id: str) -> None:
    path = custom_layout_path(layout_id)
    if layout_id not in CUSTOM_LAYOUTS:
        raise FileNotFoundError("Custom layout not found")
    for project_path in PROJECTS.glob("*.json"):
        try:
            project = json_file(project_path)
        except (OSError, ValueError):
            continue
        if any(spread.get("layout") == layout_id for spread in project.get("spreads", [])):
            raise ValueError("This layout is used by an album. Replace it there before removing it.")
    path.unlink()
    CUSTOM_LAYOUTS.pop(layout_id)
    LAYOUTS.pop(layout_id)


load_custom_layouts()


def id_path(folder: Path, value: str) -> Path:
    try:
        key = str(UUID(value))
    except ValueError as exc:
        raise ValueError("Invalid identifier") from exc
    return folder / f"{key}.json"


def media_version(path):
    stat = path.stat()
    return f"{stat.st_mtime_ns:x}-{stat.st_size:x}"


def load_photos() -> dict[str, dict]:
    photos = {p.stem: json_file(p) for p in PHOTO_META.glob("*.json")}
    annotate_photos(photos, PHOTO_META)
    for photo in photos.values():
        photo["sort_time"] = photo_sort_key(photo)[0]
        for folder, kind in ((PREVIEWS, "preview"), (THUMBS, "thumb")):
            path = folder / f"{photo['id']}.jpg"
            if path.is_file():
                photo[kind + "_url"] = f"/media/{kind}/{photo['id']}.jpg?v={media_version(path)}"
    return photos


def list_projects() -> list[dict]:
    projects = []
    for path in PROJECTS.glob("*.json"):
        try:
            item = json_file(path)
            projects.append({key: item.get(key) for key in ("id", "title", "status", "updated_at")})
        except (OSError, ValueError):
            continue
    return sorted(projects, key=lambda p: p.get("updated_at") or "", reverse=True)


def create_project(title: str, photo_ids: list[str] | None = None, source: dict | None = None, shortlist: list[str] | None = None, spreads: list[dict] | None = None) -> dict:
    value = {
        "id": str(uuid4()), "title": title.strip()[:100] or "New album",
        "status": "draft", "product": None, "design_format": "legacy",
        "companion_option": None, "companion_quantities": {"one_mid": 0, "two_small": 0},
        "cover_photo_id": None, "spreads": spreads or [],
        "photo_ids": photo_ids or [], "source": source,
        "shortlist": shortlist if shortlist is not None else (photo_ids or []), "created_at": now_iso(),
        "updated_at": now_iso(), "revision": 1,
    }
    write_json(id_path(PROJECTS, value["id"]), value)
    return value


def write_project(project):
    write_json(id_path(PROJECTS, project["id"]), {key: value for key, value in project.items()
        if key not in ("source_issues", "source_current_ids")})


def project_event(project, event, **details):
    project.setdefault('history', []).append({'event': event, 'actor': 'photographer',
        'at': now_iso(), 'revision': project['revision'], **details})


def withdraw_project(project, reason):
    if project.get('approval_record'):
        project.setdefault('approval_history', []).append(project.pop('approval_record'))
    project.update(status='draft', product=None)
    if project.get('active_proof_id'):
        # Persist a failed withdrawal as actionable work; never silently claim success.
        project['sharing_pending'] = reason
        write_project(project)
        set_active(PROOFS, project['id'], None)
        revoke_proof(PROOFS, project['active_proof_id'])
    project.pop('sharing_pending', None)
    for key in ('approved_proof_id', 'approved_at', 'active_proof_id'):
        project.pop(key, None)
    project.update(status='draft', product=None)
    project_event(project, 'review_required', reason=reason)


def check_project_sources(project, *, cancel=None, progress=None):
    if project.get('sharing_pending'):
        withdraw_project(project, project['sharing_pending'])
    photos = load_photos()
    try:
        result = verify_sources(project, photos, ORIGINALS, PHOTO_META, PREVIEWS, THUMBS,
                                SRGB_PROFILE, cancel=cancel, progress=progress)
    except ValueError:
        if project.get('active_proof_id') or project.get('approval_record'):
            withdraw_project(project, 'Source photos or gallery access need review')
            project['revision'] += 1
            write_project(project)
        raise
    expected = project.get('approval_record', {}).get('source_manifest') or project.get('source_manifest')
    if expected and expected != result['photos']:
        withdraw_project(project, 'One or more original photos changed')
        project['revision'] += 1
        project['updated_at'] = now_iso()
    project.update(source_manifest=result['photos'], source_galleries=result['galleries'],
                   sources_checked_at=result['checked_at'])
    write_project(project)
    return result, photos


def withdraw_stale_export_approval(project_id, manifest):
    """A background export must never overwrite a newer Studio edit."""
    with project_lock(id_path(PROJECTS, project_id)):
        current = load_project(project_id)
        expected = current.get('approval_record', {}).get('source_manifest') or current.get('source_manifest', {})
        if ((current.get('active_proof_id') or current.get('approval_record')) and
                any(key in expected and expected[key] != value for key, value in manifest.items())):
            withdraw_project(current, 'Original photos changed during print preparation')
            current['revision'] += 1
            current['updated_at'] = now_iso()
            write_project(current)


def load_project(value: str) -> dict:
    path = id_path(PROJECTS, value)
    if not path.is_file():
        raise FileNotFoundError("Album not found")
    project = json_file(path)
    photos = load_photos()
    project["source"] = project_source(project, photos)
    project["source_issues"] = sharing_issues(project, photos)
    project["source_current_ids"] = [key for key, photo in photos.items()
        if photo.get("source_available", True) and
        (not project["source"] or (photo.get("picpeak_event_id") == project["source"]["event_id"] and key in project["source"]["photo_ids"]))]
    return project


@locked_project
def save_project(value: str, incoming: dict) -> dict:
    current = load_project(value)
    operation = incoming.get("_operation_id")
    digest = hashlib.sha256(json.dumps({key: incoming[key] for key in (
        "revision", "title", "spreads", "photo_ids", "cover_photo_id", "status", "product",
        "design_format", "companion_option", "companion_quantities", "order_details", "shortlist", "draft_settings", "cover_crop")
        if key in incoming}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if operation is not None:
        try:
            operation = str(UUID(operation))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("Invalid save operation") from exc
        receipt = next((row for row in current.get("_save_receipts", []) if row["id"] == operation), None)
        if receipt:
            acceptable = {digest}
            if receipt.get("version", 1) == 1:
                # P1 receipts predate persisted builder settings. Preserve their
                # retry identity across the upgrade instead of inventing a conflict.
                legacy = {key: incoming[key] for key in ("revision", "title", "spreads", "photo_ids", "cover_photo_id",
                    "status", "product", "design_format", "companion_option", "companion_quantities", "order_details", "shortlist")
                    if key in incoming}
                acceptable.add(hashlib.sha256(json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
            if receipt["digest"] not in acceptable or receipt["revision"] != current["revision"]:
                raise ProjectConflict("This album has newer changes. Your local draft has been kept.")
            return current
    check_revision(current, incoming)
    photos = load_photos()
    next_value = {key: part for key, part in current.items() if key not in ("source_issues", "source_current_ids")}
    next_value["title"] = str(incoming.get("title", current["title"]))[:100]
    next_value["spreads"] = incoming.get("spreads", current["spreads"])
    next_value["photo_ids"] = incoming.get("photo_ids", current["photo_ids"])
    next_value["shortlist"] = incoming.get("shortlist", current.get("shortlist", current["photo_ids"]))
    for ids in (next_value["photo_ids"], next_value["shortlist"]):
        validate_photo_selection(ids, photos, current.get("source"))
    next_value["cover_photo_id"] = incoming.get("cover_photo_id", current.get("cover_photo_id"))
    if next_value["cover_photo_id"] is not None:
        try:
            cover_id = str(UUID(next_value["cover_photo_id"]))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("Choose an available album photo for the cover") from exc
        if cover_id not in next_value["photo_ids"] or cover_id not in photos:
            raise ValueError("Cover photo must be in this album")
        next_value["cover_photo_id"] = cover_id
    if "cover_crop" in incoming or "cover_crop" in current:
        next_value["cover_crop"] = cover_crop(incoming.get("cover_crop", current.get("cover_crop")))
    next_value["status"] = incoming.get("status", current["status"])
    next_value["product"] = incoming.get("product", current["product"])
    next_value["design_format"] = incoming.get("design_format", current.get("design_format", "legacy"))
    design_ratio(next_value["design_format"])
    next_value["companion_option"] = incoming.get("companion_option", current.get("companion_option"))
    if next_value["companion_option"] not in (None, "one_mid", "two_small"):
        raise ValueError("Choose a valid companion-book option")
    quantity_input = incoming.get("companion_quantities", current.get("companion_quantities"))
    if ("companion_option" in incoming and "companion_quantities" not in incoming and
            incoming["companion_option"] != current.get("companion_option")):
        quantity_input = None
    next_value["companion_quantities"] = companion_counts(quantity_input, next_value["companion_option"])
    settings = incoming.get("draft_settings", current.get("draft_settings", {}))
    if (not isinstance(settings, dict) or
            set(settings) - {"target_spreads", "photos_per_spread", "avoid_similar", "similar_skipped", "target_photos", "selection_method"}):
        raise ValueError("Invalid draft settings")
    for key, lower, upper in (("target_spreads", 1, 40), ("photos_per_spread", 1, 4)):
        value_setting = settings.get(key, 20 if key == "target_spreads" else 3)
        if isinstance(value_setting, bool) or not isinstance(value_setting, int) or not lower <= value_setting <= upper:
            raise ValueError("Choose valid draft settings")
    if not isinstance(settings.get("avoid_similar", True), bool):
        raise ValueError("Invalid similar-photo setting")
    if (type(settings.get('target_photos', 80)) is not int or
            not 50 <= settings.get('target_photos', 80) <= 100 or
            settings.get('selection_method') not in (None, 'timestamps')):
        raise ValueError('Choose 50 to 100 photos for the timestamp draft')
    next_value["draft_settings"] = settings
    next_value["order_details"] = incoming.get("order_details", current.get("order_details", {}))
    if (not isinstance(next_value["order_details"], dict) or
            set(next_value["order_details"]) - {"cover", "box", "finish"} or
            any(not isinstance(item, str) or len(item) > 200 for item in next_value["order_details"].values())):
        raise ValueError("Invalid order details")
    if next_value["status"] not in ("draft", "approved"):
        raise ValueError("Invalid approval status")
    if is_saal(next_value["design_format"]) and any(next_value["companion_quantities"].values()):
        raise ValueError("Companion copies are not offered for Saal albums")
    if next_value["product"] not in (None, "gobook", "wedding_book", "edge", "saal"):
        raise ValueError("Invalid album product")
    for spread in next_value["spreads"]:
        validate_photo_selection([slot["photo_id"] for slot in spread["slots"]], photos, current.get("source"))
    if next_value["spreads"]:
        problems = validate_project(next_value, photos)
        if problems:
            raise ValueError(" ".join(problems))
    if next_value["status"] == "approved":
        if next_value["design_format"] not in PRINT_FORMAT_SPECS:
            raise ValueError("Choose a supported print format before approval")
        if not next_value["product"]:
            raise ValueError("Choose an album product before approval")
        if next_value["product"] not in PRINT_FORMAT_SPECS[next_value["design_format"]]:
            raise ValueError("The selected book does not match this print format")
        if next_value["design_format"] != "legacy" and not next_value.get("approved_proof_id"):
            raise ValueError("Publish and confirm a protected client proof before approval")
        if len(next_value["spreads"]) < minimum_spreads(next_value["design_format"]):
            raise ValueError(f"An approved book needs at least {minimum_spreads(next_value['design_format']) * 2} printed sides")
    if (next_value["order_details"] != current.get("order_details", {}) or
            next_value["title"] != current["title"] or
            next_value["spreads"] != current["spreads"] or
            next_value["design_format"] != current.get("design_format", "legacy") or
            next_value["photo_ids"] != current["photo_ids"] or
            next_value["cover_photo_id"] != current.get("cover_photo_id") or
            next_value.get("cover_crop") != current.get("cover_crop") or
            next_value["companion_option"] != current.get("companion_option") or
            next_value["companion_quantities"] != companion_counts(current.get("companion_quantities"), current.get("companion_option"))):
        next_value["status"] = "draft"
        next_value["product"] = None
        next_value.pop("approved_at", None)
    elif next_value["product"] != current["product"]:
        next_value["status"] = "draft"
        next_value.pop("approved_at", None)
    elif next_value["status"] == "approved" and current["status"] != "approved":
        next_value["approved_at"] = now_iso()
    if next_value["status"] == "draft":
        next_value.pop("approved_at", None)
        next_value.pop("approved_proof_id", None)
    if (next_value["order_details"] != current.get("order_details", {}) or
            next_value["spreads"] != current["spreads"] or
            next_value["design_format"] != current.get("design_format", "legacy") or
            next_value["photo_ids"] != current["photo_ids"] or
            next_value["cover_photo_id"] != current.get("cover_photo_id") or
            next_value.get("cover_crop") != current.get("cover_crop") or
            next_value["title"] != current["title"] or
            next_value["product"] != current["product"] or
            next_value["companion_option"] != current.get("companion_option") or
            next_value["companion_quantities"] != companion_counts(current.get("companion_quantities"), current.get("companion_option"))):
        if current.get("active_proof_id"):
            set_active(PROOFS, current["id"], None)
            revoke_proof(PROOFS, current["active_proof_id"])
        next_value.pop("active_proof_id", None)
        next_value.pop("approved_proof_id", None)
        if next_value.get('approval_record'):
            next_value.setdefault('approval_history', []).append(next_value.pop('approval_record'))
        project_event(next_value, 'design_changed')
    next_value["updated_at"] = now_iso()
    next_value["revision"] += 1
    if operation:
        next_value["_save_receipts"] = (current.get("_save_receipts", []) +
            [{"id": operation, "digest": digest, "version": 2, "revision": next_value["revision"]}])[-20:]
    write_json(id_path(PROJECTS, value), next_value)
    return load_project(value)


@locked_project
def apply_client_layout(project_id: str) -> dict:
    project = load_project(project_id)
    proof_id = project.get("active_proof_id")
    if not proof_id:
        raise ValueError("No active client proof")
    pull_proof(PROOFS, proof_id)
    proof = load_proof(PROOFS, proof_id)
    if (not is_open(proof) or proof.get("project_id") != project["id"] or
            not proof.get("client_edits_pending") or proof.get("approved")):
        raise ValueError("No client layout changes are awaiting review")
    photos = load_photos()
    used = list(dict.fromkeys(slot["photo_id"] for spread in proof["spreads"]
                              for slot in spread["slots"]))
    cover_photo_id = proof.get("cover_photo_id")
    if cover_photo_id not in {photo["id"] for photo in proof["photos"]} or cover_photo_id not in photos:
        raise ValueError("Client cover photo is unavailable in Studio")
    updated = {**project, "spreads": proof["spreads"],
               "design_format": proof.get('design_format', project.get('design_format', 'legacy')),
               "cover_photo_id": cover_photo_id,
               "photo_ids": list(dict.fromkeys(project["photo_ids"] + used + [cover_photo_id]))}
    if is_saal(project.get("design_format")):
        updated["cover_crop"] = cover_crop(proof.get("cover_crop"))
    validate_photo_selection(updated["photo_ids"], photos, project.get("source"))
    problems = validate_project(updated, photos)
    if problems:
        raise ValueError("Client layout cannot be applied: " + "; ".join(problems[:3]))
    if is_saal(project.get("design_format")):
        project["cover_crop"] = updated["cover_crop"]
    project.update(spreads=proof["spreads"], cover_photo_id=cover_photo_id,
                   design_format=updated['design_format'],
                   photo_ids=updated["photo_ids"],
                   status="draft", product=None)
    project.pop("approved_at", None)
    project.pop("approved_proof_id", None)
    project["client_layout_source"] = {"proof_id": proof_id,
                                       "revision": proof.get("layout_revision", 0)}
    set_active(PROOFS, project["id"], None)
    revoke_proof(PROOFS, proof_id)
    project.pop("active_proof_id", None)
    project["revision"] += 1
    project["updated_at"] = now_iso()
    project_event(project, 'client_layout_adopted', proof_id=proof_id)
    write_project(project)
    return load_project(project_id)


def store_photo(raw: bytes, display_name: str) -> dict:
    if not raw or len(raw) > MAX_UPLOAD:
        raise ValueError("Photo must be between 1 byte and 80 MB")
    with Image.open(BytesIO(raw)) as image:
        image.verify()
    with Image.open(BytesIO(raw)) as image:
        if image.format not in ("JPEG", "PNG"):
            raise ValueError("Only JPEG and PNG photos are supported")
        exif = image.getexif()
        capture = exif_capture(exif)
        source_icc = bool(image.info.get("icc_profile"))
        full = _srgb(image)
    photo_id = str(uuid4())
    extension = ".jpg" if raw[:2] == b"\xff\xd8" else ".png"
    original_name = photo_id + extension
    (ORIGINALS / original_name).write_bytes(raw)
    for size, folder in ((1600, PREVIEWS), (390, THUMBS)):
        preview = full.copy()
        preview.thumbnail((size, size), Image.Resampling.LANCZOS)
        preview.save(folder / f"{photo_id}.jpg", "JPEG", quality=85,
                     icc_profile=SRGB_PROFILE)
        preview.close()
    metadata = {
        "id": photo_id, "name": Path(display_name).name[:160] or "Photo",
        "stored_name": original_name, "width": full.width, "height": full.height,
        **capture, "added_at": now_iso(),
        "source_icc": source_icc,
        "visual_hash": visual_hash(full),
        "chapter": "Unsorted", "favorite": False, "excluded": False,
        "preview_url": f"/media/preview/{photo_id}.jpg",
        "thumb_url": f"/media/thumb/{photo_id}.jpg",
    }
    full.close()
    write_json(id_path(PHOTO_META, photo_id), metadata)
    return metadata


CHAPTERS = ("Getting ready", "Ceremony", "Portraits", "Reception", "Unsorted")


@locked_photo
def update_photo(photo_id: str, incoming: dict) -> dict:
    path = id_path(PHOTO_META, photo_id)
    if not path.is_file():
        raise FileNotFoundError("Photo not found")
    photo = json_file(path)
    if "chapter" in incoming:
        if incoming["chapter"] not in CHAPTERS:
            raise ValueError("Choose a valid story section")
        photo["chapter"] = incoming["chapter"]
    for field in ("favorite", "excluded"):
        if field in incoming:
            if not isinstance(incoming[field], bool):
                raise ValueError(f"{field} must be true or false")
            photo[field] = incoming[field]
    write_json(path, photo)
    return photo


def evenly_select(photos: list[dict], count: int) -> list[str]:
    photos = sorted(photos, key=photo_sort_key)
    if count <= 0:
        return []
    if count == 1:
        return [photos[len(photos) // 2]["id"]] if photos else []
    if len(photos) <= count:
        return [p["id"] for p in photos]
    return [photos[round(index * (len(photos) - 1) / (count - 1))]["id"]
            for index in range(count)]


def visual_hash(image: Image.Image) -> str:
    """Small difference hash for conservative burst de-duplication, not identity."""
    with image.convert("L").resize((9, 8), Image.Resampling.LANCZOS) as sample:
        pixels = sample.load()
        bits = 0
        for row in range(8):
            for column in range(8):
                bits = (bits << 1) | (pixels[column, row] > pixels[column + 1, row])
    return f"{bits:016x}"


def ensure_visual_hashes(photos: dict[str, dict], photo_ids: list[str]) -> None:
    """Backfill older local imports from their thumbnails without reading originals."""
    for photo_id in photo_ids:
        photo = photos.get(photo_id)
        if not photo or photo.get("visual_hash"):
            continue
        thumbnail = THUMBS / f"{photo_id}.jpg"
        if not thumbnail.is_file():
            continue
        with Image.open(thumbnail) as image:
            photo["visual_hash"] = visual_hash(image)
        with project_lock(id_path(PHOTO_META, photo_id)):
            path = id_path(PHOTO_META, photo_id)
            current = json_file(path) if path.is_file() else photo
            write_json(path, {**current, "visual_hash": photo["visual_hash"]})


def similar_burst_photo(first: dict, second: dict) -> bool:
    """Skip only very similar frames shot within half a minute."""
    try:
        difference = (int(first["visual_hash"], 16) ^ int(second["visual_hash"], 16)).bit_count()
        first_time, second_time = photo_moment(first), photo_moment(second)
        if first_time is None or second_time is None or bool(first_time.tzinfo) != bool(second_time.tzinfo):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    return difference <= 4 and abs((first_time - second_time).total_seconds()) <= 30


def skip_similar_burst_photos(group: list[dict]) -> tuple[list[dict], int]:
    """Keep every favorite, and one representative from each near-identical burst."""
    favorites = [photo for photo in group if photo.get("favorite")]
    picked = []
    skipped = 0
    for photo in group:
        if photo.get("favorite"):
            picked.append(photo)
        elif any(similar_burst_photo(photo, other) for other in (*favorites, *picked)):
            skipped += 1
        else:
            picked.append(photo)
    return picked, skipped


def ranked_layouts(photo_ids: list[str], photos: dict[str, dict], previous: str | None = None,
                   design_format: str = "legacy") -> list[dict]:
    """Rank templates by how much they would crop these photos."""
    spread_ratio = design_ratio(design_format)
    options = []
    for name, rects in LAYOUTS.items():
        if name in CUSTOM_LAYOUTS and not CUSTOM_LAYOUTS[name]["auto_build"]:
            continue
        if len(rects) != len(photo_ids):
            continue
        winner: tuple[float, list[str]] | None = None
        for order in permutations(range(len(photo_ids))):
            score = 0.08 if name == previous else 0.0
            for slot_index, source_index in enumerate(order):
                photo = photos[photo_ids[source_index]]
                width, height = photo.get("width") or 3, photo.get("height") or 2
                photo_ratio = width / height
                _, _, frame_width, frame_height = rects[slot_index]
                frame_ratio = spread_ratio * frame_width / frame_height
                # The log ratio treats portrait and landscape crop losses symmetrically.
                score += abs(math.log(photo_ratio / frame_ratio))
                if ((photo_ratio < 0.85 and frame_ratio > 1.05) or
                        (photo_ratio > 1.15 and frame_ratio < 0.95)):
                    score += 0.25  # Prefer a frame with the same orientation.
                score += 0.04 * abs(source_index - slot_index)
            candidate = (score, [photo_ids[index] for index in order])
            if winner is None or score < winner[0]:
                winner = candidate
        if winner:
            options.append({"layout": name, "photo_ids": winner[1], "score": round(winner[0], 4)})
    if not options:
        raise ValueError("No layout can hold this photo group")
    return sorted(options, key=lambda item: item["score"])


def best_layout(photo_ids: list[str], photos: dict[str, dict], previous: str | None = None,
                design_format: str = "legacy") -> tuple[str, list[str]]:
    choice = ranked_layouts(photo_ids, photos, previous, design_format)[0]
    return choice["layout"], choice["photo_ids"]


def build_draft(photo_ids: list[str], photos: dict[str, dict], design_format: str = "legacy") -> list[dict]:
    if not photo_ids:
        photo_ids = evenly_select(list(photos.values()), 48)
    selected = [photos[value] for value in dict.fromkeys(photo_ids) if value in photos]
    photo_ids = evenly_select(selected, 96)
    if not photo_ids:
        raise ValueError("Import photos before building an album")
    photo_ids.sort(key=lambda value: photo_sort_key(photos[value]))
    pattern = ["hero", "pair", "feature", "pair", "grid"]
    spreads = []
    cursor = 0
    while cursor < len(photo_ids) and len(spreads) < 40:
        desired = pattern[len(spreads) % len(pattern)]
        count = min(len(LAYOUTS[desired]), len(photo_ids) - cursor)
        layout, ordered = best_layout(photo_ids[cursor:cursor + count], photos,
                                      spreads[-1]["layout"] if spreads else None, design_format)
        spreads.append({
            "id": str(uuid4()), "layout": layout,
            "slots": [{"photo_id": value, "focus_x": 0.5, "focus_y": 0.5}
                      for value in ordered],
        })
        cursor += count
    return spreads


def build_directed_draft(photo_ids: list[str], photos: dict[str, dict],
                         target_spreads: int, photos_per_spread: int,
                         design_format: str = "legacy", avoid_similar: bool = True,
                         report: dict | None = None, *, preserve_all: bool = False) -> list[dict]:
    """Build a chronological, chapter-aware draft that always includes selected favorites."""
    if not isinstance(target_spreads, int) or isinstance(target_spreads, bool) or not 1 <= target_spreads <= 40:
        raise ValueError("Choose between 1 and 40 spreads")
    if not isinstance(photos_per_spread, int) or isinstance(photos_per_spread, bool) or not 1 <= photos_per_spread <= 4:
        raise ValueError("Choose between 1 and 4 photos per spread")
    if not isinstance(avoid_similar, bool):
        raise ValueError("Invalid similar-photo setting")
    unique = list(dict.fromkeys(photo_ids))
    if any(photo_id not in photos for photo_id in unique):
        raise ValueError("The selection contains a missing photo")
    selected = [photos[photo_id] for photo_id in unique if not photos[photo_id].get("excluded")]
    if not selected:
        raise ValueError("Select at least one photo that is not excluded")
    groups = []
    skipped_similar = 0
    for chapter in CHAPTERS:
        group = sorted((photo for photo in selected if photo.get("chapter", "Unsorted") == chapter),
                       key=photo_sort_key)
        if avoid_similar:
            group, skipped = skip_similar_burst_photos(group)
            skipped_similar += skipped
        if group:
            groups.append((chapter, group))
    if len(groups) > target_spreads:
        raise ValueError("Use at least one spread per story section, or combine some sections")
    spread_count = min(target_spreads, sum(len(group) for _, group in groups))
    capacity = spread_count * photos_per_spread
    favorites = [photo for photo in selected if photo.get("favorite")]
    if len(favorites) > capacity:
        raise ValueError("Too many favorites for this spread count; increase spreads or photos per spread")
    # First reserve one spread per chapter, then apportion the rest by photo count.
    allocation = [math.ceil(len(group) / photos_per_spread) if preserve_all else 1 for _, group in groups]
    if sum(allocation) > spread_count:
        raise ValueError('Increase spreads to keep every selected photo within its story section')
    for _ in range(spread_count - sum(allocation)):
        candidates = [i for i in range(len(groups)) if allocation[i] < len(groups[i][1])]
        if not candidates:
            break
        index = max(candidates, key=lambda i: len(groups[i][1]) / (allocation[i] + 1))
        allocation[index] += 1
    spreads = []
    for (chapter, group), chapter_spreads in zip(groups, allocation):
        chapter_capacity = chapter_spreads * photos_per_spread
        must_use = [photo for photo in group if photo.get("favorite")]
        if len(must_use) > chapter_capacity:
            raise ValueError(f"Too many favorites in {chapter}; increase its available spreads")
        remaining = [photo for photo in group if not photo.get("favorite")]
        fill = max(0, min(len(group), chapter_capacity) - len(must_use))
        picked = must_use + [photos[photo_id] for photo_id in evenly_select(remaining, fill)] if fill else must_use
        picked.sort(key=photo_sort_key)
        ids = [photo["id"] for photo in picked]
        for index in range(chapter_spreads):
            start = round(index * len(ids) / chapter_spreads)
            end = round((index + 1) * len(ids) / chapter_spreads)
            subset = ids[start:end]
            if not subset:
                continue
            layout, ordered = best_layout(subset, photos, spreads[-1]["layout"] if spreads else None,
                                          design_format)
            spreads.append({"id": str(uuid4()), "chapter": chapter, "layout": layout,
                            "slots": [{"photo_id": value, "focus_x": 0.5, "focus_y": 0.5}
                                      for value in ordered]})
    if report is not None:
        report["similar_skipped"] = skipped_similar
    return spreads


def seed_demo() -> dict:
    existing = load_photos()
    if existing:
        raise ValueError("Demo photos can only be added to an empty photo library")
    chapters = ["Getting ready", "Ceremony", "Portraits", "Celebration"]
    palettes = [
        ((29, 53, 69), (160, 176, 159), (238, 205, 156)),
        ((63, 60, 78), (194, 161, 161), (251, 213, 163)),
        ((44, 77, 65), (153, 187, 163), (242, 210, 164)),
        ((56, 49, 69), (166, 125, 137), (239, 183, 141)),
    ]
    created = []
    for index in range(32):
        chapter = index // 8
        dark, mid, light = palettes[chapter]
        base = Image.new("RGB", (1800, 1200), mid)
        draw = ImageDraw.Draw(base)
        for row in range(1200):
            blend = row / 1199
            color = tuple(round(mid[channel] * (1 - blend) + dark[channel] * blend)
                          for channel in range(3))
            draw.line((0, row, 1800, row), fill=color)
        shift = (index * 107) % 500
        draw.ellipse((1000 - shift, 110, 1530 - shift, 640), fill=light)
        draw.polygon([(0, 850), (310 + shift, 470), (710 + shift, 1200), (0, 1200)], fill=dark)
        draw.polygon([(450, 1200), (1000 - shift // 2, 570), (1800, 930), (1800, 1200)],
                     fill=tuple(min(255, color + 12) for color in dark))
        draw.rounded_rectangle((76, 72, 540, 178), radius=10, fill=(255, 255, 255, 190))
        draw.text((105, 103), f"{chapters[chapter].upper()}  /  {index + 1:02d}", fill=dark)
        buffer = BytesIO()
        base.save(buffer, "JPEG", quality=88, icc_profile=SRGB_PROFILE)
        base.close()
        item = store_photo(buffer.getvalue(), f"{chapter + 1:02d}-{index + 1:02d} {chapters[chapter]}.jpg")
        item["captured_at"] = f"2026:06:14 {10 + chapter * 2:02d}:{index % 8 * 7:02d}:00"
        item["chapter"] = "Reception" if chapter == 3 else chapters[chapter]
        write_json(id_path(PHOTO_META, item["id"]), item)
        created.append(item)
    album = create_project("Sample wedding album", [p["id"] for p in created],
                           spreads=build_draft([p["id"] for p in created], {p["id"]: p for p in created}, "legacy"))
    return album


def job_path(job_id):
    return id_path(JOB_RECORDS, job_id)


def store_job(job):
    JOB_RECORDS.mkdir(parents=True, exist_ok=True)
    write_json(job_path(job["id"]), job)
    JOBS[job["id"]] = job
    return job


def get_job(job_id):
    job_id = str(UUID(job_id))
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None and job_path(job_id).is_file():
            job = json_file(job_path(job_id))
            JOBS[job_id] = job
        return dict(job) if job else None


def update_job(job_id, **changes):
    with JOBS_LOCK:
        current = get_job(job_id)
        return store_job({**current, **changes, "updated_at": now_iso()})


def list_jobs():
    with JOBS_LOCK:
        for path in JOB_RECORDS.glob("*.json"):
            try:
                get_job(path.stem)
            except (ValueError, OSError):
                continue
        return sorted((dict(job) for job in JOBS.values()), key=lambda job: job.get("created_at", ""), reverse=True)


def recover_jobs():
    """Run off the HTTP thread: checksum large legacy ZIPs without delaying Studio."""
    for job in list_jobs():
        if job.get("status") in ("queued", "working", "cancelling", "checking"):
            update_job(job["id"], status="failed", retryable=True,
                       error="Studio restarted before this task finished. Start it again.")
            (EXPORTS / f"{job['id']}.zip.part").unlink(missing_ok=True)
    for job in list_jobs():
        if job.get("kind") == "export" and job.get("status") == "ready" and not (EXPORTS / f"{job['id']}.zip").is_file():
            update_job(job["id"], status="failed", retryable=True,
                       error="This saved ZIP is missing. Create a fresh export; its original checksum remains recorded.")
    for archive in EXPORTS.glob("*.zip"):
        try:
            job_id = str(UUID(archive.stem))
        except ValueError:
            continue
        previous = get_job(job_id)
        try:
            with JOBS_LOCK:
                store_job({**(previous or {}), "id": job_id, "kind": "export", "status": "checking",
                           "created_at": (previous or {}).get("created_at", datetime.fromtimestamp(archive.stat().st_mtime, timezone.utc).isoformat())})
            info = archive_info(archive, allow_legacy=True)
            if previous and previous.get("sha256") and previous["sha256"] != info["sha256"]:
                raise ValueError("Stored print archive has changed; export it again")
            update_job(job_id, **info, filename=archive.name, status="ready", error=None, recovered=True)
        except Exception as exc:
            update_job(job_id, status="failed", error=str(exc), retryable=True)


def verified_export(job_id):
    job = get_job(job_id)
    filename = EXPORTS / f"{str(UUID(job_id))}.zip"
    if not job or job.get("kind", "export") != "export" or job["status"] != "ready" or not filename.is_file():
        raise FileNotFoundError("Export is not ready")
    stat = filename.stat()
    if not job.get("sha256") or stat.st_size != job.get("size") or stat.st_mtime_ns != job.get("mtime_ns"):
        info = archive_info(filename, allow_legacy=bool(job.get("legacy_unversioned")))
        if job.get("sha256") and job["sha256"] != info["sha256"]:
            update_job(job_id, status="failed", error="Print archive changed; create a fresh export", retryable=True)
            raise ValueError("Print archive changed; create a fresh export")
        job = update_job(job_id, **info)
    return job, filename


def start_export(project_id: str) -> str:
    project = load_project(project_id)
    if project.get('sharing_pending'):
        raise ValueError('Retry the pending review withdrawal before exporting print files.')
    if project.get('design_format') in load_catalogue(DATA / 'catalogue.json')['products']:
        project['catalogue_offer'] = catalogue_offer(project['design_format'],
            load_catalogue(DATA / 'catalogue.json'), allow_disabled=True)
    with JOBS_LOCK:
        active = [job for job in JOBS.values() if job.get("kind") == "export" and job["status"] in ("queued", "working")]
        if len(active) >= 3:
            raise ValueError("Three print exports are already queued. Wait for one to finish.")
        job_id = str(uuid4())
        write_json(JOB_RECORDS / f"{job_id}.snapshot.json", project)
        store_job({"id": job_id, "kind": "export", "status": "queued", "error": None,
                   "project_id": project["id"], "project_title": project["title"],
                   "project_revision": project["revision"], "created_at": now_iso(),
                   "manifest": None, "filename": f"{job_id}.zip"})

    def run():
        partial, target = EXPORTS / f"{job_id}.zip.part", EXPORTS / f"{job_id}.zip"
        with EXPORT_SLOTS:
            try:
                update_job(job_id, status="working")
                photos = load_photos()
                result = verify_sources(project, photos, ORIGINALS, PHOTO_META, PREVIEWS, THUMBS, SRGB_PROFILE)
                withdraw_stale_export_approval(project_id, result['photos'])
                if project.get('status') == 'approved' and project.get('design_format', 'legacy') != 'legacy':
                    record = validate_agreement(project.get('approval_record'))
                    assert_sources(record['source_manifest'], result['photos'])
                    if record['design_sha256'] != design_identity(project):
                        raise ValueError('The approved design or order details changed. Publish a fresh proof.')
                    if (record['design_format'] != project.get('design_format') or
                            record['product'] != project.get('product') or
                            record['companion_quantities'] != project.get('companion_quantities')):
                        raise ValueError('The approved product or quantities changed. Publish a fresh proof.')
                project['source_manifest'] = result['photos']
                # A replacement during rendering cannot alter the captured originals.
                with render_snapshot(project, photos, ORIGINALS, result['photos']) as staged:
                    export_album(project, photos, staged, partial)
                info = archive_info(partial)
                if info["project_revision"] != project["revision"]:
                    raise ValueError("Export revision does not match its saved album snapshot")
                os.replace(partial, target)
                target.chmod(0o400)
                info["mtime_ns"] = target.stat().st_mtime_ns
                update_job(job_id, **info, status="ready", error=None)
            except Exception as exc:
                partial.unlink(missing_ok=True)
                update_job(job_id, status="failed", error=str(exc), retryable=True)
    threading.Thread(target=run, daemon=True).start()
    return job_id


def _import_task(kind, worker, **details):
    job_id = str(uuid4())
    cancel = threading.Event()
    with JOBS_LOCK:
        if sum(job.get("kind", "").startswith("picpeak-") and job["status"] in ("queued", "working", "cancelling")
               for job in JOBS.values()) >= 3:
            raise ValueError("Three PicPeak tasks are already queued. Wait or cancel one first.")
        store_job({"id": job_id, "kind": kind, "status": "queued", "done": 0,
                   "created_at": now_iso(), "error": None, **details})
        JOB_CANCEL[job_id] = cancel
    def progress(count):
        with JOBS_LOCK:
            JOBS[job_id]["done"] = count
            if count == 1 or count % 10 == 0:
                store_job(JOBS[job_id])
    slots = IMPORT_SLOTS
    def run():
        acquired = False
        try:
            while not cancel.is_set():
                acquired = slots.acquire(timeout=.2)
                if acquired:
                    break
            if cancel.is_set():
                raise BridgeCancelled("PicPeak task cancelled. You can try again.")
            update_job(job_id, status="working")
            result = worker(cancel, progress)
            with JOBS_LOCK:
                if cancel.is_set():
                    raise BridgeCancelled("PicPeak task cancelled. You can try again.")
                update_job(job_id, status="ready", **result)
        except Exception as exc:
            update_job(job_id, status="cancelled" if cancel.is_set() or isinstance(exc, BridgeCancelled) else "failed",
                       error=str(exc), retryable=True)
        finally:
            if acquired:
                slots.release()
            with JOBS_LOCK:
                JOB_CANCEL.pop(job_id, None)
    threading.Thread(target=run, daemon=True).start()
    return job_id


def cancel_job(job_id):
    with JOBS_LOCK:
        job = get_job(job_id)
        if not job or not job.get("kind", "").startswith("picpeak-"):
            raise FileNotFoundError("PicPeak task not found")
        if job["status"] in ("queued", "working", "cancelling"):
            event = JOB_CANCEL.get(job_id)
            if event:
                event.set()
                job = update_job(job_id, status="cancelling")
        return job


def start_picpeak_import(event_id):
    galleries = picpeak_galleries()
    gallery = next((row for row in galleries if row.get("id") == event_id), None)
    if not gallery:
        raise ValueError("Choose an available PicPeak gallery")
    def worker(cancel, progress):
        imported = import_previews(event_id, PHOTO_META, PREVIEWS, THUMBS, SRGB_PROFILE, progress, cancel=cancel)
        if not imported:
            raise ValueError("No readable photos were found in this gallery")
        photos = load_photos()
        visible = getattr(imported, "visible_ids", None)
        visible = visible if visible is not None else imported
        candidates = sorted((photos[key] for key in visible), key=photo_sort_key)
        highlighted = [p["id"] for p in candidates if p.get("picpeak_favorites") or p.get("picpeak_rating", 0) >= 4]
        shortlist = list(dict.fromkeys(highlighted[:80] + evenly_select(candidates, 180)))[:220]
        return {"done": len(imported), "photo_ids": list(imported), "visible_ids": list(visible),
                "suggested_ids": shortlist, "skipped": getattr(imported, "skipped", 0)}
    return _import_task("picpeak-import", worker, total=gallery["photo_count"], event_id=event_id, gallery_name=gallery["event_name"])


def start_picpeak_originals(project_id):
    project, photos = load_project(project_id), load_photos()
    ids = list(dict.fromkeys([slot["photo_id"] for spread in project["spreads"] for slot in spread["slots"]] +
                            ([project["cover_photo_id"]] if project.get("cover_photo_id") else [])))
    if not ids:
        raise ValueError("Build a draft before fetching print originals")
    def worker(cancel, progress):
        with project_lock(id_path(PROJECTS, project_id)):
            current = load_project(project_id)
            check_project_sources(current, cancel=cancel, progress=progress)
        return {"done": len(ids)}
    return _import_task("picpeak-originals", worker, total=len(ids), project_id=project_id,
                        project_revision=project["revision"])


class Handler(BaseHTTPRequestHandler):
    server_version = "AlbumPrototype/0.1"

    def log_message(self, format: str, *args: object) -> None:
        if urlparse(getattr(self, "path", "")).path.startswith("/review/"):
            print(f"[{now_iso()}] {self.address_string()} {self.command} private preview request", file=sys.stderr)
            return
        print(f"[{now_iso()}] {self.address_string()} {format % args}", file=sys.stderr)

    def _send(self, status: int, body: bytes, content_type: str, cache="no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", cache)
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Vary", "Sec-Fetch-Site")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: object) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= MAX_UPLOAD:
            raise ValueError("Request body is empty or too large")
        return self.rfile.read(length)

    def _json_body(self) -> dict:
        if hasattr(self, "_parsed_body"):
            return self._parsed_body
        value = json.loads(self._body())
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        self._parsed_body = value
        return value

    def _preview_proxy(self, method: str) -> None:
        """Keep phone previews on the authorised HTTPS origin and fixed sidecar."""
        if LOCAL_REVIEW_PORT is None:
            raise ValueError("Client preview is unavailable")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 <= length <= 65536:
            return self._json(413, {"error": "This preview request is too large"})
        body = self.rfile.read(length) if length else None
        headers = {name: self.headers[name] for name in ("Host", "Origin", "Cookie", "Content-Type")
                   if self.headers.get(name) is not None}
        headers["X-Forwarded-Proto"] = urlparse(self.studio_origin).scheme
        parsed = urlparse(self.path)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        connection = HTTPConnection("127.0.0.1", LOCAL_REVIEW_PORT, timeout=30)
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            payload = response.read(32 * 1024 * 1024 + 1)
            if len(payload) > 32 * 1024 * 1024:
                return self._json(502, {"error": "Preview response is too large"})
            self.send_response(response.status)
            allowed = {"content-type", "cache-control", "x-content-type-options", "x-frame-options",
                       "referrer-policy", "content-security-policy", "location", "set-cookie"}
            for name, value in response.getheaders():
                if name.lower() in allowed:
                    if name.lower() == "set-cookie" and self.studio_origin.startswith("https://"):
                        if "; secure" not in value.lower():
                            value += "; Secure"
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        finally:
            connection.close()

    def _route(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        parts = [part for part in path.split("/") if part]
        if path.startswith("/review/") or path in ("/review-app.js", "/review-app.css"):
            return self._preview_proxy(method)
        if method == "GET" and path == "/api/picpeak/galleries":
            return self._json(200, {"galleries": picpeak_galleries()})
        if method == "POST" and path == "/api/picpeak/import":
            event_id = self._json_body().get("event_id")
            if not isinstance(event_id, int) or isinstance(event_id, bool) or event_id <= 0:
                raise ValueError("Choose a PicPeak gallery")
            return self._json(202, {"job_id": start_picpeak_import(event_id)})
        if method == "GET" and len(parts) == 4 and parts[:3] == ["api", "picpeak", "jobs"]:
            with JOBS_LOCK:
                job = get_job(parts[3])
            if not job or not job.get("kind", "").startswith("picpeak-"):
                raise FileNotFoundError("Import job not found")
            return self._json(200, job)
        if method == "POST" and len(parts) == 5 and parts[:3] == ["api", "picpeak", "jobs"] and parts[4] == "cancel":
            return self._json(200, cancel_job(parts[3]))
        if method == "GET" and path == "/api/jobs":
            return self._json(200, {"jobs": list_jobs()})
        if method == "GET" and path == "/api/state":
            catalogue = load_catalogue(DATA / 'catalogue.json')
            return self._json(200, {"photos": list(load_photos().values()),
                                    "projects": list_projects(), "jobs": list_jobs(),
                                    "specs": {key: spec_dict(value) for key, value in SPECS.items()},
                                    "print_profiles": {key: {product: spec_dict(spec) for product, spec in specs.items()}
                                                       for key, specs in PRINT_FORMAT_SPECS.items()},
                                    "design_formats": {key: {**value, **({'label': f"{catalogue_offer(key, catalogue, allow_disabled=True)['product_label']} · {catalogue_offer(key, catalogue, allow_disabled=True)['closed_size']} · €{catalogue_offer(key, catalogue, allow_disabled=True)['base_eur']:g}"} if key in catalogue['products'] else {})}
                                                       for key, value in DESIGN_FORMATS.items()},
                                    "catalogue": catalogue,
                                    "offers": {key: catalogue_offer(key, catalogue, allow_disabled=True) for key in catalogue['products']},
                                    "saal_offers": {key: catalogue_offer(key, catalogue, allow_disabled=True) for key in SAAL_PROFILES},
                                    "layouts": LAYOUTS, "custom_layouts": CUSTOM_LAYOUTS})
        if method == 'PUT' and path == '/api/catalogue':
            return self._json(200, save_catalogue(DATA / 'catalogue.json', self._json_body()))
        if method == 'GET' and len(parts) == 4 and parts[:2] == ['api', 'projects'] and parts[3] == 'crm':
            return self._json(200, crm_handoff.status(load_project(parts[2])))
        if method == 'GET' and len(parts) == 4 and parts[:2] == ['api', 'projects'] and parts[3] == 'approval-record':
            value = crm_handoff.handoff(load_project(parts[2]))
            return self._send(200, json.dumps(value, indent=2, ensure_ascii=False).encode(), 'application/json; charset=utf-8')
        if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "projects"]:
            return self._json(200, load_project(parts[2]))
        if method == "GET" and len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "proof":
            project = load_project(parts[2])
            proof_id = project.get("active_proof_id")
            if proof_id:
                pull_proof(PROOFS, proof_id)
            return self._json(200, {"proof": public_view(load_proof(PROOFS, proof_id)) if proof_id else None})
        if method == "GET" and len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "preflight":
            project = load_project(parts[2])
            if project.get("design_format", "legacy") not in PRINT_FORMAT_SPECS:
                raise ValueError("Choose a supported Print & Bind format")
            return self._json(200, preflight_project(project, load_photos(), ORIGINALS))
        if method == "POST" and path == "/api/projects":
            body = self._json_body()
            source, shortlist = None, []
            if body.get("import_job_id"):
                with JOBS_LOCK:
                    job = get_job(body["import_job_id"]) or {}
                if job.get("kind") != "picpeak-import" or job.get("status") != "ready" or not job.get("photo_ids"):
                    raise ValueError("Complete the PicPeak gallery import before creating an album")
                photos = load_photos()
                eligible = [key for key in job.get("visible_ids", job["photo_ids"])
                            if key in photos and photos[key].get("source_available", True)]
                if not eligible:
                    raise ValueError("No currently visible photos remain in this import. Refresh the gallery.")
                source = {"kind": "picpeak", "event_id": job["event_id"], "photo_ids": eligible}
                shortlist = [key for key in job["suggested_ids"] if key in eligible]
            return self._json(201, create_project(str(body.get("title", "New album")), source=source, shortlist=shortlist))
        if method == "POST" and path == "/api/layouts":
            return self._json(201, create_custom_layout(self._json_body()))
        if method == "DELETE" and len(parts) == 3 and parts[:2] == ["api", "layouts"]:
            delete_custom_layout(parts[2])
            return self._json(200, {"deleted": parts[2]})
        if method == "POST" and path == "/api/layout-suggestion":
            body = self._json_body()
            ids = body.get("photo_ids")
            photos = load_photos()
            if not isinstance(ids, list) or not 1 <= len(ids) <= 4 or any(photo_id not in photos for photo_id in ids):
                raise ValueError("Choose one to four available photos")
            layout, ordered = best_layout(ids, photos, design_format=body.get("design_format", "legacy"))
            return self._json(200, {"layout": layout, "photo_ids": ordered})
        if method == "POST" and path == "/api/layout-suggestions":
            body = self._json_body()
            ids = body.get("photo_ids")
            photos = load_photos()
            if not isinstance(ids, list) or not 1 <= len(ids) <= 4 or any(photo_id not in photos for photo_id in ids):
                raise ValueError("Choose one to four available photos")
            return self._json(200, {"options": ranked_layouts(ids, photos,
                                    design_format=body.get("design_format", "legacy"))[:4]})
        if method == "PUT" and len(parts) == 3 and parts[:2] == ["api", "projects"]:
            return self._json(200, save_project(parts[2], self._json_body()))
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "projects"]:
            project = load_project(parts[2])
            if parts[3] in ("autobuild", "proof", "adopt-approval", "apply-client-layout", "refresh-source", "crm-quote"):
                check_revision(project, self._json_body())
            if parts[3] == "refresh-source":
                source = project.get("source")
                if not source:
                    raise ValueError("This album is not linked to one PicPeak gallery")
                manifest = current_manifest(PHOTO_META, source["event_id"])
                if manifest is None:
                    raise ValueError("Refresh the PicPeak gallery previews first")
                current_ids = [key for key, row in manifest["photos"].items()
                               if row.get("readable") and row.get("visibility") == "visible"]
                historical = {slot["photo_id"] for spread in project["spreads"] for slot in spread["slots"]}
                if project.get("cover_photo_id"):
                    historical.add(project["cover_photo_id"])
                project["source"] = {**source, "photo_ids": list(dict.fromkeys(current_ids + sorted(historical)))}
                project["shortlist"] = [key for key in project.get("shortlist", project["photo_ids"]) if key in current_ids]
                project["revision"] += 1
                project["updated_at"] = now_iso()
                project.pop("source_issues", None)
                project.pop("source_current_ids", None)
                write_project(project)
                return self._json(200, load_project(project["id"]))
            if parts[3] == "client-preview":
                studio_origin = self.studio_origin
                if self.headers.get("Origin") != studio_origin:
                    return self._json(403, {"error": "Invalid local preview request"})
                if LOCAL_REVIEW_PORT is None:
                    raise ValueError("Local client preview service is unavailable")
                if project.get("design_format", "legacy") not in COMPARISON_FORMATS:
                    raise ValueError("Choose an EDGE or GoBook format before previewing")
                if not project.get("spreads"):
                    raise ValueError("Build an album before previewing it as a client")
                with LOCAL_PREVIEW_LOCK:
                    pointer = LOCAL_PREVIEW_PROOFS / "active" / f"{project['id']}.txt"
                    previous_id = pointer.read_text(encoding="ascii").strip() if pointer.is_file() else None
                    token = secrets.token_urlsafe(24)
                    password = secrets.token_urlsafe(24)
                    proof, _ = create_proof(LOCAL_PREVIEW_PROOFS, project, load_photos(), PREVIEWS,
                                            THUMBS, password, LAYOUTS, link_media=True,
                                            catalogue=load_catalogue(DATA / 'catalogue.json'))
                    proof["preview_only"] = True
                    proof["preview_token"] = token
                    proof["preview_studio_url"] = f"{studio_origin}/studio"
                    _save(LOCAL_PREVIEW_PROOFS / proof["id"] / "proof.json", proof)
                    pointer.parent.mkdir(exist_ok=True)
                    temp = pointer.with_suffix(".tmp")
                    temp.write_text(proof["id"], encoding="ascii")
                    os.replace(temp, pointer)
                    if previous_id and previous_id != proof["id"]:
                        try:
                            shutil.rmtree(LOCAL_PREVIEW_PROOFS / str(UUID(previous_id)))
                        except (OSError, ValueError):
                            pass
                preview_origin = studio_origin if studio_origin.startswith("https://") else f"http://127.0.0.1:{LOCAL_REVIEW_PORT}"
                return self._json(201, {"url": f"{preview_origin}/review/{proof['id']}/preview-start/{token}"})
            if parts[3] == "fetch-originals":
                return self._json(202, {"job_id": start_picpeak_originals(project["id"])})
            if parts[3] == 'crm-quote':
                body = self._json_body()
                result, _ = check_project_sources(project)
                record = validate_agreement(project.get('approval_record'))
                assert_sources(record['source_manifest'], result['photos'])
                if record['design_sha256'] != design_identity(project):
                    raise ValueError('The approved order changed. Publish a fresh proof before creating a quote.')
                project['crm_quote'] = crm_handoff.create_draft(project, body)
                project_event(project, 'crm_draft_created', quote_id=project['crm_quote']['id'],
                              agreement_sha256=record['sha256'])
                write_project(project)
                return self._json(200, project)
            if parts[3] == "adopt-approval":
                proof_id = project.get("active_proof_id")
                if not proof_id:
                    raise ValueError("No active client proof")
                pull_proof(PROOFS, proof_id)
                proof = load_proof(PROOFS, proof_id)
                result, _ = check_project_sources(project)
                assert_sources(proof.get('source_manifest'), result['photos'])
                if proof.get('order_details', {}) != project.get('order_details', {}):
                    raise ValueError('Order choices changed. Publish a fresh proof before approval.')
                if (not is_open(proof) or proof.get("project_id") != project["id"] or
                        not proof.get("approved") or proof.get("feedback") or
                        proof["spreads"] != project["spreads"] or proof["title"] != project["title"] or
                        (proof.get("cover_photo_id") and proof["cover_photo_id"] !=
                         (project.get("cover_photo_id") or project["spreads"][0]["slots"][0]["photo_id"])) or
                        proof.get("design_format", "legacy") != project.get("design_format", "legacy")):
                    raise ValueError("The client has not approved this exact album version")
                if is_saal(project.get("design_format")) and cover_crop(proof.get("cover_crop")) != cover_crop(project.get("cover_crop")):
                    raise ValueError("The client has not approved this exact cover crop")
                approved_format = proof["approved"].get("design_format", project.get("design_format", "legacy"))
                if proof["approved"]["product"] not in PRINT_FORMAT_SPECS.get(approved_format, {}):
                    raise ValueError("The approved book does not match this print format")
                approved_project = {**project, "design_format": approved_format}
                checks = preflight_project(approved_project, load_photos(), ORIGINALS)
                if checks["errors"]:
                    raise ValueError("Resolve print-check blockers before preparing the order: " + "; ".join(checks["errors"][:3]))
                project["product"] = proof["approved"]["product"]
                project["design_format"] = approved_format
                project["companion_option"] = proof["approved"].get("companion_option")
                project["companion_quantities"] = companion_counts(proof["approved"].get("companion_quantities"),
                                                                   proof["approved"].get("companion_option"))
                project["status"] = "approved"
                project["approved_at"] = proof["approved"]["at"]
                project["approved_proof_id"] = proof_id
                project['approval_record'] = validate_agreement(proof['approved'].get('agreement'))
                project['order_details'] = project['approval_record']['order_details']
                project_event(project, 'approval_adopted', proof_id=proof_id,
                              agreement_sha256=project['approval_record']['sha256'])
                project["revision"] += 1
                project["updated_at"] = now_iso()
                write_project(project)
                return self._json(200, project)
            if parts[3] == "apply-client-layout":
                return self._json(200, apply_client_layout(project["id"]))
            if parts[3] == "autobuild":
                body = self._json_body()
                photos = load_photos()
                selected = body.get("photo_ids")
                validate_photo_selection(selected, photos, project.get("source"), allow_empty=False)
                if any(photos[key].get("source_available") is False for key in selected):
                    raise ValueError("Refresh the gallery and select photos currently visible in PicPeak")
                method = body.get('selection_method')
                if method not in (None, 'timestamps'):
                    raise ValueError('Choose a supported draft selection method')
                timestamp_report = None
                if method == 'timestamps':
                    target_photos = body.get('target_photos', 80)
                    if type(target_photos) is not int or not 50 <= target_photos <= 100:
                        raise ValueError('Choose 50 to 100 photos for the timestamp draft')
                    selected, timestamp_report = select_by_time([photos[key] for key in selected], target_photos)
                    if len(selected) < minimum_spreads(project.get('design_format')):
                        raise ValueError(f"This format needs at least {minimum_spreads(project.get('design_format'))} available photos for a timestamp draft")
                    spread_count = max(minimum_spreads(project.get('design_format')),
                        sum(math.ceil(sum(photos[key].get('chapter', 'Unsorted') == chapter for key in selected) / 3)
                            for chapter in CHAPTERS))
                    spreads = build_directed_draft(selected, photos, spread_count, 3,
                        project.get('design_format', 'legacy'), avoid_similar=False, preserve_all=True)
                    if {slot['photo_id'] for spread in spreads for slot in spread['slots']} != set(selected):
                        raise ValueError('Review unsupported story sections before creating the timestamp draft')
                    settings = {'selection_method': 'timestamps', 'target_photos': target_photos,
                                'target_spreads': len(spreads), 'photos_per_spread': 3,
                                'avoid_similar': False, 'similar_skipped': 0}
                elif body.get("directed"):
                    avoid_similar = body.get("avoid_similar", True)
                    if not isinstance(avoid_similar, bool):
                        raise ValueError("Invalid similar-photo setting")
                    if avoid_similar:
                        ensure_visual_hashes(photos, selected)
                    report = {}
                    spreads = build_directed_draft(
                        selected, photos, body.get("target_spreads", 20), body.get("photos_per_spread", 3),
                        project.get("design_format", "legacy"), avoid_similar, report)
                    settings = {"target_spreads": body.get("target_spreads", 20),
                                                 "photos_per_spread": body.get("photos_per_spread", 3),
                                                 "avoid_similar": avoid_similar,
                                                 "similar_skipped": report["similar_skipped"]}
                else:
                    spreads = build_draft(selected, photos, project.get("design_format", "legacy"))
                    settings = project.get('draft_settings', {})
                # Finish selection/layout validation before withdrawing an existing
                # proof. A failed remote withdrawal retains the old layout and
                # visible pending recovery; historical approvals remain archived.
                problems = validate_project({**project, 'spreads': spreads}, photos)
                if problems:
                    raise ValueError(' '.join(problems))
                withdraw_project(project, 'A rebuilt draft needs photographer review and fresh approval')
                project['spreads'] = spreads
                project['draft_settings'] = settings
                project.pop('timestamp_draft', None)
                if timestamp_report is not None:
                    project['timestamp_draft'] = timestamp_report
                    cover_id = project.get('cover_photo_id')
                    if cover_id and cover_id not in selected and photos.get(cover_id, {}).get('source_available', True):
                        if cover_id in body['photo_ids']:
                            selected = [*selected, cover_id]
                project["photo_ids"] = selected
                project["shortlist"] = selected
                if project.get("cover_photo_id") not in selected:
                    project["cover_photo_id"] = None
                project["status"] = "draft"
                project["product"] = None
                project["revision"] += 1
                project["updated_at"] = now_iso()
                write_project(project)
                return self._json(200, load_project(project["id"]))
            if parts[3] == "export":
                if project.get("design_format", "legacy") not in PRINT_FORMAT_SPECS:
                    raise ValueError("Choose a supported Print & Bind format")
                return self._json(202, {"job_id": start_export(project["id"])})
            if parts[3] == "proof":
                body = self._json_body()
                if not isinstance(body.get('show_in_gallery', False), bool):
                    raise ValueError('Choose whether to publish in the gallery')
                if body.get('show_in_gallery') and not project.get('source'):
                    raise ValueError('Link this album to its PicPeak gallery first')
                if project.get("design_format", "legacy") not in PRINT_FORMAT_SPECS:
                    raise ValueError("Choose a supported format for client proofing")
                check_project_sources(project)
                catalogue = load_catalogue(DATA / 'catalogue.json')
                for design_format in COMPARISON_FORMATS.get(project.get("design_format", "legacy"),
                                                              (project.get("design_format", "legacy"),)):
                    if design_format in catalogue['products'] and not catalogue['products'][design_format]['enabled']:
                        continue
                    checks = preflight_project({**project, "design_format": design_format}, load_photos(), ORIGINALS)
                    if checks["errors"]:
                        raise ValueError("Resolve print-check blockers before publishing either book: " + "; ".join(checks["errors"][:3]))
                body = self._json_body()
                previous_proof = project.get("active_proof_id")
                proof, password = create_proof(PROOFS, project, load_photos(), PREVIEWS, THUMBS,
                                               body.get("password"), LAYOUTS, catalogue=catalogue)
                if body.get('show_in_gallery'):
                    prepare_showcase(PROOFS, proof, project)
                published = publish_proof(PROOFS, proof["id"])
                set_active(PROOFS, project["id"], proof["id"])
                if previous_proof:
                    revoke_proof(PROOFS, previous_proof)
                project["active_proof_id"] = proof["id"]
                project['show_in_gallery'] = bool(body.get('show_in_gallery'))
                if project.get('approval_record'):
                    project.setdefault('approval_history', []).append(project.pop('approval_record'))
                project.update(status='draft', product=None)
                project.pop('approved_proof_id', None)
                project.pop('approved_at', None)
                project['order_details'] = proof.get('order_details', {})
                project_event(project, 'proof_published', proof_id=proof['id'], catalogue_revision=catalogue['revision'])
                project["revision"] += 1
                project["updated_at"] = now_iso()
                write_project(project)
                base = public_base() if published else None
                return self._json(201, {"id": proof["id"], "password": password, "revision": project["revision"],
                                        "expires_at": proof["expires_at"],
                                        "published": bool(base),
                                        "show_in_gallery": bool(body.get('show_in_gallery')),
                                        "url": f"{base}/{proof['id']}" if base else f"http://127.0.0.1:8776/review/{proof['id']}"})
        if method == "POST" and path == "/api/photos":
            name = parse_qs(parsed.query).get("name", ["Photo"])[0]
            return self._json(201, store_photo(self._body(), name))
        if method == "PUT" and len(parts) == 3 and parts[:2] == ["api", "photos"]:
            return self._json(200, update_photo(parts[2], self._json_body()))
        if method == "PUT" and path == "/api/photos-batch":
            body = self._json_body()
            ids = body.get("photo_ids")
            chapter = body.get("chapter")
            if (not isinstance(ids, list) or not 1 <= len(ids) <= 500 or
                    any(not isinstance(value, str) for value in ids) or chapter not in CHAPTERS):
                raise ValueError("Select up to 500 photos and a valid story section")
            for value in ids:
                if not id_path(PHOTO_META, value).is_file():
                    raise ValueError("The selection contains a missing photo")
            return self._json(200, {"photos": [update_photo(value, {"chapter": chapter}) for value in dict.fromkeys(ids)]})
        if method == "POST" and path == "/api/demo":
            return self._json(201, seed_demo())
        if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "exports"]:
            with JOBS_LOCK:
                job = get_job(parts[2])
            if not job:
                raise FileNotFoundError("Export not found")
            return self._json(200, job)
        if method == "GET" and len(parts) == 2 and parts[0] == "download":
            job_id = str(UUID(parts[1]))
            job, filename = verified_export(job_id)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="album-revision-{job.get("project_revision") or "unknown"}-{job_id[:8]}.zip"')
            self.send_header("Content-Length", str(filename.stat().st_size))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Content-SHA256", job["sha256"])
            self.end_headers()
            with filename.open("rb") as stream:
                shutil.copyfileobj(stream, self.wfile, length=1024 * 1024)
            return
        if method == "GET" and len(parts) == 3 and parts[0] == "media":
            if parts[1] == "original":
                photo_id = str(UUID(parts[2]))
                metadata_path = id_path(PHOTO_META, photo_id)
                if not metadata_path.is_file():
                    raise FileNotFoundError("Original photo not found")
                photo = json_file(metadata_path)
                filename = ORIGINALS / photo["stored_name"]
                if (filename.resolve().parent != ORIGINALS.resolve() or
                        filename.suffix not in (".jpg", ".png") or not filename.is_file()):
                    raise FileNotFoundError("Original photo not found")
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg" if filename.suffix == ".jpg" else "image/png")
                self.send_header("Content-Length", str(filename.stat().st_size))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "private, no-store")
                self.end_headers()
                with filename.open("rb") as stream:
                    shutil.copyfileobj(stream, self.wfile, length=1024 * 1024)
                return
            photo_id = str(UUID(parts[2].removesuffix(".jpg")))
            folder = PREVIEWS if parts[1] == "preview" else THUMBS if parts[1] == "thumb" else None
            if folder is None:
                raise FileNotFoundError("Media not found")
            filename = folder / f"{photo_id}.jpg"
            if not filename.is_file():
                raise FileNotFoundError("Media not found")
            requested = parse_qs(parsed.query).get("v", [None])[0]
            if requested and requested != media_version(filename):
                raise FileNotFoundError("This photo preview has changed. Refresh the gallery.")
            return self._send(200, filename.read_bytes(), "image/jpeg",
                              "private, max-age=31536000, immutable" if requested else "private, no-store")
        if method == "GET":
            if path in ("/", "/studio", "/proof"):
                filename = STATIC / "index.html"
            elif path.startswith("/static/") and re.fullmatch(r"/[a-zA-Z0-9_./-]+", path):
                filename = STATIC / path[len("/static/"):]
                if STATIC.resolve() not in filename.resolve().parents:
                    raise FileNotFoundError("Static file not found")
            else:
                raise FileNotFoundError("Page not found")
            if not filename.is_file():
                raise FileNotFoundError("Page not found")
            content_type = {".html": "text/html", ".js": "text/javascript",
                            ".css": "text/css"}.get(filename.suffix, "application/octet-stream")
            return self._send(200, filename.read_bytes(), content_type + "; charset=utf-8")
        raise FileNotFoundError("Route not found")

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        try:
            self.studio_origin = request_origin(self.headers, self.client_address[0],
                                                self.server.server_address[1], PRIVATE_ACCESS)
            if self.studio_origin is None:
                return self._json(403, {"error": "Use the local Studio address or its authorised Tailscale HTTPS address"})
            # The synthetic pilot is optional and cannot capture Studio routes.
            import pilot_preview
            if os.environ.get("ALBUM_PRINT_PILOT_PREVIEW") == "true" and pilot_preview.matches(self.path):
                if method != "GET" and self.headers.get("Origin") != self.studio_origin:
                    return self._json(403, {"error": "Invalid private pilot request origin"})
                return pilot_preview.forward(self, method)
            if method in ("POST", "PUT", "DELETE"):
                if self.headers.get("Origin") != self.studio_origin:
                    return self._json(403, {"error": "Invalid Studio request origin"})
                mime = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
                upload = method == "POST" and urlparse(self.path).path == "/api/photos"
                if mime not in (("image/jpeg", "image/png") if upload else ("application/json",)):
                    return self._json(415, {"error": "Use JPEG or PNG for photos, and JSON for other Studio requests"})
            parts = unquote(urlparse(self.path).path).strip("/").split("/")
            if method in ("POST", "PUT") and len(parts) >= 3 and parts[:2] == ["api", "projects"]:
                with project_lock(id_path(PROJECTS, parts[2])):
                    self._route(method)
            else:
                self._route(method)
        except ProjectConflict as exc:
            self._json(409, {"error": str(exc)})
        except FileNotFoundError as exc:
            self._json(404, {"error": str(exc)})
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self.log_error("Unhandled error: %s", exc)
            self._json(500, {"error": "Unexpected server error"})


if __name__ == "__main__":
    import proof_server

    port = int(os.environ.get("ALBUM_PROTOTYPE_PORT", "8775"))
    threading.Thread(target=recover_jobs, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    proof_server.PROOFS = LOCAL_PREVIEW_PROOFS
    proof_server.REQUIRE_ACTIVE = True
    proof_server.LOCAL_PREVIEW = True
    local_review = ThreadingHTTPServer(("127.0.0.1", 0), proof_server.Handler)
    LOCAL_REVIEW_PORT = local_review.server_address[1]
    threading.Thread(target=local_review.serve_forever, daemon=True).start()
    print(f"Album prototype: http://127.0.0.1:{port}", flush=True)
    print(f"Local client preview: http://127.0.0.1:{LOCAL_REVIEW_PORT}", flush=True)
    try:
        server.serve_forever()
    finally:
        local_review.shutdown()
