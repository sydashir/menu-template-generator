"""
Core pipeline — ties extractor, separator, analyzer, and builder together.
Called by both the FastAPI app and any direct script usage.
"""

import base64
import io
import json
from pathlib import Path
from typing import Optional

from PIL import Image
from dotenv import load_dotenv

load_dotenv(override=True)

from extractor import (
    load_pages, extract_blocks_pdf, extract_blocks_image,
    detect_logo_pdf, is_double_sided, split_double_sided, extract_separators_pdf,
)
from separator import detect_separators
from analyzer import detect_columns, classify_blocks, build_menu_data
from builder import build_template, build_template_from_claude
from claude_extractor import (
    build_menu_data_from_claude,
    extract_full_layout_via_tool_use,
    extract_layout_surya_som,
    merge_layouts,
    match_badges,
    _mask_logo_elements,
    _refine_logo_bbox_by_pixels,
    _enforce_single_logo,
)
from hybrid_engine import validate_graphic_elements
from s3_asset_library import resolve_asset

SUPPORTED_PDF = {".pdf"}
SUPPORTED_IMG = {".jpg", ".jpeg", ".png", ".webp"}


def _apply_s3_natural_bbox(
    el: dict,
    asset_bytes: bytes,
    canvas_w: int,
    canvas_h: int,
) -> None:
    """
    After resolving an S3 asset, resize the element's bbox to match the asset's
    natural aspect ratio at a physically sensible display size.
    Mutates el["bbox"] in-place. Preserves the detected center position.

    Target display sizes at 200 DPI (pixel-space):
    - badge (square):  130 px square
    - badge (wide):     90 px tall, width from aspect ratio
    - separator/*:     fill detected width, height from aspect ratio (cap 55 px)
    - ornament/*:       70 px tall, width from aspect ratio (cap 70% of canvas)
    """
    try:
        pil = Image.open(io.BytesIO(asset_bytes))
        nat_w, nat_h = pil.size
    except Exception:
        return

    if nat_w <= 0 or nat_h <= 0:
        return

    aspect = nat_w / nat_h
    bd = el.get("bbox") or {}
    cx = float(bd.get("x", 0)) + float(bd.get("w", 0)) / 2
    cy = float(bd.get("y", 0)) + float(bd.get("h", 0)) / 2
    sl = el.get("semantic_label", "")
    el_type = el.get("type", "")
    el_sub = el.get("subtype", "")

    if el_type == "image" and el_sub == "badge":
        target_h = 130.0 if aspect < 1.5 else 90.0
        target_w = round(target_h * aspect)
        target_h = round(target_h)

    elif sl.startswith("separator/"):
        detected_w = float(bd.get("w", 0))
        # Use detected width if reasonable; otherwise fill 60% of canvas
        target_w = detected_w if detected_w > canvas_w * 0.1 else canvas_w * 0.60
        target_w = min(target_w, float(canvas_w))
        target_h = target_w / aspect
        if target_h > 55:   # cap: real separator strips are thin
            target_h = 55.0
            target_w = target_h * aspect
        target_w, target_h = round(target_w), round(target_h)

    elif sl.startswith("ornament/"):
        target_h = 70.0
        target_w = target_h * aspect
        # Narrow ornaments (scrolls, diamonds, rules) are small in-column dividers;
        # cap them tightly so they don't bleed across columns.
        _NARROW_ORNAMENTS = {
            "ornament/scroll_divider", "ornament/diamond_rule",
            "ornament/dotted_ornament", "ornament/calligraphic_rule",
        }
        max_frac = 0.30 if sl in _NARROW_ORNAMENTS else 0.65
        if target_w > canvas_w * max_frac:
            target_w = canvas_w * max_frac
            target_h = target_w / aspect
        target_w, target_h = round(target_w), round(target_h)

    else:
        return  # unknown — don't touch bbox

    bd["w"] = float(target_w)
    bd["h"] = float(target_h)
    bd["x"] = max(0.0, cx - float(target_w) / 2)
    bd["y"] = max(0.0, cy - float(target_h) / 2)
    # Clamp to canvas bounds
    bd["x"] = min(bd["x"], max(0.0, float(canvas_w) - bd["w"]))
    bd["y"] = min(bd["y"], max(0.0, float(canvas_h) - bd["h"]))


def _scan_pdf_decorators_via_claude(
    img: Image.Image,
    existing_elements: list,
    canvas_w: int,
    canvas_h: int,
) -> list:
    """
    Dedicated Anthropic API call that exhaustively finds ALL decorative graphical
    elements in the menu image — ornaments, separator patterns, badges, logos,
    collage boxes — and matches them to S3 asset slugs.

    Runs AFTER the main extraction to catch anything still missing.
    Returns a list of element dicts ready to inject into the template.
    Each resolved S3 asset gets bbox normalized via _apply_s3_natural_bbox.
    """
    from claude_extractor import _get_client
    from s3_asset_library import KNOWN_LABELS

    client = _get_client()
    if client is None:
        return []

    orig_w, orig_h = img.size
    scale_x = scale_y = 1.0
    send_img = img
    _MAX_DIM = 1920
    if max(orig_w, orig_h) > _MAX_DIM:
        ratio = _MAX_DIM / max(orig_w, orig_h)
        nw, nh = max(1, int(orig_w * ratio)), max(1, int(orig_h * ratio))
        send_img = img.resize((nw, nh), Image.LANCZOS)
        scale_x = orig_w / nw
        scale_y = orig_h / nh

    sw, sh = send_img.size
    buf = io.BytesIO()
    send_img.convert("RGB").save(buf, format="JPEG", quality=90)
    b64 = base64.b64encode(buf.getvalue()).decode()

    palette_lines = "\n".join(f"  {lbl}" for lbl in KNOWN_LABELS)

    # Build a summary of already-extracted elements so Claude doesn't re-detect them
    already_found = []
    for el in existing_elements:
        bd = el.get("bbox") or {}
        already_found.append(
            f"  {el.get('type')}/{el.get('subtype','?')} "
            f"@ x={bd.get('x',0):.0f} y={bd.get('y',0):.0f} "
            f"w={bd.get('w',0):.0f} h={bd.get('h',0):.0f}"
        )
    already_text = "\n".join(already_found) if already_found else "  (none yet)"

    prompt = (
        f"This restaurant menu image is {sw}×{sh} pixels.\n\n"
        "TASK: Find EVERY graphical/decorative element that is NOT pure text:\n"
        "  - Ornaments: calligraphic swashes, scrollwork, floral dividers\n"
        "  - Separator patterns: wavy lines, double lines, diamond rules\n"
        "  - Badges: brand circles (Food Network, OpenTable/Diners Choice, YouTube, Yelp, TripAdvisor, Hulu, Michelin)\n"
        "  - Logos: restaurant emblems, crests, wordmarks\n"
        "  - Collage boxes: panels containing multiple logos or social handles side by side\n\n"
        "S3 PALETTE — use the exact slug if the element matches:\n"
        f"{palette_lines}\n\n"
        "If an element doesn't match any palette slug, use semantic_label=null and still include it.\n\n"
        "Already extracted (DO NOT re-add these — their positions are already captured):\n"
        f"{already_text}\n\n"
        "Return ONLY a JSON array — no explanation:\n"
        "[\n"
        "  {\"type\": \"image|separator\", \"subtype\": \"badge|ornament|collage_box|logo|horizontal_line\",\n"
        "   \"semantic_label\": \"slug_or_null\",\n"
        "   \"bbox\": {\"x\": <px>, \"y\": <px>, \"w\": <px>, \"h\": <px>}}\n"
        "]\n"
        "Be exhaustive. Include every non-text graphical element you see, even small ones."
    )

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        raw = (resp.content[0].text or "").strip()
        import re as _re
        m = _re.search(r"\[.*\]", raw, _re.DOTALL)
        if not m:
            print("[scan_decorators] no JSON array in response")
            return []
        items = json.loads(m.group())
    except Exception as exc:
        print(f"[scan_decorators] failed: {exc}")
        return []

    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        bd = item.get("bbox") or {}
        w = max(1.0, float(bd.get("w", 0)) * scale_x)
        h = max(1.0, float(bd.get("h", 0)) * scale_y)
        x = float(bd.get("x", 0)) * scale_x
        y = float(bd.get("y", 0)) * scale_y
        if w < 5 or h < 5:
            continue
        el = {
            "type": item.get("type", "image"),
            "subtype": item.get("subtype", "ornament"),
            "semantic_label": item.get("semantic_label") or None,
            "bbox": {"x": x, "y": y, "w": w, "h": h},
        }
        # Resolve S3 asset and normalize bbox
        sl = el.get("semantic_label")
        if sl:
            s3_bytes = resolve_asset(sl)
            if s3_bytes:
                el["image_data"] = base64.b64encode(s3_bytes).decode()
                _apply_s3_natural_bbox(el, s3_bytes, canvas_w, canvas_h)
        result.append(el)

    print(f"[scan_decorators] {len(result)} decorative elements found by dedicated scan")
    return result


