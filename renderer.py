"""Graphistudio spread renderer for the local album-design prototype.

The editable project stores photo IDs, layouts and focal points. This module
renders each product from the original files; it never scales an exported JPEG.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import json
import math
import zipfile

from PIL import Image, ImageCms, ImageOps
from saal_profiles import SAAL_PROFILES, is_saal, minimum_spreads


@dataclass(frozen=True)
class PrintSpec:
    label: str
    width_cm: float
    height_cm: float
    dpi: int
    bleed_mm: int
    safe_mm: int

    @property
    def pixels(self) -> tuple[int, int]:
        return (round(self.width_cm / 2.54 * self.dpi),
                round(self.height_cm / 2.54 * self.dpi))


# Graphistudio Info_GoBook_EN.pdf and Print_Bind_EN_Metric.pdf.
SPECS = {
    "gobook": PrintSpec("GoBook · 35×25 cm", 69.7, 25.5, 300, 5, 18),
    "wedding_book": PrintSpec("Original Wedding Book · 45×35 cm", 94.3, 34.5, 250, 6, 25),
}

# GoBook uses its product-specific guide. The user has chosen to use the
# general Print & Bind guide for EDGE interiors because the Trade Area has no
# separate EDGE interior guide. Record that distinction in every hand-off.
GOBOOK_PORTRAIT = PrintSpec("GoBook · 25×35 cm", 50.8, 34.7, 300, 5, 18)
EDGE_PORTRAIT = PrintSpec("EDGE · 30×40 cm", 60, 41, 250, 6, 20)
EDGE_LANDSCAPE = PrintSpec("EDGE · 40×30 cm", 82, 30, 250, 6, 20)
PRINT_FORMAT_SPECS = {
    "legacy": SPECS,
    "gobook_landscape_35x25": {"gobook": SPECS["gobook"]},
    "gobook_portrait_25x35": {"gobook": GOBOOK_PORTRAIT},
    "landscape_40x30": {"edge": EDGE_LANDSCAPE},
    "portrait_30x40": {"edge": EDGE_PORTRAIT},
}

for _key, _profile in SAAL_PROFILES.items():
    _w, _h = _profile['trim_mm']
    PRINT_FORMAT_SPECS[_key] = {'saal': PrintSpec(_profile['label'], _w / 10, _h / 10, 300, 3, 10)}


def print_specs(project: dict) -> dict[str, PrintSpec]:
    design_format = project.get("design_format", "legacy")
    if design_format not in PRINT_FORMAT_SPECS:
        raise ValueError("Choose a supported Print & Bind format")
    return PRINT_FORMAT_SPECS[design_format]


def companion_requests(project: dict) -> list[dict]:
    from proofing import companion_counts
    counts = companion_counts(project.get("companion_quantities"), project.get("companion_option"))
    if is_saal(project.get("design_format")):
        if any(counts.values()):
            raise ValueError("Companion copies are not offered for this Saal album")
        return []
    sizes = {
        "gobook_portrait_25x35": {"one_mid": (1, "20×30 cm"), "two_small": (2, "15×20 cm")},
        "gobook_landscape_35x25": {"one_mid": (1, "30×20 cm"), "two_small": (2, "20×15 cm")},
        "portrait_30x40": {"one_mid": (1, "20×30 cm"), "two_small": (2, "15×20 cm")},
        "landscape_40x30": {"one_mid": (1, "30×20 cm"), "two_small": (2, "20×15 cm")},
    }
    available = sizes.get(project.get("design_format"), {})
    if any(counts.values()) and not available:
        raise ValueError("Choose a companion size supported by this book format")
    return [{"quantity": quantity, "closed_size": available[key][1],
             "note": "Order the smaller copies in Graphistudio; only main-book interior files are included."}
            for key, quantity in counts.items() if quantity]


def companion_request(project: dict) -> dict | None:
    """Compatibility for single-size orders and older hand-off consumers."""
    requests = companion_requests(project)
    return requests[0] if len(requests) == 1 else None

# Normalized rectangles (x, y, width, height) on a complete double-page spread.
LAYOUTS: dict[str, list[tuple[float, float, float, float]]] = {
    "hero": [(0, 0, 1, 1)],
    "portrait_solo": [(0.135, 0.085, 0.23, 0.83)],
    "pair": [(0.025, 0.045, 0.465, 0.91), (0.51, 0.045, 0.465, 0.91)],
    "portrait_pair": [(0.135, 0.085, 0.23, 0.83), (0.635, 0.085, 0.23, 0.83)],
    "portrait_focus": [(0.115, 0.08, 0.25, 0.84), (0.53, 0.20, 0.44, 0.60)],
    "feature": [(0.025, 0.045, 0.56, 0.91), (0.605, 0.045, 0.37, 0.445),
                (0.605, 0.51, 0.37, 0.445)],
    "triptych": [(0.025, 0.045, 0.303, 0.91), (0.348, 0.045, 0.304, 0.91),
                 (0.672, 0.045, 0.303, 0.91)],
    "portrait_trio": [(0.05, 0.085, 0.19, 0.83), (0.27, 0.085, 0.19, 0.83),
                      (0.635, 0.085, 0.23, 0.83)],
    "portrait_story": [(0.12, 0.07, 0.25, 0.86), (0.53, 0.07, 0.44, 0.40),
                       (0.53, 0.53, 0.44, 0.40)],
    "grid": [(0.025, 0.045, 0.465, 0.445), (0.51, 0.045, 0.465, 0.445),
             (0.025, 0.51, 0.465, 0.445), (0.51, 0.51, 0.465, 0.445)],
    "portrait_four": [(0.05, 0.085, 0.19, 0.83), (0.28, 0.085, 0.19, 0.83),
                      (0.55, 0.085, 0.19, 0.83), (0.78, 0.085, 0.19, 0.83)],
    "mosaic_four": [(0.05, 0.085, 0.19, 0.83), (0.28, 0.085, 0.19, 0.83),
                    (0.53, 0.07, 0.44, 0.40), (0.53, 0.53, 0.44, 0.40)],
}

SRGB_PROFILE = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def spec_dict(spec: PrintSpec) -> dict:
    return {
        "label": spec.label, "width_cm": spec.width_cm,
        "height_cm": spec.height_cm, "dpi": spec.dpi,
        "bleed_mm": spec.bleed_mm, "safe_mm": spec.safe_mm,
        "pixels": spec.pixels,
    }


def _rgb_icc(raw: bytes | None, filename: str) -> tuple[ImageCms.ImageCmsProfile, str]:
    if not raw:
        raise ValueError(f"{filename}: no embedded colour profile; re-export from Lightroom with a tagged RGB profile")
    try:
        profile = ImageCms.ImageCmsProfile(BytesIO(raw))
        if profile.profile.xcolor_space.strip() != "RGB":
            raise ValueError("not an RGB profile")
        return profile, ImageCms.getProfileDescription(profile).strip()
    except (OSError, ValueError, ImageCms.PyCMSError) as exc:
        raise ValueError(f"{filename}: unreadable or non-RGB colour profile; re-export from Lightroom") from exc


def source_print_profile(source_path: Path, label: str | None = None) -> tuple[bytes, str]:
    label = label or source_path.name
    with Image.open(source_path) as image:
        if image.mode != "RGB":
            raise ValueError(f"{label}: print source must be an RGB JPEG or PNG")
        raw = image.info.get("icc_profile")
        _, description = _rgb_icc(raw, label)
        return raw, description


def print_output_profile(project: dict, photos: dict[str, dict], originals_dir: Path) -> tuple[bytes, str]:
    """Keep one shared source RGB profile; normalize mixed profiles to sRGB."""
    profile_bytes = set()
    names = set()
    used = {slot["photo_id"] for spread in project["spreads"] for slot in spread["slots"]}
    for photo_id in used:
        photo = photos[photo_id]
        raw, description = source_print_profile(originals_dir / photo["stored_name"], photo["name"])
        profile_bytes.add(raw)
        names.add(description)
    if len(profile_bytes) == 1:
        return next(iter(profile_bytes)), next(iter(names))
    return SRGB_PROFILE, "sRGB (mixed source profiles converted for one album)"


def _convert_rgb(image: Image.Image, target_profile: bytes,
                 allow_untagged: bool = False) -> Image.Image:
    embedded = image.info.get("icc_profile")
    oriented = ImageOps.exif_transpose(image)
    if not embedded:
        if not allow_untagged:
            raise ValueError("Print source has no embedded colour profile")
        return oriented.convert("RGB")
    source, _ = _rgb_icc(embedded, "Image")
    if embedded == target_profile:
        return oriented.convert("RGB")
    target, _ = _rgb_icc(target_profile, "Output")
    try:
        return ImageCms.profileToProfile(oriented.convert("RGB"), source, target, outputMode="RGB")
    except (OSError, ValueError, ImageCms.PyCMSError) as exc:
        raise ValueError("Could not convert the embedded colour profile for this image") from exc


def _srgb(image: Image.Image) -> Image.Image:
    """Convert previews to browser-friendly sRGB; untagged sources are provisional."""
    return _convert_rgb(image, SRGB_PROFILE, allow_untagged=True)


def validate_project(project: dict, photos: dict[str, dict]) -> list[str]:
    errors = []
    spreads = project.get("spreads", [])
    if not isinstance(spreads, list) or not spreads:
        return ["The album needs at least one spread."]
    if len(spreads) > 40:
        errors.append("This prototype allows at most 40 spreads.")
    for index, spread in enumerate(spreads, 1):
        layout = spread.get("layout")
        slots = spread.get("slots")
        if layout not in LAYOUTS:
            errors.append(f"Spread {index} has an unknown layout.")
            continue
        if not isinstance(slots, list) or len(slots) != len(LAYOUTS[layout]):
            errors.append(f"Spread {index} needs {len(LAYOUTS[layout])} photos.")
            continue
        for slot in slots:
            if slot.get("photo_id") not in photos:
                errors.append(f"Spread {index} references a missing photo.")
            for axis in ("focus_x", "focus_y"):
                value = slot.get(axis, 0.5)
                if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                    errors.append(f"Spread {index} has an invalid crop position.")
    return errors


def preflight_project(project: dict, photos: dict[str, dict], originals_dir: Path) -> dict:
    """Report print risks without guessing whether a face or subject is safe."""
    specs = print_specs(project)
    errors = validate_project(project, photos)
    issues = []
    profile_checks: dict[str, str | None] = {}
    for index, spread in enumerate(project.get("spreads", []), 1):
        rects = LAYOUTS.get(spread.get("layout"), [])
        for slot_number, (rect, slot) in enumerate(zip(rects, spread.get("slots", [])), 1):
            photo = photos.get(slot.get("photo_id"))
            if not photo:
                continue
            source = originals_dir / photo.get("stored_name", "")
            if source.parent != originals_dir or not source.is_file():
                errors.append(f"Spread {index}: original file missing for {photo.get('name', 'photo')}.")
                continue
            if slot["photo_id"] not in profile_checks:
                try:
                    _, profile_checks[slot["photo_id"]] = source_print_profile(source, photo["name"])
                except (OSError, ValueError) as exc:
                    profile_checks[slot["photo_id"]] = None
                    errors.append(str(exc))
            width, height = photo.get("width", 0), photo.get("height", 0)
            if not width or not height:
                errors.append(f"Spread {index}: dimensions missing for {photo['name']}.")
                continue
            for product, spec in specs.items():
                target_w = rect[2] * spec.pixels[0]
                target_h = rect[3] * spec.pixels[1]
                scale = max(target_w / width, target_h / height)
                actual_dpi = round(spec.dpi / scale)
                if actual_dpi < spec.dpi * .75:
                    issues.append({"severity": "high", "spread": index, "slot": slot_number,
                                   "product": product, "message": f"{photo['name']}: about {actual_dpi} effective DPI in {spec.label}; check sharpness or choose a smaller frame."})
                elif actual_dpi < spec.dpi * .9:
                    issues.append({"severity": "review", "spread": index, "slot": slot_number,
                                   "product": product, "message": f"{photo['name']}: about {actual_dpi} effective DPI in {spec.label}; inspect at print size."})
            x, y, w, h = rect
            if x < .5 < x + w and w < .99:
                issues.append({"severity": "review", "spread": index, "slot": slot_number,
                               "product": "all", "message": "This photo crosses the centre fold; check faces and text in the chosen edition."})
    minimum = minimum_spreads(project.get("design_format"))
    if len(project.get("spreads", [])) < minimum:
        errors.append(f"At least {minimum} spreads / {minimum * 2} printed sides are required for this book.")
    if is_saal(project.get("design_format")):
        from saal_export import cover_preflight
        errors.extend(cover_preflight(project, photos, originals_dir, issues))
    if len(project.get("spreads", [])) > 40:
        errors.append("This album tool allows at most 40 spreads / 80 printed sides.")
    return {"errors": list(dict.fromkeys(errors)), "issues": issues,
            "spread_count": len(project.get("spreads", [])),
            "source_profiles": sorted({name for name in profile_checks.values() if name}),
            "specifications": {key: spec_dict(spec) for key, spec in specs.items()},
            "checked_at": datetime.now(timezone.utc).isoformat()}


def render_spread(spread: dict, spec: PrintSpec, photos: dict[str, dict],
                  originals_dir: Path, output_profile: bytes = SRGB_PROFILE) -> tuple[Image.Image, list[str]]:
    width, height = spec.pixels
    canvas = Image.new("RGB", (width, height), "white")
    warnings = []
    for number, (rect, slot) in enumerate(zip(LAYOUTS[spread["layout"]], spread["slots"]), 1):
        x, y, w, h = rect
        left, top = round(x * width), round(y * height)
        right, bottom = round((x + w) * width), round((y + h) * height)
        target = (right - left, bottom - top)
        photo = photos[slot["photo_id"]]
        # The path comes from the server-generated manifest, never the project.
        source_path = originals_dir / photo["stored_name"]
        if source_path.parent != originals_dir or not source_path.is_file():
            raise ValueError(f"Original missing for {photo['name']}")
        with Image.open(source_path) as opened:
            source = _convert_rgb(opened, output_profile)
            source_ratio = source.width / source.height
            target_ratio = target[0] / target[1]
            if source_ratio > target_ratio:
                effective = (round(source.height * target_ratio), source.height)
            else:
                effective = (source.width, round(source.width / target_ratio))
            scale = max(target[0] / effective[0], target[1] / effective[1])
            if scale > 1.12:
                warnings.append(
                    f"{photo['name']} on spread {spread.get('number', '?')}, slot {number}: "
                    f"enlarged {scale:.2f}× for {spec.label}; inspect sharpness."
                )
            crop = ImageOps.fit(source, target, Image.Resampling.LANCZOS,
                                centering=(slot.get("focus_x", 0.5), slot.get("focus_y", 0.5)))
            canvas.paste(crop, (left, top))
            crop.close()
            source.close()
    return canvas, warnings


def export_album(project: dict, photos: dict[str, dict], originals_dir: Path,
                 output_zip: Path) -> dict:
    if is_saal(project.get("design_format")):
        from saal_export import export_saal
        return export_saal(project, photos, originals_dir, output_zip)
    specs = print_specs(project)
    companions = companion_requests(project)
    errors = validate_project(project, photos)
    if errors:
        raise ValueError(" ".join(errors))
    output_profile, output_profile_name = print_output_profile(project, photos, originals_dir)
    preflight = preflight_project(project, photos, originals_dir)
    warnings = [issue["message"] for issue in preflight["issues"]]
    if len(project["spreads"]) < 10:
        warnings.append("Draft has fewer than 20 printed sides; the album minimum is 20.")
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_STORED) as archive:
        products = (project["product"],) if project.get("status") == "approved" and project.get("approved_proof_id") and project.get("product") in specs else specs.keys()
        for key in products:
            spec = specs[key]
            for index, spread in enumerate(project["spreads"]):
                spread = {**spread, "number": index + 1}
                image, spread_warnings = render_spread(spread, spec, photos, originals_dir, output_profile)
                warnings.extend(spread_warnings)
                data = BytesIO()
                image.save(data, format="JPEG", quality=100, subsampling=0,
                           dpi=(spec.dpi, spec.dpi), icc_profile=output_profile)
                image.close()
                archive.writestr(f"{key}/{index * 2 + 2:03d}-{index * 2 + 3:03d}.jpg",
                                 data.getvalue())
        manifest = {
            "project_id": project.get("id"),
            "title": project.get("title", "Untitled album"),
            "spreads": len(project["spreads"]),
            "printed_sides": 2 * len(project["spreads"]),
            "specifications": {key: spec_dict(spec) for key, spec in specs.items()},
            "output_color_profile": output_profile_name,
            "specification_source": ("Graphistudio general Print & Bind metric guide (EDGE interior)"
                                     if project.get("design_format") in ("portrait_30x40", "landscape_40x30")
                                     else "Graphistudio product-specific GoBook guide / Print & Bind guide"),
            "design_format": project.get("design_format", "legacy"),
            "companion_option": project.get("companion_option"),
            "companion_quantities": project.get("companion_quantities"),
            "companion_request": companions[0] if len(companions) == 1 else None,
            "companion_requests": companions,
            "warnings": warnings,
            "preflight": preflight,
            "project_revision": project.get("revision"),
            "approved_proof_id": project.get("approved_proof_id"),
            "approval_status": project.get("status", "draft"),
            "selected_product": project.get("product"),
            "order_details": project.get("order_details", {}),
            "approval_record": project.get("approval_record"),
            "source_sha256": project.get("source_manifest", {}),
            "note": "Prototype output. Review every crop and submit test files to Graphistudio before production.",
        }
        archive.writestr("print-check.json", json.dumps(manifest, indent=2))
        lines = ["GRAPHISTUDIO PRINT & BIND HAND-OFF", "", f"Album: {project.get('title', 'Untitled')}",
                 f"Spreads: {len(project['spreads'])}; printed sides: {2 * len(project['spreads'])}",
                 f"Selected edition: {project.get('product') or 'Not selected'}",
                 f"Client approval: {'confirmed proof' if project.get('approved_proof_id') else 'not recorded'}",
                 f"Approved at: {project.get('approved_at', 'Not approved')}",
                 f"Proof reference: {project.get('approved_proof_id', 'None')}", "",
                 f"Book interiors: numbered double-page, maximum-quality RGB JPEGs with embedded {output_profile_name} profile.",
                 f"Interior measurements: {manifest['specification_source']}.",
                 "Use only the selected product folder. Legacy draft exports contain both editions for comparison.",
                 "Companion request: " + (", ".join(f"{item['quantity']} × {item['closed_size']}" for item in companions) if companions else "None"),
                 "Only the main book's interior files are included; confirm smaller-copy availability in Graphistudio Trade Area.",
                 ("EDGE: design the photographic cover, back and printed page-block sides in Graphistudio's EDGE designer; none are in this ZIP."
                  if project.get("design_format") in ("portrait_30x40", "landscape_40x30") else
                  "Use Graphistudio's cover designer for any photographic or personalized cover."),
                 "The Graphistudio Trade Area order form handles the cover, box, material and checkout.",
                 "Choose PRINT & BIND and upload the matching JPEG sequence after the Buy step.", "",
                 "ORDER CHOICES"]
        for key, value in project.get("order_details", {}).items():
            lines.append(f"{key}: {value}")
        lines += ["", "BEFORE ORDERING", "1. Confirm exact product, format, materials, box and cover in Graphistudio Trade Area.",
                  "2. Inspect every spread at full size, especially fold, bleed, faces and margins.",
                  "3. Resolve print-check.json errors/warnings and send Graphistudio a test spread before a paid client order.",
                  "4. Complete any photographic cover or EDGE printed-side design in Graphistudio; no cover artwork is included here."]
        archive.writestr("GRAPHISTUDIO-HANDOFF.txt", "\n".join(lines) + "\n")
    return manifest
