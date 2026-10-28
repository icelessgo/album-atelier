"""Manual Saal hand-off, using verified matte/white-acrylic IDML geometry.

Render one sheet at a time. JPEGs are also included so a PDF over the shop's
200 MB file limit never forces an invisible reduction in print quality.
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import zipfile

from PIL import Image, ImageOps

from saal_profiles import SAAL_PROFILES, PROFILE_REVISION, cover_crop, saal_offer

PDF_BUDGET = 190_000_000  # room for PDF objects beneath the 200 MB shop limit
SOURCE = "https://www.saal-digital.eu/photo-book/professional-zone/"


def cover_photo(project, photos):
    photo_id = project.get("cover_photo_id") or project["spreads"][0]["slots"][0]["photo_id"]
    if photo_id not in project.get("photo_ids", []) or photo_id not in photos:
        raise ValueError("Choose an available album photo for the cover")
    return photos[photo_id]


def cover_preflight(project, photos, originals, issues):
    from renderer import source_print_profile
    try:
        crop = cover_crop(project.get("cover_crop"))
        photo = cover_photo(project, photos)
        source = originals / photo["stored_name"]
        if source.parent != originals or not source.is_file():
            raise ValueError("The cover original is missing; fetch originals before exporting")
        source_print_profile(source, photo["name"])
        w, h = SAAL_PROFILES[project["design_format"]]["cover_trim_mm"]
        dpi = min(photo["width"] / (w / 25.4), photo["height"] / (h / 25.4))
        if dpi < 360:
            issues.append({"severity": "high" if dpi < 300 else "review", "product": "saal",
                           "message": f"Cover: about {round(dpi)} effective DPI; Saal specifies 400 DPI. Inspect sharpness."})
        return []
    except (OSError, ValueError, KeyError, IndexError) as exc:
        return [str(exc)]


def sheet_geometry(trim_mm, dpi, *, cover=False):
    left, top, right, bottom = (20, 3, 3, 3) if cover else (3, 3, 3, 3)
    w, h = trim_mm
    return {"trim_mm": list(trim_mm), "bleed_mm": [left, top, right, bottom],
            "output_mm": [w + left + right, h + top + bottom], "dpi": dpi}


def extend_bleed(trim, geometry):
    """Extend edge pixels outside the approved trim, without changing its crop."""
    dpi = geometry["dpi"]
    left, top, _, _ = [round(mm / 25.4 * dpi) for mm in geometry["bleed_mm"]]
    size = tuple(round(mm / 25.4 * dpi) for mm in geometry["output_mm"])
    result = Image.new("RGB", size)
    w, h = trim.size
    # Extend horizontally first, then vertically, including corner pixels.
    result.paste(trim, (left, top))
    for box, target in [((0, 0, 1, h), (0, top, left, top + h)),
                        ((w - 1, 0, w, h), (left + w, top, size[0], top + h))]:
        with trim.crop(box) as edge:
            with edge.resize((target[2] - target[0], h)) as stretched:
                result.paste(stretched, target[:2])
    for y, target in [(top, (0, 0, size[0], top)),
                      (top + h - 1, (0, top + h, size[0], size[1]))]:
        with result.crop((0, y, size[0], y + 1)) as edge:
            with edge.resize((size[0], target[3] - target[1])) as stretched:
                result.paste(stretched, target[:2])
    return result


def write_pdf(paths, output, geometry, icc):
    """JPEG streams remain compressed. DefaultRGB supplies the embedded sRGB ICC."""
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfdoc
    mm = 72 / 25.4
    width, height = [v * mm for v in geometry["output_mm"]]
    left, _, _, bottom = geometry["bleed_mm"]
    trim_w, trim_h = geometry["trim_mm"]
    pdf = canvas.Canvas(str(output), pagesize=(width, height), pageCompression=1)
    pdf.setTitle("Saal Professional Line print sheets")
    pdf.setAuthor("Album Studio")
    profile = pdf._doc.Reference(pdfdoc.PDFStream(pdfdoc.PDFDictionary({"N": 3}), icc))
    pdf._doc.Reference(pdfdoc.PDFArray([pdfdoc.PDFName("ICCBased"), profile]), "AlbumSRGB")
    for path in paths:
        pdf._colorsUsed["AlbumSRGB"] = "DefaultRGB"
        pdf.setTrimBox((left * mm, bottom * mm, (left + trim_w) * mm, (bottom + trim_h) * mm))
        pdf.setBleedBox((0, 0, width, height))
        pdf.drawImage(str(path), 0, 0, width=width, height=height)
        pdf.showPage()
    pdf.save()


def export_saal(project, photos, originals, output_zip):
    from renderer import (SRGB_PROFILE, _convert_rgb, companion_requests,
                          preflight_project, print_specs, render_spread)
    checks = preflight_project(project, photos, originals)
    companion_requests(project)
    if checks["errors"]:
        raise ValueError(" ".join(checks["errors"]))
    profile = SAAL_PROFILES[project["design_format"]]
    interior = sheet_geometry(profile["trim_mm"], 300)
    cover = sheet_geometry(profile["cover_trim_mm"], 400, cover=True)
    warnings = list(dict.fromkeys(issue["message"] for issue in checks["issues"]))
    manifest = {"project_id": project.get("id"), "project_revision": project.get("revision"),
                "title": project.get("title"), "design_format": project["design_format"],
                "spreads": len(project["spreads"]), "printed_sides": 2 * len(project["spreads"]),
                "profile_revision": PROFILE_REVISION, "specification_source": SOURCE,
                "output_color_profile": "sRGB", "specifications": {"interiors": interior, "cover": cover},
                "offer": project.get('approval_record', {}).get('offer') or project.get('catalogue_offer') or saal_offer(project["design_format"]),
                "client_price_eur": project.get('approval_record', {}).get('total_minor',
                                        (project.get('catalogue_offer') or saal_offer(project["design_format"]))['base_eur'] * 100) / 100,
                "approval_record": project.get('approval_record'),
                "order_details": project.get('order_details', {}),
                "approved_proof_id": project.get("approved_proof_id"),
                "approval_status": project.get("status", "draft"),
                "cover_crop": cover_crop(project.get("cover_crop")), "preflight": checks,
                "files": {}, "pdf_omissions": [], "warnings": warnings}
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="saal-", dir=output_zip.parent) as folder:
        folder = Path(folder)
        pages = []
        def save_jpeg(trim, geometry, path):
            with extend_bleed(trim, geometry) as sheet:
                sheet.save(path, "JPEG", quality=100, subsampling=0,
                           dpi=(geometry["dpi"],) * 2, icc_profile=SRGB_PROFILE)
            if path.stat().st_size >= 200_000_000:
                raise ValueError("A print sheet exceeds Saal's 200 MB limit; ask the photographer to review it")
        for index, spread in enumerate(project["spreads"], 1):
            image, _ = render_spread(spread, print_specs(project)["saal"], photos, originals, SRGB_PROFILE)
            path = folder / f"{index:03d}.jpg"
            try:
                save_jpeg(image, interior, path)
            finally:
                image.close()
            pages.append(path)
        photo = cover_photo(project, photos)
        crop = manifest["cover_crop"]
        with Image.open(originals / photo["stored_name"]) as opened:
            with _convert_rgb(opened, SRGB_PROFILE) as source:
                size = tuple(round(mm / 25.4 * 400) for mm in profile["cover_trim_mm"])
                with ImageOps.fit(source, size, Image.Resampling.LANCZOS,
                                  centering=(crop["focus_x"], crop["focus_y"])) as trim:
                    save_jpeg(trim, cover, folder / "cover.jpg")
        files = [(f"saal/interiors/{p.name}", p) for p in pages]
        files.append(("saal/cover.jpg", folder / "cover.jpg"))
        for name, inputs, geometry in [("interiors", pages, interior), ("cover", [folder / "cover.jpg"], cover)]:
            if sum(p.stat().st_size for p in inputs) > PDF_BUDGET:
                manifest["pdf_omissions"].append(name)
                continue
            path = folder / f"{name}.pdf"
            write_pdf(inputs, path, geometry, SRGB_PROFILE)
            if path.stat().st_size >= 200_000_000:
                manifest["pdf_omissions"].append(name)
            else:
                files.append((f"saal/{name}.pdf", path))
        used = {slot["photo_id"] for spread in project["spreads"] for slot in spread["slots"]}
        used.add(photo["id"])
        manifest["source_sha256"] = {key: sha256((originals / photos[key]["stored_name"]).read_bytes()).hexdigest() for key in sorted(used)}
        with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, path in files:
                manifest["files"][name] = {"bytes": path.stat().st_size, "sha256": sha256(path.read_bytes()).hexdigest()}
                archive.write(path, name)
            archive.writestr("print-check.json", json.dumps(manifest, indent=2, ensure_ascii=False))
            archive.writestr("SAAL-HANDOFF.txt", handoff(project, manifest))
    return manifest


def handoff(project, manifest):
    offer = manifest["offer"]
    return f"""SAAL PROFESSIONAL LINE — MANUAL ORDER HAND-OFF
