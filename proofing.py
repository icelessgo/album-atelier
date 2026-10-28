"""Isolated, password-protected review snapshots. No studio or original-photo routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import scrypt
import base64
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
from uuid import UUID, uuid4

from project_store import project_lock, write_json
from product_catalogue import default_offers, offer_for, order_details
from approval_record import make_agreement

from payments import PaymentError, checkout_session, configured as stripe_configured
from saal_profiles import SAAL_PROFILES, is_saal, minimum_spreads, saal_offer, cover_crop


CLIENT_LAYOUT_SLOTS = {"hero": 1, "pair": 2, "feature": 3, "grid": 4}
REVIEW_FORMATS = default_offers()
COMPARISON_FORMATS = {
    "gobook_portrait_25x35": ("gobook_portrait_25x35", "portrait_30x40"),
    "portrait_30x40": ("gobook_portrait_25x35", "portrait_30x40"),
    "gobook_landscape_35x25": ("gobook_landscape_35x25", "landscape_40x30"),
    "landscape_40x30": ("gobook_landscape_35x25", "landscape_40x30"),
}

for _format in SAAL_PROFILES:
    REVIEW_FORMATS[_format] = saal_offer(_format)
    COMPARISON_FORMATS[_format] = (_format,)


def same_price_shape(first, second):
    """Shapes must keep the same book, currency and price, including extra copies."""
    def amount(offer):
        return offer.get('base_minor', offer.get('amount_minor'))
    return (first.get('product') == second.get('product') and
            first.get('currency') == second.get('currency') == 'EUR' and
            type(amount(first)) is int and amount(first) == amount(second) and
            first.get('companion_eur', 0) == second.get('companion_eur', 0))


def shape_offers(offers, catalogue, details):
    """Freeze compatible shape alternatives at publication; keep existing offers intact."""
    result = [dict(offer) for offer in offers]
    for key in REVIEW_FORMATS:
        if any(item['design_format'] == key for item in result):
            continue
        if catalogue and not catalogue['products'][key]['enabled']:
            continue
        candidate = offer_for(key, catalogue)
        if not any(same_price_shape(candidate, offer) for offer in offers):
            continue
        try:
            candidate['order_details'] = order_details(candidate, details)
        except ValueError:
            continue
        result.append(candidate)
    return result


def companion_counts(value: dict | None = None, legacy: str | None = None) -> dict[str, int]:
    """Counts are actual books; the smaller edition is sold in pairs."""
    if value is None:
        value = {"one_mid": int(legacy == "one_mid"), "two_small": 2 * int(legacy == "two_small")}
    if not isinstance(value, dict) or set(value) - {"one_mid", "two_small"}:
        raise ValueError("Choose valid companion-book quantities")
    counts = {key: value.get(key, 0) for key in ("one_mid", "two_small")}
    if any(type(count) is not int or not 0 <= count <= 1000 for count in counts.values()):
        raise ValueError("Companion quantities must be whole numbers from 0 to 1000")
    if counts["two_small"] % 2:
        raise ValueError("Smaller companion books must be ordered in pairs")
    return counts


def companion_total(counts: dict[str, int], offer: dict) -> int:
    if is_saal(offer.get("design_format")) and any(counts.values()):
        raise ValueError("Companion copies are not offered for this Saal album")
    return offer["companion_eur"] * (counts["one_mid"] + counts["two_small"] // 2)


def _save(path: Path, value: dict) -> None:
    write_json(path, value)


def _proof_path(root: Path, proof_id: str) -> Path:
    return root / str(UUID(proof_id))


def load_proof(root: Path, proof_id: str) -> dict:
    path = _proof_path(root, proof_id) / "proof.json"
    if not path.is_file():
        raise FileNotFoundError("Review not found")
    return json.loads(path.read_text(encoding="utf-8"))


def create_proof(root: Path, project: dict, photos: dict[str, dict],
                 previews: Path, thumbs: Path, password: str | None = None,
                 layouts: dict | None = None, *, link_media: bool = False, catalogue=None) -> tuple[dict, str]:
    design_format = project.get("design_format", "legacy")
    if is_saal(design_format) and not 13 <= len(project.get("spreads", [])) <= 40:
        raise ValueError("A Saal album needs 13 to 40 spreads / 26 to 80 pages")
    if design_format != "legacy" and design_format not in REVIEW_FORMATS:
        raise ValueError("Choose a supported print format for client proofing")
    if design_format in REVIEW_FORMATS and project.get("companion_option") not in (None, *REVIEW_FORMATS[design_format]["companions"]):
        raise ValueError("Remove the earlier companion choice before publishing this book")
    if not project.get("spreads"):
        raise ValueError("Build an album before preparing a review")
    if is_saal(design_format):
        companion_total(companion_counts(project.get("companion_quantities"), project.get("companion_option")),
                        saal_offer(design_format))
    chosen_cover = project.get("cover_photo_id")
    cover_photo_id = chosen_cover or project["spreads"][0]["slots"][0]["photo_id"]
    if chosen_cover and cover_photo_id not in project.get("photo_ids", []):
        raise ValueError("Cover photo must be in this album")
    selected_offer, offers = None, None
    if design_format in REVIEW_FORMATS:
        selected_offer = offer_for(design_format, catalogue)
        selected_details = order_details(selected_offer, project.get('order_details'))
        offers = []
        for key in COMPARISON_FORMATS.get(design_format, (design_format,)):
            if catalogue and not catalogue['products'][key]['enabled']:
                continue
            candidate = offer_for(key, catalogue)
            try:
                candidate['order_details'] = order_details(candidate, project.get('order_details'))
            except ValueError:
                # A cover/box offered for one book cannot be sold on another.
                continue
            offers.append(candidate)
        selected_offer['order_details'] = selected_details
    if password is None:
        password = base64.b32encode(secrets.token_bytes(8)).decode().rstrip("=")
    if not isinstance(password, str) or not 10 <= len(password) <= 100:
        raise ValueError("Review password must have 10 to 100 characters")
    proof_id = str(uuid4())
    folder = root / proof_id
    media = folder / "media"
    placed = {slot["photo_id"] for spread in project["spreads"] for slot in spread["slots"]}
    placed.add(cover_photo_id)
    def available(photo_id):
        photo = photos.get(photo_id, {})
        return bool(photo and photo.get("source_available", True) and
                    photo.get("picpeak_current", True) and photo.get("picpeak_visibility", "visible") == "visible")
    blocked = [photo_id for photo_id in placed if not available(photo_id)]
    if blocked:
        raise ValueError("Review photos removed, hidden or unavailable in PicPeak before sharing this album")
    used = placed | {photo_id for photo_id in project.get("photo_ids", []) if available(photo_id)}
    source_events = {photos[photo_id].get("picpeak_event_id") for photo_id in used if photo_id in photos}
    if len(source_events) == 1 and None not in source_events:
        event_id = next(iter(source_events))
        manifest = project.get("source", {}).get("photo_ids") if project.get("source") else None
        gallery_ids = {photo_id for photo_id, photo in photos.items()
                       if photo.get("picpeak_event_id") == event_id and available(photo_id) and
                       (manifest is None or photo_id in manifest)}
        if manifest is not None and not used.issubset(gallery_ids):
            raise ValueError("Album photos must belong to its imported PicPeak gallery")
        if len(gallery_ids) > 1500:
            raise ValueError("This gallery has more than 1500 photos; contact the photographer")
        used.update(gallery_ids)
    else:
        event_id = None
    media.mkdir(parents=True, mode=0o700)
    folder.chmod(0o700)
    media.chmod(0o700)
    proof_photos = []
    try:
        for photo_id in sorted(used, key=lambda item: (photos.get(item, {}).get("captured_at") or "",
                                                        photos.get(item, {}).get("name") or "")):
            if photo_id not in photos:
                raise ValueError("The album contains a missing photo")
            UUID(photo_id)
            preview = previews / f"{photo_id}.jpg"
            thumb = thumbs / f"{photo_id}.jpg"
            if not preview.is_file() or not thumb.is_file():
                raise ValueError("A review preview is missing")
            for source_file, destination in ((preview, media / f"{photo_id}.jpg"),
                                             (thumb, media / f"{photo_id}-thumb.jpg")):
                if link_media:
                    try:
                        os.link(source_file, destination)
                    except OSError:
                        shutil.copyfile(source_file, destination)
                        destination.chmod(0o600)
                else:
                    shutil.copyfile(source_file, destination)
                    destination.chmod(0o600)
            source = photos[photo_id]
            proof_photos.append({"id": photo_id, "name": source["name"],
                                 "width": source["width"], "height": source["height"],
                                 "chapter": source.get("chapter") or "Unsorted"})
        salt = secrets.token_bytes(16)
        digest = scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
        proof = {
            "id": proof_id, "project_id": project["id"], "project_revision": project["revision"],
            "title": project["title"], "spreads": project["spreads"], "photos": proof_photos,
            "photographer_spreads": project["spreads"], "layout_revision": 0,
            "client_edits_pending": False,
            "gallery_photo_count": len(proof_photos) if event_id is not None else 0,
            "cover_photo_id": cover_photo_id,
            "cover_crop": cover_crop(project.get("cover_crop")),
            "design_format": design_format,
            "photographer_design_format": design_format,
            "companion_option": project.get("companion_option") if design_format in REVIEW_FORMATS else None,
            "companion_quantities": companion_counts(project.get("companion_quantities"),
                                                     project.get("companion_option")) if design_format in REVIEW_FORMATS else {"one_mid": 0, "two_small": 0},
            "offer": selected_offer,
            "offers": offers,
            "shape_offers": shape_offers(offers, catalogue, project.get('order_details')) if offers else [],
            "layouts": {spread["layout"]: layouts[spread["layout"]]
                        for spread in project["spreads"]} if layouts else {},
            "product": project.get("product"), "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
            "status": "open", "feedback": [], "approved": None,
            "order_details": selected_details if selected_offer else project.get("order_details", {}),
            "source_manifest": project.get("source_manifest", {}),
            "source_galleries": project.get("source_galleries", []),
            "history": [{"event": "published", "actor": "photographer", "at": datetime.now(timezone.utc).isoformat(), "project_revision": project["revision"]}],
            "password_salt": salt.hex(), "password_digest": digest.hex(),
            "session_secret": secrets.token_hex(32),
        }
        for gallery in proof["source_galleries"]:
            if gallery.get("expires_at"):
                expiry = datetime.fromisoformat(gallery["expires_at"].replace("Z", "+00:00"))
                if expiry < datetime.fromisoformat(proof["expires_at"]):
                    proof["expires_at"] = expiry.isoformat()
        _save(folder / "proof.json", proof)
        return proof, password
    except Exception:
        shutil.rmtree(folder)
        raise


def revoke_proof(root: Path, proof_id: str) -> None:
    if not _proof_path(root, proof_id).is_dir():
        return
    with project_lock(_proof_path(root, proof_id) / "proof.json"):
        try:
            proof = load_proof(root, proof_id)
        except (FileNotFoundError, ValueError):
            return
        if proof["status"] == "replaced":
            return
        proof["status"] = "replaced"
        history_event(proof, "withdrawn", "photographer")
        _save(_proof_path(root, proof_id) / "proof.json", proof)


def history_event(proof, event, actor, **details):
    proof.setdefault("history", []).append({"event": event, "actor": actor,
        "at": datetime.now(timezone.utc).isoformat(), **details})


def verify_password(proof: dict, password: str) -> bool:
    if len(password) > 100:
        return False
    digest = scrypt(password.encode(), salt=bytes.fromhex(proof["password_salt"]),
                    n=2**14, r=8, p=1)
    return hmac.compare_digest(digest, bytes.fromhex(proof["password_digest"]))


def is_open(proof: dict) -> bool:
    return proof["status"] == "open" and datetime.fromisoformat(proof["expires_at"]) > datetime.now(timezone.utc)


def session_cookie(proof: dict) -> str:
    expiry = int((datetime.now(timezone.utc) + timedelta(hours=12)).timestamp())
    nonce = secrets.token_hex(16)
    payload = f"{proof['id']}.{expiry}.{nonce}"
    signature = hmac.new(bytes.fromhex(proof["session_secret"]), payload.encode(), "sha256").hexdigest()
    return f"{payload}.{signature}"


def verify_session(proof: dict, value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4 or parts[0] != proof["id"]:
        return False
    try:
        expiry = int(parts[1])
    except ValueError:
        return False
    if expiry < int(datetime.now(timezone.utc).timestamp()) or expiry > int((datetime.now(timezone.utc) + timedelta(hours=12)).timestamp()) + 60:
        return False
    payload = ".".join(parts[:3])
    signature = hmac.new(bytes.fromhex(proof["session_secret"]), payload.encode(), "sha256").hexdigest()
    return hmac.compare_digest(signature, parts[3])


def consume_preview_start(root: Path, proof_id: str, token: str) -> dict | None:
    """Consume a one-use, loopback-only Studio preview opening token."""
    with project_lock(_proof_path(root, proof_id) / "proof.json"):
        proof = load_proof(root, proof_id)
        if (not proof.get("preview_only") or proof.get("preview_started") or
                not isinstance(token, str) or not token or len(token) > 100 or
                not proof.get("preview_token") or
                not hmac.compare_digest(token, proof.get("preview_token", ""))):
            return None
        proof.pop("preview_token", None)
        proof["preview_started"] = True
        _save(_proof_path(root, proof_id) / "proof.json", proof)
        return proof


def add_feedback(root: Path, proof_id: str, kind: str, spread_id: str, message: str,
                 photo_id: str | None = None) -> dict:
    with project_lock(_proof_path(root, proof_id) / "proof.json"):
        proof = load_proof(root, proof_id)
        if not is_open(proof):
            raise ValueError("This review is no longer open")
        if proof.get("approved"):
            raise ValueError("This album has been approved; contact your photographer for revisions")
        if kind not in ("comment", "swap", "add_pages"):
            raise ValueError("Choose a valid request type")
        if not isinstance(spread_id, str) or spread_id not in {spread["id"] for spread in proof["spreads"]}:
            raise ValueError("Choose a spread in this album")
        if photo_id is not None and (not isinstance(photo_id, str) or photo_id not in {photo["id"] for photo in proof["photos"]}):
            raise ValueError("Choose a photo in this album")
        message = str(message).strip()
        if not 1 <= len(message) <= 1000:
            raise ValueError("Write a note of up to 1000 characters")
        entry = {"id": str(uuid4()), "kind": kind, "spread_id": spread_id,
                 "photo_id": photo_id, "message": message,
                 "created_at": datetime.now(timezone.utc).isoformat()}
        proof["feedback"].append(entry)
        history_event(proof, 'feedback_added', 'client', feedback_id=entry['id'], spread_id=spread_id)
        proof["approved"] = None
        _save(_proof_path(root, proof_id) / "proof.json", proof)
        return entry


def update_client_layout(root: Path, proof_id: str, revision: int, spreads: list,
                         cover_photo_id: str | None = None, *,
                         selected_format: str | None = None,
                         from_format: str | None = None) -> dict:
    """Save an isolated client draft; the photographer must apply and republish it."""
    with project_lock(_proof_path(root, proof_id) / "proof.json"):
        proof = load_proof(root, proof_id)
        if not is_open(proof) or proof.get("approved") or (proof.get("payment") or {}).get("status") in ("pending", "paid"):
            raise ValueError("This album can no longer be edited")
        if type(revision) is not int or revision != proof.get("layout_revision", 0):
            raise ValueError("This album changed in another tab. Reload before editing")
        selected_format = selected_format or proof.get('design_format', 'legacy')
        shape_changed = selected_format != proof.get('design_format', 'legacy')
        chosen = None
        if shape_changed:
            source = next((item for item in proof.get('offers') or []
                           if item['design_format'] == from_format), None)
            chosen = next((item for item in proof.get('shape_offers') or []
                           if item['design_format'] == selected_format), None)
            if not source or not chosen or not same_price_shape(source, chosen):
                raise ValueError('Choose an available shape for the same book and price')
        minimum = minimum_spreads(selected_format)
        if not isinstance(spreads, list) or not minimum <= len(spreads) <= 40:
            raise ValueError(f"An album needs {minimum} to 40 spreads")
        allowed_photos = {photo["id"] for photo in proof["photos"]}
        if cover_photo_id is None:
            cover_photo_id = proof.get("cover_photo_id")
        if not isinstance(cover_photo_id, str) or cover_photo_id not in allowed_photos:
            raise ValueError("Choose a cover photo from this gallery")
        try:
            if str(UUID(cover_photo_id)) != cover_photo_id:
                raise ValueError("Invalid cover photo ID")
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("Invalid cover photo ID") from exc
        known_layouts = {**CLIENT_LAYOUT_SLOTS,
                         **{name: len(rects) for name, rects in (proof.get("layouts") or {}).items()}}
        seen = set()
        clean = []
        for spread in spreads:
            if not isinstance(spread, dict):
                raise ValueError("Invalid album spread")
            try:
                spread_id = str(UUID(spread.get("id", "")))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError("Invalid spread ID") from exc
            if spread_id in seen:
                raise ValueError("A spread cannot appear twice")
            seen.add(spread_id)
            layout = spread.get("layout")
            slots = spread.get("slots")
            if not isinstance(layout, str) or layout not in known_layouts or not isinstance(slots, list) or len(slots) != known_layouts[layout]:
                raise ValueError("Choose a supported spread layout")
            safe_slots = []
            for slot in slots:
                if not isinstance(slot, dict) or not isinstance(slot.get("photo_id"), str) or slot["photo_id"] not in allowed_photos:
                    raise ValueError("Choose a photo from this gallery")
                focus = {}
                for axis in ("focus_x", "focus_y"):
                    value = slot.get(axis, 0.5)
                    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                        raise ValueError("Invalid image placement")
                    focus[axis] = round(float(value), 4)
                safe_slots.append({"photo_id": slot["photo_id"], **focus})
            chapter = spread.get("chapter", "")
            if not isinstance(chapter, str) or len(chapter) > 80:
                raise ValueError("Invalid chapter")
            clean.append({"id": spread_id, "layout": layout, "slots": safe_slots, "chapter": chapter})
        if clean != proof["spreads"] or cover_photo_id != proof.get("cover_photo_id") or shape_changed:
            if shape_changed:
                proof.setdefault('photographer_design_format', proof['design_format'])
                before_format = proof['design_format']
                proof.update(design_format=selected_format, offer=chosen,
                             order_details=chosen.get('order_details', proof.get('order_details', {})))
                proof['offers'] = [item for item in proof['shape_offers']
                                   if item['design_format'] in COMPARISON_FORMATS[selected_format]]
                history_event(proof, 'shape_proposed', 'client', from_format=before_format,
                              design_format=selected_format)
            proof["spreads"] = clean
            if cover_photo_id != proof.get("cover_photo_id") and is_saal(proof.get("design_format")):
                proof["cover_crop"] = cover_crop()
            proof["cover_photo_id"] = cover_photo_id
            proof["layout_revision"] = revision + 1
            proof["client_edits_pending"] = True
            proof["client_edited_at"] = datetime.now(timezone.utc).isoformat()
            history_event(proof, "layout_proposed", "client", layout_revision=proof["layout_revision"])
            _save(_proof_path(root, proof_id) / "proof.json", proof)
        return public_view(proof)


def approve_proof(root: Path, proof_id: str, product: str,
                  companion_option: str | None = None,
                  selected_format: str | None = None,
                  companion_quantities: dict | None = None) -> dict:
    with project_lock(_proof_path(root, proof_id) / "proof.json"):
        proof = load_proof(root, proof_id)
        if not is_open(proof):
            raise ValueError("This review is no longer open")
        design_format = proof.get("design_format", "legacy")
        selected_format = selected_format or design_format
        if design_format in REVIEW_FORMATS:
            offer = next((item for item in (proof.get("offers") or [proof.get("offer") or offer_for(design_format)])
                          if item.get("design_format", design_format) == selected_format), None)
            if not offer or product != offer["product"] or companion_option not in (None, *offer["companions"]):
                raise ValueError("Choose the matching book and companion option")
            counts = companion_counts(companion_quantities, companion_option)
            if companion_quantities is not None and companion_option is not None:
                raise ValueError("Send companion quantities without a legacy option")
        elif product not in ("gobook", "wedding_book") or companion_option is not None:
            raise ValueError("Choose an album edition")
        else:
            counts = companion_counts(companion_quantities)
            if any(counts.values()):
                raise ValueError("Companion books are unavailable for this edition")
        if len(proof["spreads"]) < minimum_spreads(design_format):
            raise ValueError("The proof does not have enough printed pages for this edition")
        if proof["feedback"]:
            raise ValueError("Changes were requested. Please wait for the revised proof before approving")
        if proof.get("client_edits_pending"):
            raise ValueError("Your layout changes need photographer review before approval")
        if proof.get("approved"):
            if (proof["approved"]["product"] == product and
                    companion_counts(proof["approved"].get("companion_quantities"), proof["approved"].get("companion_option")) == counts and
                    proof["approved"].get("design_format", design_format) == selected_format):
                return proof["approved"]
            raise ValueError("This album is already approved for a different edition")
        legacy_option = ("one_mid" if counts == {"one_mid": 1, "two_small": 0} else
                         "two_small" if counts == {"one_mid": 0, "two_small": 2} else None)
        proof["approved"] = {"product": product, "at": datetime.now(timezone.utc).isoformat(),
                             "feedback_count": len(proof["feedback"]),
                             "design_format": selected_format or design_format,
                             "companion_option": legacy_option, "companion_quantities": counts,
                             "total_eur": (offer["base_eur"] +
                                           companion_total(counts, offer)) if design_format in REVIEW_FORMATS else (300 if product == "gobook" else 500)}
        proof["approved"]["agreement"] = make_agreement(proof, proof["approved"], offer if design_format in REVIEW_FORMATS else {"product_label": product, "closed_size": "Legacy format"})
        history_event(proof, "approved", "client", agreement_sha256=proof["approved"]["agreement"]["sha256"])
        _save(_proof_path(root, proof_id) / "proof.json", proof)
        return proof["approved"]


def public_view(proof: dict) -> dict:
    view = {key: proof.get(key) for key in ("id", "title", "spreads", "photos", "cover_photo_id", "cover_crop", "created_at", "gallery_path",
                                           "expires_at", "status", "feedback", "approved", "layouts",
                                           "design_format", "companion_option", "companion_quantities", "offer", "offers",
                                           "shape_offers",
                                           "gallery_photo_count", "layout_revision", "client_edits_pending",
                                           "preview_only", "preview_studio_url", "order_details", "history")}
    payment = proof.get("payment") or {}
    view['shape_review_required'] = bool(proof.get('client_edits_pending') and
        proof.get('design_format') != proof.get('photographer_design_format', proof.get('design_format')))
    view["payment"] = {key: payment.get(key) for key in ("status", "amount_eur", "paid_at")}
    view["checkout_available"] = bool(proof.get("offers") and stripe_configured() and
                                      not proof.get("preview_only") and not is_saal(proof.get("design_format")))
    return view


def start_checkout(root: Path, proof_id: str) -> dict:
    if is_saal(load_proof(root, proof_id).get("design_format")):
        raise PaymentError("Your photographer will arrange payment and place this Saal order")
    with project_lock(_proof_path(root, proof_id) / "proof.json"):
        proof = load_proof(root, proof_id)
        if proof.get("preview_only"):
            raise PaymentError("Checkout is unavailable in a local preview")
        if not is_open(proof) or not proof.get("approved") or proof.get("feedback") or proof.get("client_edits_pending"):
            raise PaymentError("Approve an open, unchanged proof before payment")
        previous = proof.get("payment") or {}
        if previous.get("status") == "paid":
            raise PaymentError("This album has already been paid for")
        if previous.get("status") == "pending" and previous.get("expires_at", 0) > int(datetime.now(timezone.utc).timestamp()) + 30:
            return {"url": previous["url"], "id": previous["session_id"]}
        proof["checkout_attempt"] = int(proof.get("checkout_attempt") or 0) + 1
        session = checkout_session(proof)
        proof["payment"] = {"status": "pending", "session_id": session["id"],
                            "url": session["url"], "amount_eur": session["amount_eur"],
                            "expires_at": session["expires_at"], "attempt": proof["checkout_attempt"]}
        _save(_proof_path(root, proof_id) / "proof.json", proof)
        return {"url": session["url"], "id": session["id"]}


def record_stripe_event(root: Path, event: dict) -> bool:
    if event.get("type") not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        return False
    session = event.get("data", {}).get("object", {})
    if session.get("payment_status") != "paid":
        return False
    proof_id = str(UUID(session.get("client_reference_id", "")))
    with project_lock(_proof_path(root, proof_id) / "proof.json"):
        proof = load_proof(root, proof_id)
        payment = proof.get("payment") or {}
        if (session.get("id") != payment.get("session_id") or
                session.get("currency") != "eur" or
                session.get("amount_total") != payment.get("amount_eur", 0) * 100 or
                session.get("metadata", {}).get("proof_id") != proof_id or
                session.get("metadata", {}).get("design_format") != proof["approved"].get("design_format") or
                session.get("metadata", {}).get("project_revision") != str(proof["project_revision"])):
            raise PaymentError("Stripe session does not match the approved album")
        shipping = ((session.get("collected_information") or {}).get("shipping_details") or
                    session.get("shipping_details") or {})
        if (shipping.get("address") or {}).get("country") != "IE":
            raise PaymentError("Stripe checkout does not have an Irish delivery address")
        if payment.get("status") == "paid":
            return False
        payment.pop("url", None)
        payment.update(status="paid", paid_at=datetime.now(timezone.utc).isoformat(),
                       stripe_event_id=event.get("id"), customer_email=(session.get("customer_details") or {}).get("email"),
                       shipping_details=shipping)
        proof["payment"] = payment
        _save(_proof_path(root, proof_id) / "proof.json", proof)
        return True