def _enrich_template_separators_from_claude(
    template,
    claude_layout: dict,
    side_img: Image.Image,
    canvas_w: int,
    canvas_h: int,
) -> None:
    """
    Post-process: match PDF vector separator elements (already in template) against
    Claude's detected decorative elements that have semantic_labels.

    PDF vector extraction gives exact positions but no S3 identity.
    Claude Vision gives approximate positions but correct S3 labels.
    This function bridges the two: when a template separator is close to a Claude
    decorative element with a semantic_label, it copies the label, fetches the S3
    asset, and applies natural-proportion bbox sizing.

    Mutates template.elements in-place.
    """
    claude_decoratives = [
        e for e in claude_layout.get("elements", [])
        if e.get("semantic_label")
        and (e.get("type") in ("separator", "image"))
        and (
            e["semantic_label"].startswith("separator/")
            or e["semantic_label"].startswith("ornament/")
        )
    ]
    if not claude_decoratives:
        return

    _TOL_Y = 80   # px — Claude's y estimate can be off by this much
    _TOL_X = 200  # px — wide tolerance for x since widths differ

    for el in template.elements:
        if el.get("type") != "separator":
            continue
        if el.get("image_data") or el.get("semantic_label"):
            continue  # already has S3 asset

        bd = el.get("bbox") or {}
        el_cy = float(bd.get("y", 0)) + float(bd.get("h", 0)) / 2
        el_cx = float(bd.get("x", 0)) + float(bd.get("w", 0)) / 2

        # Find closest Claude decorative by vertical proximity
        best = None
        best_dist = float("inf")
        for cd in claude_decoratives:
            cbd = cd.get("bbox") or {}
            cd_cy = float(cbd.get("y", 0)) + float(cbd.get("h", 0)) / 2
            cd_cx = float(cbd.get("x", 0)) + float(cbd.get("w", 0)) / 2
            dy = abs(el_cy - cd_cy)
            dx = abs(el_cx - cd_cx)
            if dy < _TOL_Y and dx < _TOL_X and dy < best_dist:
                best = cd
                best_dist = dy

        if best is None:
            continue

        label = best["semantic_label"]
        s3_bytes = resolve_asset(label)
        if not s3_bytes:
            continue

        el["semantic_label"] = label
        el["image_data"] = base64.b64encode(s3_bytes).decode()
        _apply_s3_natural_bbox(el, s3_bytes, canvas_w, canvas_h)
        print(f"[enrich_seps] matched sep @ y={el_cy:.0f} → {label}")


def _cleanup_duplicate_graphics(template) -> None:
    """
    Post-processing pass run after all injection/enrichment steps.

    Two problems it fixes:
    1. A PDF vector separator and a Claude-detected image/ornament both land at the
       same position (cross-type duplicates survive the same-type dedup).  Keep the
       element that has image_data; fall back to keeping the image type.
    2. ornament/floral_swash_* images land inside dense text content because the
       "already covered" guard in _inject_pdf_graphics only checks OpenCV blobs, not
       text.  An ornament whose vertical center sits within 35 px of 2+ text elements
       is treated as misplaced and dropped.
    """
    els = template.elements

    # ── Fix 1: cross-type positional dedup ───────────────────────────────────────
    graphic_types = {"separator", "image"}
    graphics = [e for e in els if e.get("type") in graphic_types]
    text_els  = [e for e in els if e.get("type") == "text"]

    def _cy(e):
        b = e.get("bbox") or {}
        return float(b.get("y", 0)) + float(b.get("h", 0)) / 2

    def _cx(e):
        b = e.get("bbox") or {}
        return float(b.get("x", 0)) + float(b.get("w", 0)) / 2

    drop_ids: set[int] = set()
    for i, a in enumerate(graphics):
        if id(a) in drop_ids:
            continue
        for b in graphics[i + 1:]:
            if id(b) in drop_ids:
                continue
            if abs(_cx(a) - _cx(b)) < 20 and abs(_cy(a) - _cy(b)) < 20:
                # Same position — keep whichever has image_data; prefer image over separator
                a_score = (bool(a.get("image_data")), a.get("type") == "image")
                b_score = (bool(b.get("image_data")), b.get("type") == "image")
                if b_score >= a_score:
                    drop_ids.add(id(a))
                else:
                    drop_ids.add(id(b))

    removed_dup = len(drop_ids)

    # ── Fix 2: drop ornament images sitting on text content ──────────────────────
    # Any ornament/separator image whose vertical center is within 40px of 2+ text
    # elements is misplaced (landed inside menu content, not on a section header).
    for el in els:
        if id(el) in drop_ids:
            continue
        if el.get("type") != "image" or el.get("subtype") != "ornament":
            continue
        el_cy = _cy(el)
        nearby_text = sum(
            1 for t in text_els
            if abs(_cy(t) - el_cy) < 40
        )
        if nearby_text >= 2:
            drop_ids.add(id(el))

    removed_floral = len(drop_ids) - removed_dup

    # ── Fix 3: drop oversized ornament pixel-crops ────────────────────────────────
    # Large ornament bboxes are always background/border artefacts, not real decorators.
    canvas_els = [e for e in els if e.get("type") in ("separator","image","logo","text")]
    if canvas_els:
        all_x2 = [float((e.get("bbox") or {}).get("x",0)) + float((e.get("bbox") or {}).get("w",0)) for e in canvas_els]
        all_y2 = [float((e.get("bbox") or {}).get("y",0)) + float((e.get("bbox") or {}).get("h",0)) for e in canvas_els]
        est_canvas_area = max(all_x2, default=1) * max(all_y2, default=1)
    else:
        est_canvas_area = 1
    for el in els:
        if id(el) in drop_ids:
            continue
        if el.get("type") != "image" or el.get("subtype") != "ornament":
            continue
        b = el.get("bbox") or {}
        area = float(b.get("w", 0)) * float(b.get("h", 0))
        if area > est_canvas_area * 0.03:
            drop_ids.add(id(el))

    removed_large = len(drop_ids) - removed_dup - removed_floral

    if drop_ids:
        template.elements = [e for e in els if id(e) not in drop_ids]
        print(f"[cleanup] removed {removed_dup} cross-type duplicates, "
              f"{removed_floral} text-area ornaments, "
              f"{removed_large} oversized ornament crops "
              f"({len(template.elements)} elements remain)")