Album: {project.get('title', 'Untitled')}
Client selling price: EUR {manifest['client_price_eur']:.2f}. Supplier price, extras and delivery must be checked separately.
Format: {offer['closed_size']} standard Professional Line (NOT XT).
Paper: matte photographic paper. Cover: acrylic + WHITE leatherette. No barcode.
Book pages: {manifest['printed_sides']} ({manifest['spreads']} double-page spreads), excluding cover.
Approval: {manifest['approval_status']}; proof: {manifest.get('approved_proof_id') or 'not recorded'}.

1. Extract this ZIP. Read print-check.json and resolve sharpness/crop warnings.
2. Inspect cover.pdf and every page of interiors.pdf at print size. Pages include
   the supplier's bleed, sRGB profile and trim boxes; there are no printer marks.
   Cover artwork has 20 mm left bleed and 3 mm on the other edges. Interior bleed
   is 3 mm. Check content near the trim and fold; edge pixels extend into bleed.
3. In Saal's online shop use PDF upload for the exact configuration above.
   Upload cover.pdf separately from interiors.pdf. Each interior PDF page is one
   COMPLETE double-page spread. First left and last right halves are printed
   inside the covers; Studio does not insert blank halves or extra pages.
4. If a PDF was omitted ({', '.join(manifest['pdf_omissions']) or 'none'}), use the
   numbered JPEG sequence in saal/interiors and cover.jpg in the corresponding
   PDF-upload workflow. Individual files must be below 200 MB. Do not upload ZIP.
5. Check every crop, sequence, product, cover material/colour and total in Saal's
   preview. Professional-account discounts do not stack with vouchers/promotions.
   Studio does not place an order or charge the client. Arrange payment manually.
6. Validate a sample with Saal before a paid client order. These measured export
   profiles have not yet been confirmed by a physical printed sample.

Technical profile: {PROFILE_REVISION}
Official measurements: {SOURCE}
PDF upload: https://www.saal-digital.eu/service/professional-zone/upload-your-pdf-in-the-online-shop/
"""