def _get_overlap(a: dict, b: dict) -> float:
    """Calculate Intersection-over-Union (IoU) between two bounding boxes."""
    ax1, ay1 = a.get("x", 0), a.get("y", 0)
    ax2, ay2 = ax1 + a.get("w", 0), ay1 + a.get("h", 0)
    bx1, by1 = b.get("x", 0), b.get("y", 0)
    bx2, by2 = bx1 + b.get("w", 0), by1 + b.get("h", 0)

    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)

    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0

    inter = (ix2 - ix1) * (iy2 - iy1)
    # Area of intersection relative to the SMALLEST box (more robust for label transfer)
    min_area = min(a.get("w", 0) * a.get("h", 0), b.get("w", 0) * b.get("h", 0))
    return inter / min_area if min_area > 0 else 0.0


def _inject_pdf_graphics(
    template,
    claude_layout: dict,
    side_img,
    canvas_w: int,
    canvas_h: int,
    logo_info: Optional[dict] = None,
) -> None:
    """
    Extract logos/badges/S3 decorators from hybrid OpenCV + Claude labels.
    PDFs use PyMuPDF for text (exact); this function overlays only the visual graphics.
    """
    # PDF canvas dimensions are in PDF points (standard letter = 612×792).
    # Pixel-space images are typically >1000px wide. Scale guards accordingly.
    _is_point_space = canvas_w < 1000
    # Badge size cap: in points, 45pt ≈ 0.6 inch (real icon). In pixels, 220px.
    _MAX_BADGE_DIM = 45 if _is_point_space else 220
    # Fixed physical dimensions for S3 assets (don't trust Claude's sloppy CV bboxes)
    _S3_BADGE_DIM  = 40 if _is_point_space else 130   # square badge target size
    _S3_SEP_H      = 15 if _is_point_space else 40     # separator height; width preserved

    # 1. Get Text Elements from the existing template for spatial context
    text_elements = [el for el in template.elements if el.get("type") == "text"]
    
    # 2. Get Raw OpenCV extractions for the PDF side image (rasterized)
    from separator import detect_graphic_blobs, detect_separators
    cv_blobs = detect_graphic_blobs(side_img)
    matched_assets = match_badges(side_img)
    
    # Also detect lines if they aren't already in template
    # (extract_separators_pdf already finds vector lines, but some ornamental lines might be images)
    img_lines = detect_separators(side_img)
    cv_lines = []
    for ln in img_lines:
        cv_lines.append({
            "bbox": {
                "x": min(ln.x1, ln.x2), "y": min(ln.y1, ln.y2),
                "w": abs(ln.x2 - ln.x1) or 2.0, "h": abs(ln.y2 - ln.y1) or 2.0
            },
            "orientation": ln.orientation,
            "subtype": ln.subtype or ("horizontal_line" if ln.orientation == "horizontal" else "vertical_line")
        })
    
    # 3. Run Hybrid Engine
    # We use the semantic_labels from claude_layout if they match spatially
    hybrid_graphics = validate_graphic_elements(
        text_elements=text_elements,
        raw_cv_lines=cv_lines,
        raw_cv_contours=cv_blobs,
        matched_assets=matched_assets,
        canvas_w=canvas_w,
        canvas_h=canvas_h
    )
    
    # 4. Filter and label hybrid graphics using Claude's semantic labels as hints
    # If Claude found an image with a semantic_label, and it overlaps with a hybrid graphic,
    # transfer the label.
    for hg in hybrid_graphics:
        h_bbox = hg["bbox"]
        for ce in claude_layout.get("elements", []):
            if ce.get("type") in ("image", "separator") and ce.get("semantic_label"):
                c_bbox = ce["bbox"]
                # Use a simple overlap check
                if _get_overlap(h_bbox, c_bbox) > 0.4:
                    hg["semantic_label"] = ce["semantic_label"]
                    if ce.get("subtype"):
                        hg["subtype"] = ce["subtype"]
                    break

    # 4b. Add Claude-only BADGES not covered by any OpenCV detection.
    # Ornaments and separators must come from PyMuPDF vector data or OpenCV blobs —
    # not from Claude's free-form vision scan.  Claude invents ornament positions
    # throughout menu content which produces phantom boxes over text.
    # Badges are different: they are graphical images with no PDF vector representation
    # so we still allow Claude to locate them, but ONLY badge/ labels.
    def _hg_center(el):
        b = el.get("bbox") or {}
        return float(b.get("x", 0)) + float(b.get("w", 0)) / 2, float(b.get("y", 0)) + float(b.get("h", 0)) / 2

    for ce in claude_layout.get("elements", []):
        if ce.get("type") not in ("image", "separator"):
            continue
        sl = ce.get("semantic_label") or ""
        subtype = ce.get("subtype", "")
        is_badge = sl.startswith("badge/")
        is_collage = subtype == "collage_box"
        if not is_badge and not is_collage:
            continue
        ce_bd = ce.get("bbox") or {}
        if ce_bd.get("w", 0) < 5 or ce_bd.get("h", 0) < 5:
            continue
        ce_cx = float(ce_bd.get("x", 0)) + float(ce_bd.get("w", 0)) / 2
        ce_cy = float(ce_bd.get("y", 0)) + float(ce_bd.get("h", 0)) / 2
        # Only treat as covered if an element of the SAME semantic category
        # (badge or collage_box) already exists nearby.  Logo-type blobs are
        # excluded from this check: they get dropped in step 5 when PyMuPDF
        # already found an embedded logo, which would otherwise erase a badge
        # that happens to share the same bounding box position.
        target_subtypes = {"badge"} if is_badge else {"collage_box"}
        already_covered = any(
            abs(ce_cx - _hg_center(hg)[0]) < 80 and abs(ce_cy - _hg_center(hg)[1]) < 80
            for hg in hybrid_graphics
            if hg.get("subtype") in target_subtypes
        )
        if already_covered:
            continue
        hybrid_graphics.append({
            "type": "image",
            "subtype": "collage_box" if is_collage else "badge",
            "semantic_label": sl or None,
            "bbox": {
                "x": float(ce_bd.get("x", 0)), "y": float(ce_bd.get("y", 0)),
                "w": max(1.0, float(ce_bd.get("w", 0))), "h": max(1.0, float(ce_bd.get("h", 0))),
            },
        })
        print(f"[pipeline] PDF Claude {'collage_box' if is_collage else 'badge'}: {sl or subtype}")

    # Skip logo if PyMuPDF already found one
    graphic_els = []
    for hg in hybrid_graphics:
        if hg["type"] == "logo" and logo_info is not None:
            continue
        # For badges in PDFs, only keep if they have a recognized semantic_label
        if hg.get("subtype") == "badge" and not hg.get("semantic_label"):
            continue
        graphic_els.append(hg)

    # Dedup by position: remove near-duplicate elements (within 15px center distance).
    # Cross-type duplicates (e.g. separator + image at same spot) are also caught here;
    # when two elements share a position we keep the one with image_data, or the image type.
    _deduped = []
    for el in graphic_els:
        bd = el.get("bbox") or {}
        cx = float(bd.get("x", 0)) + float(bd.get("w", 0)) / 2
        cy = float(bd.get("y", 0)) + float(bd.get("h", 0)) / 2
        dup_idx = next(
            (
                i for i, p in enumerate(_deduped)
                if abs(cx - (float((p.get("bbox") or {}).get("x", 0)) + float((p.get("bbox") or {}).get("w", 0)) / 2)) < 15
                and abs(cy - (float((p.get("bbox") or {}).get("y", 0)) + float((p.get("bbox") or {}).get("h", 0)) / 2)) < 15
            ),
            None,
        )
        if dup_idx is None:
            _deduped.append(el)
        else:
            # Keep the richer element: prefer image_data, then type==image,
            # then semantically-typed (badge/collage_box) over generic ornament.
            existing = _deduped[dup_idx]
            el_score = (bool(el.get("image_data")), el.get("type") == "image",
                        el.get("subtype") in ("badge", "collage_box"))
            ex_score = (bool(existing.get("image_data")), existing.get("type") == "image",
                        existing.get("subtype") in ("badge", "collage_box"))
            if el_score > ex_score:
                _deduped[dup_idx] = el
    graphic_els = _deduped

    # Dedup by semantic_label: each badge brand appears exactly once (keep smallest bbox)
    _LABEL_LIMITS = [("badge/", 1), ("ornament/", 3), ("separator/", 5)]
    _by_label: dict[str, list] = {}
    for el in graphic_els:
        sl = el.get("semantic_label")
        if sl and el.get("type") in ("image", "separator"):
            _by_label.setdefault(sl, []).append(el)
    _drop_ids: set[int] = set()
    for sl, group in _by_label.items():
        limit = next((v for pf, v in _LABEL_LIMITS if sl.startswith(pf)), 3)
        if len(group) > limit:
            is_badge = sl.startswith("badge/")
            def _score(e, _b=is_badge):
                a = (e.get("bbox") or {}).get("w", 0) * (e.get("bbox") or {}).get("h", 0)
                return -a if _b else a
            keep_ids = {id(e) for e in sorted(group, key=_score, reverse=True)[:limit]}
            for e in group:
                if id(e) not in keep_ids:
                    _drop_ids.add(id(e))
            print(f"[pipeline] PDF dedup: kept {limit}/{len(group)} '{sl}'")
    graphic_els = [el for el in graphic_els if id(el) not in _drop_ids]

    # Badge position-cluster dedup: even when labels differ slightly, two badge
    # elements within 50px of each other are the same physical badge — keep the
    # one with a semantic_label (or the larger one if both labelled).
    _badge_kept: list = []
    _badge_drop: set[int] = set()
    for el in graphic_els:
        if el.get("subtype") != "badge":
            continue
        cx = float((el.get("bbox") or {}).get("x", 0)) + float((el.get("bbox") or {}).get("w", 0)) / 2
        cy = float((el.get("bbox") or {}).get("y", 0)) + float((el.get("bbox") or {}).get("h", 0)) / 2
        clash = next(
            (k for k in _badge_kept
             if abs(cx - (float((k.get("bbox") or {}).get("x", 0)) + float((k.get("bbox") or {}).get("w", 0)) / 2)) < 50
             and abs(cy - (float((k.get("bbox") or {}).get("y", 0)) + float((k.get("bbox") or {}).get("h", 0)) / 2)) < 50),
            None,
        )
        if clash is None:
            _badge_kept.append(el)
        else:
            el_score = (bool(el.get("semantic_label")), float((el.get("bbox") or {}).get("w", 0)))
            k_score  = (bool(clash.get("semantic_label")), float((clash.get("bbox") or {}).get("w", 0)))
            if el_score > k_score:
                _badge_drop.add(id(clash))
                _badge_kept.remove(clash)
                _badge_kept.append(el)
            else:
                _badge_drop.add(id(el))
    if _badge_drop:
        graphic_els = [el for el in graphic_els if id(el) not in _badge_drop]
        print(f"[pipeline] PDF badge cluster dedup: dropped {len(_badge_drop)} duplicates")

    # Pre-S3 badge size cap: discard detections that are clearly background artefacts
    for el in graphic_els:
        if el.get("type") == "image" and el.get("subtype") == "badge":
            bd = el.get("bbox") or {}
            bw, bh = float(bd.get("w", 0)), float(bd.get("h", 0))
            if bw > _MAX_BADGE_DIM or bh > _MAX_BADGE_DIM:
                cx = float(bd.get("x", 0)) + bw / 2
                cy = float(bd.get("y", 0)) + bh / 2
                nd = min(bw, bh, _MAX_BADGE_DIM)
                bd["w"] = nd; bd["h"] = nd
                bd["x"] = cx - nd / 2; bd["y"] = cy - nd / 2
                print(f"[pipeline] PDF badge cap: {bw:.0f}×{bh:.0f} → {nd:.0f}pt")

    # S3 asset resolution (wavy lines, ornaments, known badge PNGs)
    # After fetching, normalize bbox to the asset's natural proportions.
    for el in graphic_els:
        if el.get("semantic_label"):
            s3_bytes = resolve_asset(el["semantic_label"])
            if s3_bytes:
                el["image_data"] = base64.b64encode(s3_bytes).decode()
                _apply_s3_natural_bbox(el, s3_bytes, canvas_w, canvas_h)
                print(f"[pipeline] PDF S3 asset: {el['semantic_label']}")

    # Pixel crop for image elements that didn't resolve via S3.
    # Use a tight area cap per subtype:
    #   ornament — 2% of canvas (real ornaments are small decorative marks)
    #   badge    — 8% of canvas (badges can be larger circular images)
    #   other    — 5% of canvas
    # Anything larger is a background/border crop artefact and must be skipped.
    _canvas_area = canvas_w * canvas_h
    _crop_limits = {"ornament": 0.02, "badge": 0.08}
    for el in graphic_els:
        if el.get("type") == "image" and not el.get("image_data"):
            bd = el.get("bbox") or {}
            ix1 = max(0, int(bd.get("x", 0)))
            iy1 = max(0, int(bd.get("y", 0)))
            ix2 = min(canvas_w, int(bd.get("x", 0) + bd.get("w", 0)))
            iy2 = min(canvas_h, int(bd.get("y", 0) + bd.get("h", 0)))
            if ix2 <= ix1 or iy2 <= iy1:
                continue
            area_limit = _crop_limits.get(el.get("subtype", ""), 0.05)
            if (ix2 - ix1) * (iy2 - iy1) > _canvas_area * area_limit:
                print(f"[pipeline] PDF skip large pixel crop: {el.get('subtype')} "
                      f"{ix2-ix1}×{iy2-iy1}px > {area_limit*100:.0f}% canvas")
                continue
            crop = side_img.crop((ix1, iy1, ix2, iy2))
            buf = io.BytesIO()
            crop.convert("RGB").save(buf, format="PNG")
            el["image_data"] = base64.b64encode(buf.getvalue()).decode()

    # Logo (only reached if PyMuPDF found nothing): pixel-refine bbox + crop
    logo_image_data = None
    for el in graphic_els:
        if el.get("type") == "logo":
            bd = el.get("bbox") or {}
            refined = _refine_logo_bbox_by_pixels(side_img, bd, canvas_w, canvas_h)
            if refined and refined["w"] > 20 and refined["h"] > 20:
                bd.update(refined)
            x1, y1 = max(0, int(bd.get("x", 0))), max(0, int(bd.get("y", 0)))
            x2 = min(canvas_w, int(bd.get("x", 0) + bd.get("w", 0)))
            y2 = min(canvas_h, int(bd.get("y", 0) + bd.get("h", 0)))
            if x2 > x1 and y2 > y1:
                crop = side_img.crop((x1, y1, x2, y2))
                buf = io.BytesIO()
                crop.convert("RGB").save(buf, format="PNG")
                logo_image_data = base64.b64encode(buf.getvalue()).decode()
                print(f"[pipeline] PDF logo cropped: {x2-x1}×{y2-y1}px")
            break

    graphic_els = _enforce_single_logo(graphic_els)

    # Build validated model instances and append to the existing template element list
    tmp = build_template_from_claude(
        {"elements": graphic_els, "background_color": "#ffffff"},
        source_file="",
        canvas_w=canvas_w,
        canvas_h=canvas_h,
        logo_image_data=logo_image_data,
    )
    for el_dict in tmp.elements:
        template.elements.append(el_dict)

    if tmp.elements:
        print(f"[pipeline] PDF hybrid: +{len(tmp.elements)} graphic elements injected")


def process(file_path: str, output_dir: str, file_stem: str = None) -> list[dict]:
    """
    Process a menu file and write outputs to output_dir.
    Returns list of result dicts (one per side/page).
    """
    p = Path(file_path)
    ext = p.suffix.lower()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    if ext not in SUPPORTED_PDF | SUPPORTED_IMG:
        raise ValueError(
            f"Unsupported file type '{ext}'. "
            "Accepted: .pdf, .jpg, .jpeg, .png. "
            "Export .psd files to PNG first."
        )

    pages = load_pages(file_path)
    results = []

    # Extract PDF text blocks once for the whole file (not per-page)
    pdf_blocks_by_page = extract_blocks_pdf(file_path) if ext in SUPPORTED_PDF else []
    # Extract embedded font binaries once — same TTF set applies to every page.
    pdf_fonts: list[dict] = []
    if ext in SUPPORTED_PDF:
        try:
            from extractor import extract_embedded_fonts
            pdf_fonts = extract_embedded_fonts(file_path)
            if pdf_fonts:
                print(f"[pipeline] extracted {len(pdf_fonts)} embedded fonts: "
                      f"{', '.join(f['family'] for f in pdf_fonts)}")
        except Exception as exc:
            print(f"[pipeline] font extraction skipped: {exc}")

    for img, page_idx in pages:
        sides = []
        if is_double_sided(img):
            front, back = split_double_sided(img)
            sides = [(front, "front"), (back, "back")]
        else:
            sides = [(img, "full")]

        for side_img, side_label in sides:
            canvas_w, canvas_h = side_img.size

            # --- layout extraction: parallel ensemble + chunking ---
            # Always run Claude Vision + Surya OCR to capture high-precision graphics,
            # S3 decorators, and logos (even for PDFs).
            claude_layout = _process_side_image(side_img)
            if claude_layout is not None:
                print(f"[pipeline] layout: {len(claude_layout.get('elements', []))} elements")

            # --- logo (PDF only, used as fallback if Claude missed it) ---
            logo_info = None
            if ext in SUPPORTED_PDF and side_label in ("full", "front"):
                logo_info = detect_logo_pdf(file_path, page_idx)
                # If no embedded image found, and claude_layout didn't find one either,
                # we don't need a separate probing step because we already ran Vision!
                if logo_info is None and claude_layout is not None:
                    _logo_el = next(
                        (e for e in claude_layout.get("elements", []) if e.get("type") == "logo"),
                        None,
                    )
                    if _logo_el:
                        bd = _logo_el.get("bbox") or {}
                        x1 = max(0, int(bd.get("x", 0)))
                        y1 = max(0, int(bd.get("y", 0)))
                        x2 = min(canvas_w, int(bd.get("x", 0) + bd.get("w", 0)))
                        y2 = min(canvas_h, int(bd.get("y", 0) + bd.get("h", 0)))
                        if x2 > x1 and y2 > y1:
                            crop = side_img.crop((x1, y1, x2, y2))
                            buf = io.BytesIO()
                            crop.convert("RGB").save(buf, format="PNG")
                            logo_info = {
                                "x": float(x1), "y": float(y1),
                                "w": float(x2 - x1), "h": float(y2 - y1),
                                "image_bytes": buf.getvalue(),
                                "ext": "png",
                            }
                            print(f"[pipeline] PDF vector logo recovered via Vision: {x2-x1}×{y2-y1}px")

            # --- build outputs ---
            stem = file_stem or p.stem
            suffix = f"_p{page_idx + 1}" if len(pages) > 1 else ""
            side_suffix = f"_{side_label}" if side_label != "full" else ""
            base_name = f"{stem}{suffix}{side_suffix}"

            if ext in SUPPORTED_PDF:
                # === PDF HYBRID PATH ===
                # Text: PyMuPDF vector extraction (mathematically exact, 95%+ accuracy)
                # Graphics: Claude Vision logos/badges/S3 decorators injected on top
                raw_blocks = pdf_blocks_by_page[page_idx] if page_idx < len(pdf_blocks_by_page) else []
                if side_label == "front":
                    raw_blocks = [b for b in raw_blocks if b.x < canvas_w]
                elif side_label == "back":
                    raw_blocks = [_shift_block(b, canvas_w) for b in raw_blocks if b.x >= canvas_w]

                lines = extract_separators_pdf(
                    file_path=file_path,
                    page_idx=page_idx,
                    side_label=side_label,
                    side_canvas_w=canvas_w if side_label in ("front", "back") else None,
                )
                if not lines:
                    lines = detect_separators(side_img)

                raw_blocks = sorted(raw_blocks, key=lambda b: (round(b.y / 10) * 10, b.x))
                col_assignments = detect_columns(raw_blocks, canvas_w)
                classified = classify_blocks(raw_blocks, canvas_h=canvas_h)

                menu_data = build_menu_data(
                    classified=classified,
                    col_assignments=col_assignments,
                    source_file=stem,
                    side=side_label,
                    num_separators=len(lines),
                )
                template = build_template(
                    classified=classified,
                    col_assignments=col_assignments,
                    lines=lines,
                    canvas_w=canvas_w,
                    canvas_h=canvas_h,
                    source_file=stem,
                    page=page_idx + 1,
                    side=side_label,
                    logo_info=logo_info,
                    background_color=(
                        claude_layout.get("background_color", "#ffffff")
                        if claude_layout is not None else "#ffffff"
                    ),
                    fonts=pdf_fonts,
                )

                # Inject graphics from Claude Vision (logos, badges, S3 decorators)
                if claude_layout is not None:
                    _inject_pdf_graphics(template, claude_layout, side_img, canvas_w, canvas_h, logo_info=logo_info)
                    # Enrich vector-exact PDF separators with S3 assets identified by Claude
                    _enrich_template_separators_from_claude(template, claude_layout, side_img, canvas_w, canvas_h)
                    # Remove cross-type positional duplicates and misplaced floral ornaments
                    _cleanup_duplicate_graphics(template)

                # Dedicated decorator scan: one focused Anthropic API call to exhaustively
                # find all ornaments, separator patterns, badges, and collage boxes.
                # Runs after main extraction to catch anything still missing.
                extra_decorators = _scan_pdf_decorators_via_claude(
                    side_img, template.elements, canvas_w, canvas_h
                )
                # Dedup against existing template elements before injecting.
                # Only inject an extra decorator if it sits within 40px of an existing
                # NON-TEXT graphic element (separator/image/logo).  Text elements are
                # everywhere so using them as anchors lets ornaments land on menu content.
                _existing_graphic_centers = [
                    (
                        float((el.get("bbox") or {}).get("x", 0)) + float((el.get("bbox") or {}).get("w", 0)) / 2,
                        float((el.get("bbox") or {}).get("y", 0)) + float((el.get("bbox") or {}).get("h", 0)) / 2,
                    )
                    for el in template.elements
                    if el.get("type") in ("separator", "image", "logo")
                ]

                for dec in extra_decorators:
                    bd = dec.get("bbox") or {}
                    cx = float(bd.get("x", 0)) + float(bd.get("w", 0)) / 2
                    cy = float(bd.get("y", 0)) + float(bd.get("h", 0)) / 2
                    near_graphic = any(
                        abs(cx - gc_x) < 40 and abs(cy - gc_y) < 40
                        for gc_x, gc_y in _existing_graphic_centers
                    )
                    if near_graphic:
                        template.elements.append(dec)
                        _existing_graphic_centers.append((cx, cy))
                        print(f"[pipeline] PDF extra decorator anchored: {dec.get('semantic_label','?')}")

                # After graphics injection, reflect logo presence in menu_data
                if any(el.get("type") == "logo" for el in template.elements):
                    menu_data.logo_detected = True

            elif claude_layout is not None:
                # === IMAGE VISION PATH ===
                # Hybrid Strategy:
                # 1. Use Claude Vision for high-accuracy text labeling (the text elements)
                # 2. Use OpenCV for pixel-accurate physical shapes (lines, blobs, templates)
                # 3. Use hybrid_engine to validate shapes using text context
                
                text_elements = [e for e in claude_layout.get("elements", []) if e.get("type") == "text"]
                
                # Raw OpenCV extractions
                img_lines = detect_separators(side_img)
                # Convert RawLine objects to dicts for hybrid_engine
                cv_lines = []
                for ln in img_lines:
                    cv_lines.append({
                        "bbox": {
                            "x": min(ln.x1, ln.x2), "y": min(ln.y1, ln.y2),
                            "w": abs(ln.x2 - ln.x1) or 2.0, "h": abs(ln.y2 - ln.y1) or 2.0
                        },
                        "orientation": ln.orientation,
                        "subtype": ln.subtype or ("horizontal_line" if ln.orientation == "horizontal" else "vertical_line")
                    })
                
                from separator import detect_graphic_blobs
                cv_blobs = detect_graphic_blobs(side_img)
                matched_assets = match_badges(side_img)
                
                # Run Hybrid Engine to filter and label graphics
                hybrid_graphics = validate_graphic_elements(
                    text_elements=text_elements,
                    raw_cv_lines=cv_lines,
                    raw_cv_contours=cv_blobs,
                    matched_assets=matched_assets,
                    canvas_w=canvas_w,
                    canvas_h=canvas_h
                )
                
                # Final element list: Claude Text + Hybrid Graphics
                elements = text_elements + hybrid_graphics

                # --- Badge bbox size cap ---
                # Real badge circles on a menu are 80-200px. Cap to 220px to prevent
                # background watermarks detected as huge circles from rendering at 1000px.
                _MAX_BADGE_DIM = 220
                for el in elements:
                    if el.get("type") == "image" and el.get("subtype") == "badge":
                        bd = el.get("bbox") or {}
                        bw, bh = float(bd.get("w", 0)), float(bd.get("h", 0))
                        if bw > _MAX_BADGE_DIM or bh > _MAX_BADGE_DIM:
                            cx = float(bd.get("x", 0)) + bw / 2
                            cy = float(bd.get("y", 0)) + bh / 2
                            new_dim = min(bw, bh, _MAX_BADGE_DIM)
                            bd["w"] = new_dim; bd["h"] = new_dim
                            bd["x"] = cx - new_dim / 2; bd["y"] = cy - new_dim / 2
                            print(f"[pipeline] badge cap: {bw:.0f}×{bh:.0f} → {new_dim:.0f}px")

                # --- Drop oversized border separators (>20% of canvas = false positives) ---
                _canvas_area = canvas_w * canvas_h
                elements = [
                    el for el in elements
                    if not (
                        el.get("type") == "separator" and el.get("subtype") == "border"
                        and (el.get("bbox") or {}).get("w", 0) * (el.get("bbox") or {}).get("h", 0)
                            > _canvas_area * 0.20
                    )
                ]

                # Resolve S3 assets for both "image" and "separator" types
                # After fetching, normalize bbox to asset's natural proportions.
                for el in elements:
                    if el.get("type") in ("image", "separator") and el.get("semantic_label"):
                        s3_label = el.get("semantic_label")
                        s3_bytes = resolve_asset(s3_label)
                        if s3_bytes:
                            el["image_data"] = base64.b64encode(s3_bytes).decode()
                            _apply_s3_natural_bbox(el, s3_bytes, canvas_w, canvas_h)
                            print(f"[pipeline] S3 asset resolved: {s3_label} ({el.get('type')}/{el.get('subtype')})")

                # Crop-and-embed image elements that didn't resolve via S3
                _MAX_CROP_AREA_FRAC = 0.12  # skip crops covering >12% of canvas
                for el in elements:
                    if el.get("type") == "image" and not el.get("image_data"):
                        bd = el.get("bbox") or {}
                        ix1 = max(0, int(bd.get("x", 0)))
                        iy1 = max(0, int(bd.get("y", 0)))
                        ix2 = min(canvas_w, int(bd.get("x", 0) + bd.get("w", 0)))
                        iy2 = min(canvas_h, int(bd.get("y", 0) + bd.get("h", 0)))
                        if ix2 <= ix1 or iy2 <= iy1:
                            continue

                        crop_area = (ix2 - ix1) * (iy2 - iy1)
                        if crop_area > _canvas_area * _MAX_CROP_AREA_FRAC:
                            print(f"[pipeline] size guard: skipping oversized {el.get('subtype')} crop "
                                  f"{ix2-ix1}×{iy2-iy1}px ({100*crop_area/_canvas_area:.1f}% of canvas)")
                            continue

                        crop = side_img.crop((ix1, iy1, ix2, iy2))
                        buf = io.BytesIO()
                        crop.convert("RGB").save(buf, format="PNG")
                        el["image_data"] = base64.b64encode(buf.getvalue()).decode()
                        print(f"[pipeline] image/{el.get('subtype')} pixel crop: {ix2-ix1}×{iy2-iy1}px")
                # Convert OpenCV lines to standard separator elements and append
                for ln in img_lines:
                    x1, y1 = min(ln.x1, ln.x2), min(ln.y1, ln.y2)
                    if ln.subtype == "border":
                        # Detected as a closed rectangle — output as a border element
                        # with full w×h so the renderer can draw a box outline
                        bw = abs(ln.x2 - ln.x1)
                        bh = abs(ln.y2 - ln.y1)
                        elements.append({
                            "type": "separator",
                            "subtype": "border",
                            "orientation": "horizontal",
                            "bbox": {"x": float(x1), "y": float(y1), "w": float(bw), "h": float(bh)},
                            "style": {"color": "#000000", "stroke_width": 1.5, "stroke_style": "solid"},
                        })
                    else:
                        lw = abs(ln.x2 - ln.x1) if ln.orientation == "horizontal" else max(2.0, abs(ln.x2 - ln.x1))
                        lh = abs(ln.y2 - ln.y1) if ln.orientation == "vertical" else max(2.0, abs(ln.y2 - ln.y1))
                        # Cap stroke_width to 2.5px — contour heights/widths can be
                        # inflated by morphological dilation; real menu lines are 1-3px.
                        raw_stroke = lh if ln.orientation == "horizontal" else lw
                        stroke_w = min(float(raw_stroke), 2.5)
                        elements.append({
                            "type": "separator",
                            "subtype": "horizontal_line" if ln.orientation == "horizontal" else "vertical_line",
                            "orientation": ln.orientation,
                            "bbox": {"x": float(x1), "y": float(y1), "w": float(lw), "h": float(lh)},
                            "style": {"color": "#000000", "stroke_width": stroke_w, "stroke_style": "solid"},
                        })
                # Suppress border separators whose interior overlaps any logo or image element.
                # These are usually decorative frames around graphical elements we already crop.
                def _bd_overlap_frac(sep_bd: dict, img_bd: dict) -> float:
                    """Fraction of sep_bd area that overlaps img_bd."""
                    sx1 = sep_bd.get("x", 0); sy1 = sep_bd.get("y", 0)
                    sx2 = sx1 + sep_bd.get("w", 0); sy2 = sy1 + sep_bd.get("h", 0)
                    ix1 = img_bd.get("x", 0); iy1 = img_bd.get("y", 0)
                    ix2 = ix1 + img_bd.get("w", 0); iy2 = iy1 + img_bd.get("h", 0)
                    ox = max(0.0, min(sx2, ix2) - max(sx1, ix1))
                    oy = max(0.0, min(sy2, iy2) - max(sy1, iy1))
                    inter = ox * oy
                    sep_area = max(1.0, sep_bd.get("w", 1) * sep_bd.get("h", 1))
                    return inter / sep_area

                graphic_bds = [e.get("bbox", {}) for e in elements
                                if e.get("type") in ("logo", "image")]
                elements = [
                    el for el in elements
                    if not (
                        el.get("type") == "separator" and el.get("subtype") == "border"
                        and any(_bd_overlap_frac(el.get("bbox", {}), gbd) > 0.35
                                for gbd in graphic_bds)
                    )
                ]
                claude_layout["elements"] = elements

                num_cols = max(
                    (el.get("column", 0) for el in elements if el.get("type") == "text"),
                    default=0,
                ) + 1
                md_raw = claude_layout.get("menu_data", {})
                menu_data = build_menu_data_from_claude(
                    md_raw,
                    source_file=stem,
                    side=side_label,
                    num_separators=sum(1 for e in elements if e.get("type") == "separator"),
                    num_columns=num_cols,
                    logo_detected=any(e.get("type") == "logo" for e in elements),
                )

                # Crop logo pixels from source image for embedding in template
                logo_image_data = None
                for el in elements:
                    if el.get("type") == "logo":
                        bd = el.get("bbox") or {}
                        ex, ey, ew, eh = bd.get("x", 0), bd.get("y", 0), bd.get("w", 0), bd.get("h", 0)
                        logo_bottom = ey + eh
                        logo_right = ex + ew

                        # --- Primary: Pixel-based logo bbox refinement ---
                        # Use OpenCV to find the true ink extent of the logo graphic,
                        # eliminating heuristic expansion entirely when it succeeds.
                        refined = _refine_logo_bbox_by_pixels(side_img, bd, canvas_w, canvas_h)
                        if refined and refined["w"] > 20 and refined["h"] > 20:
                            bd.update(refined)
                            ex, ey = bd["x"], bd["y"]
                            ew, eh = bd["w"], bd["h"]
                        else:
                            # --- Fallback: Heuristic expansion ---
                            # Find nearest element directly below logo_bottom, stop 5px before it.
                            next_below_y = float("inf")
                            for other_el in elements:
                                if other_el is el: continue
                                obd = other_el.get("bbox") or {}
                                oy = float(obd.get("y", 0))
                                if oy > logo_bottom:
                                    next_below_y = min(next_below_y, oy)

                            max_extra_h = min(int(eh * 0.35), 120)
                            avail_h = max(0, int(next_below_y) - int(logo_bottom) - 5) if next_below_y < float("inf") else max_extra_h
                            extra_h = min(max_extra_h, avail_h)
                            eh_ext = eh + extra_h

                            next_right_in_band = float("inf")
                            next_left_in_band = 0.0
                            for other_el in elements:
                                if other_el is el: continue
                                obd = other_el.get("bbox") or {}
                                ox, oy, oh = float(obd.get("x", 0)), float(obd.get("y", 0)), float(obd.get("h", 1))
                                ow = float(obd.get("w", 0))
                                if oy < (ey + eh_ext) and (oy + oh) > ey:
                                    if ox > logo_right:
                                        next_right_in_band = min(next_right_in_band, ox)
                                    elif (ox + ow) < ex:
                                        next_left_in_band = max(next_left_in_band, ox + ow)

                            max_extra_w = int(ew * 2.0)
                            if next_right_in_band < float("inf"):
                                avail_w = max(0, int(next_right_in_band) - int(logo_right) + 25)
                                extra_w = min(max_extra_w, avail_w)
                            else:
                                is_centered = (ex + ew/2) > (canvas_w * 0.4) and (ex + ew/2) < (canvas_w * 0.6)
                                if is_centered:
                                    extra_w = min(max_extra_w, max(0, canvas_w - 60 - int(logo_right)))
                                else:
                                    extra_w = min(max_extra_w, max(0, canvas_w // 2 - int(logo_right)))

                            avail_left = max(0, int(ex) - int(next_left_in_band) - 5)
                            extra_left = min(int(ew * 0.30), 80, avail_left)
                            ew_ext = min(ew + extra_w + extra_left, canvas_w)

                            bd["x"] = float(ex - extra_left)
                            bd["w"], bd["h"] = float(ew_ext), float(eh_ext)
                            ex = bd["x"]
                            ew, eh = bd["w"], bd["h"]

                        x1, y1 = max(0, int(ex)), max(0, int(ey))
                        x2, y2 = min(canvas_w, int(ex + ew)), min(canvas_h, int(ey + eh))
                        
                        if x2 > x1 and y2 > y1:
                            crop = side_img.crop((x1, y1, x2, y2))
                            buf = io.BytesIO()
                            crop.convert("RGB").save(buf, format="PNG")
                            logo_image_data = base64.b64encode(buf.getvalue()).decode()
                            print(f"[pipeline] logo cropped: {x2-x1}×{y2-y1}px")
                        break

                # Enforce single primary restaurant logo — reclassify distant logos
                # (badge circles, "As seen on" panels) as image/badge so they get
                # individually pixel-cropped instead of inheriting the restaurant logo image.
                elements = _enforce_single_logo(elements)

                # Mask OpenCV separators inside logo
                elements = _mask_logo_elements(elements)
                claude_layout["elements"] = elements

                template = build_template_from_claude(
                    claude_layout,
                    source_file=stem,
                    page=page_idx + 1,
                    side=side_label,
                    canvas_w=canvas_w,
                    canvas_h=canvas_h,
                    logo_image_data=logo_image_data,
                    background_color=claude_layout.get("background_color", "#ffffff"),
                )
            else:
                # === IMAGE OCR FALLBACK ===
                # Claude Vision unavailable — fall back to Surya/basic OCR for image files.
                raw_blocks = extract_blocks_image(side_img, page_idx)
                lines = detect_separators(side_img)

                raw_blocks = sorted(raw_blocks, key=lambda b: (round(b.y / 10) * 10, b.x))
                col_assignments = detect_columns(raw_blocks, canvas_w)
                classified = classify_blocks(raw_blocks, canvas_h=canvas_h)

                menu_data = build_menu_data(
                    classified=classified,
                    col_assignments=col_assignments,
                    source_file=stem,
                    side=side_label,
                    num_separators=len(lines),
                )
                template = build_template(
                    classified=classified,
                    col_assignments=col_assignments,
                    lines=lines,
                    canvas_w=canvas_w,
                    canvas_h=canvas_h,
                    source_file=stem,
                    page=page_idx + 1,
                    side=side_label,
                    logo_info=logo_info,
                )

            menu_path = out / f"{base_name}_menu_data.json"
            tmpl_path = out / f"{base_name}_template.json"

            menu_path.write_text(menu_data.model_dump_json(indent=2))
            tmpl_path.write_text(template.model_dump_json(indent=2))

            results.append({
                "side": side_label,
                "page": page_idx + 1,
                "menu_data": str(menu_path),
                "template": str(tmpl_path),
                "num_elements": len(template.elements),
                "num_categories": len(menu_data.categories),
            })

    return results


# ---------------------------------------------------------------------------
# Phase 2 — Image chunking
# ---------------------------------------------------------------------------

_CHUNK_THRESHOLD_H = 5000  # chunk images taller than this — only extreme high-res scans trigger this


def _chunk_image(img, overlap_frac: float = 0.12):
    """
    Split img into top/bottom halves with overlap.
    Returns (top_chunk, bottom_chunk, bottom_offset_y).
    bottom_offset_y is the y-coordinate in the original image where the
    bottom chunk starts — add this to all bottom-chunk bbox y-values when merging.
    """
    w, h = img.size
    mid_y = h // 2
    overlap_px = int(h * overlap_frac)
    top = img.crop((0, 0, w, mid_y + overlap_px))
    bottom_start = max(0, mid_y - overlap_px)
    bottom = img.crop((0, bottom_start, w, h))
    return top, bottom, bottom_start


def _offset_layout_y(layout: dict, offset_y: float) -> dict:
    """Return a new layout with every bbox y-coordinate shifted by offset_y."""
    result = dict(layout)
    shifted = []
    for el in layout.get("elements", []):
        el_copy = dict(el)
        bd = dict(el.get("bbox") or {})
        bd["y"] = bd.get("y", 0) + offset_y
        el_copy["bbox"] = bd
        shifted.append(el_copy)
    result["elements"] = shifted
    return result


def _merge_chunk_layouts(top: dict | None, bottom: dict | None,
                         bottom_offset_y: float) -> dict | None:
    """Merge top/bottom chunk layouts, offsetting bottom bbox y-coordinates first."""
    if top is None and bottom is None:
        return None
    if bottom is None:
        return top
    if top is None:
        return _offset_layout_y(bottom, bottom_offset_y) if bottom else None
    return merge_layouts(top, _offset_layout_y(bottom, bottom_offset_y))


# ---------------------------------------------------------------------------
# Phase 3 — Parallel Ensemble: Surya+SoM (Precision) || Claude Vision (Holistic)
# ---------------------------------------------------------------------------

def _run_image_ensemble(img) -> dict | None:
    """
    Precision extraction: Surya OCR (pixel-accurate text positions) + Set-of-Marks
    visual prompting → Claude labels blocks and identifies missed decorative elements
    from the clean image.

    No parallel holistic pass — merging two Claude passes created ghost elements at
    wrong Y positions whenever their bboxes didn't overlap Surya blocks (IoU < 0.05).
    Single-source extraction eliminates all such ghosts.

    Falls back to Claude Vision tool_use if Surya is unavailable.
    """
    # Primary: Surya+SoM — pixel-accurate coordinates for all readable text,
    # Claude fills in decorative/script headers via dual-image prompting.
    try:
        result = extract_layout_surya_som(img)
        if result is not None:
            return result
    except Exception as e:
        print(f"[pipeline] Claude Surya+SoM failed: {e}")

    # Fallback: Claude Vision tool_use (holistic, no Surya)
    print("[pipeline] Surya+SoM unavailable — falling back to Claude Vision tool_use")
    return extract_full_layout_via_tool_use(img)


def _process_side_image(img) -> dict | None:
    """
    Full image extraction pipeline with automatic chunking and upscaling.
    Upscales very small images to ensure text is legible for OCR.
    Scales all bounding boxes back to original dimensions for pixel-perfect alignment.
    """
    orig_w, orig_h = img.size
    
    # Only upscale if image is too small for reliable OCR (<1400px height).
    # Target 1800px — enough for Surya to read small text; larger sizes hurt speed significantly.
    upscaled_img = img
    scale = 1.0
    if orig_h < 1400:
        scale = 1800 / orig_h
        new_w, new_h = int(orig_w * scale), int(orig_h * scale)
        print(f"[pipeline] upscaling small image from {orig_h}px to {new_h}px for legibility")
        upscaled_img = img.resize((new_w, new_h), Image.LANCZOS)
        
    _, h = upscaled_img.size

    layout = None
    if h > _CHUNK_THRESHOLD_H:
        print(f"[pipeline] tall image ({h}px > {_CHUNK_THRESHOLD_H}) — chunking into halves")
        top_chunk, bottom_chunk, bottom_offset_y = _chunk_image(upscaled_img)
        top_layout = _run_image_ensemble(top_chunk)
        bottom_layout = _run_image_ensemble(bottom_chunk)
        layout = _merge_chunk_layouts(top_layout, bottom_layout, bottom_offset_y)
    else:
        layout = _run_image_ensemble(upscaled_img)

    # Scale ALL bboxes back to original image dimensions
    if layout and scale != 1.0:
        inv_scale = 1.0 / scale
        for el in layout.get("elements", []):
            bd = el.get("bbox")
            if bd:
                bd["x"] = bd.get("x", 0) * inv_scale
                bd["y"] = bd.get("y", 0) * inv_scale
                bd["w"] = bd.get("w", 0) * inv_scale
                bd["h"] = bd.get("h", 0) * inv_scale
        # Scale background_color if it was a sampling coordinate (it's not, it's hex)
        
    return layout


def _shift_block(block, offset_x: float):
    """Return a new RawBlock with x shifted left by offset_x (for back-side crops)."""
    from models import RawBlock
    return RawBlock(
        text=block.text,
        x=block.x - offset_x,
        y=block.y,
        w=block.w,
        h=block.h,
        font_size=block.font_size,
        is_bold=block.is_bold,
        is_italic=block.is_italic,
        page=block.page,
        source=block.source,
    )
