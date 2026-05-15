"""
Core pipeline — ties extractor, separator, analyzer, and builder together.
Called by both the FastAPI app and any direct script usage.
"""

import base64
import io
import json
import re
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

# R3-1 follow-up: generic section/page-title words that should never be used as
# `restaurant_name` — applied symmetrically to BOTH the analyzer's pick and
# Claude vision's pick. Compared lowercase.
_GENERIC_TITLE_WORDS = {
    "brunch", "lunch", "dinner", "menu", "dinner menu", "brunch menu",
    "wine list", "white wines", "red wines", "le premier menu",
    "patio & bar menu", "sparkling wine", "rosé", "rose",
    # R4 additions: page-title placeholders that Claude vision returns when a
    # single page lacks the actual restaurant brand (wine pages, food-only pages, etc.).
    "wine menu", "food menu", "drinks menu", "cocktail menu",
}


def _is_generic_name(name: Optional[str]) -> bool:
    """Return True if `name` looks like a menu title / section header rather than a restaurant brand.

    R5-B: extends the exact-match `_GENERIC_TITLE_WORDS` set with two pattern
    rules — (a) the string contains the standalone word "menu" or "list",
    (b) the string contains 2+ wine-list / drink-section tokens. Closes the
    "White Wines / Red Wines Wine Menu" edge case where Claude vision returns
    a compound page-title for a wine list.
    """
    if not name:
        return True
    lc = name.strip().lower()
    if not lc:
        return True
    # Exact match against the curated set (covers "brunch", "wine menu", etc.)
    if lc in _GENERIC_TITLE_WORDS:
        return True
    # Placeholder / unknown sentinels Claude sometimes returns instead of a real name.
    if lc in {"<unknown>", "unknown", "n/a", "na", "none", "null", "tbd", "tbc"}:
        return True
    # Stripped placeholders like "Unknown" or "<UNKNOWN>" surrounded by punctuation
    if re.sub(r"[^a-z]", "", lc) in {"unknown", "na", "none", "null", "tbd", "tbc"}:
        return True
    # Contains the word "menu" or "list" as a standalone token
    if re.search(r"\bmenu\b|\blist\b", lc):
        return True
    # Contains 2+ wine-list / section-title tokens
    wine_token_count = len(re.findall(
        r"\b(wine|wines|reds?|whites?|sparkling|rosé|rose|champagne|sake|beer|cocktails?)\b",
        lc,
    ))
    if wine_token_count >= 2:
        return True
    return False


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
        # R7-C: big brand badges (Food Network / Diners' Choice) that landed in
        # the lower brand zone (after snap) get a larger 200 px target height —
        # the lower-zone placements correspond to the big gray circular variants,
        # not the small inline icons.
        # Big gray brand badges live in the BOTTOM RIGHT corner of the canvas
        # (typically x > 60% canvas_w AND y > 60% canvas_h). The small inline
        # icons inside an "As Seen On" collage_box live on the LEFT side. Both
        # carry the same semantic_label so we discriminate by position only.
        is_brand_badge = (
            sl in ("badge/food_network", "badge/opentable_diners_choice")
            and cy > canvas_h * 0.6
            and cx > canvas_w * 0.55
        )
        target_h = 200.0 if is_brand_badge else (130.0 if aspect < 1.5 else 90.0)
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


# DEPRECATED (Fix 2): second decorator scan removed from the main flow because it re-injects ornaments after cleanup with weak text-overlap validation. Kept for reference.
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
    # Only consider Claude elements that are themselves separator-type or labelled
    # as separator/ornament.  Skip collage_box elements explicitly — those are
    # multi-logo panels, not decorative dividers, and copying their semantic_label
    # onto a thin PDF separator was producing 660×44 panels with ornament/* labels.
    claude_decoratives = [
        e for e in claude_layout.get("elements", [])
        if e.get("semantic_label")
        and e.get("subtype") != "collage_box"
        and (
            e.get("type") == "separator"
            or (e.get("type") == "image" and (
                (e.get("semantic_label") or "").startswith(("separator/", "ornament/"))
            ))
        )
        and (
            e["semantic_label"].startswith("separator/")
            or e["semantic_label"].startswith("ornament/")
        )
    ]
    if not claude_decoratives:
        return

    _TOL_Y = 25   # px — was 80; tightened to avoid pulling labels across content gaps
    _TOL_X = 80   # px — was 200; tightened so wide collage_box panels don't capture thin separators

    for el in template.elements:
        if el.get("type") != "separator":
            continue
        if el.get("image_data") or el.get("semantic_label"):
            continue  # already has S3 asset

        bd = el.get("bbox") or {}
        el_w = float(bd.get("w", 0))
        el_h = float(bd.get("h", 0))
        el_cy = float(bd.get("y", 0)) + el_h / 2
        el_cx = float(bd.get("x", 0)) + el_w / 2
        # Orientation of the destination PDF separator (must match source).
        el_orient = el.get("orientation") or ("horizontal" if el_w >= el_h else "vertical")

        # Find closest Claude decorative by vertical proximity
        best = None
        best_dist = float("inf")
        for cd in claude_decoratives:
            cbd = cd.get("bbox") or {}
            cd_w = float(cbd.get("w", 0))
            cd_h = float(cbd.get("h", 0))
            cd_cy = float(cbd.get("y", 0)) + cd_h / 2
            cd_cx = float(cbd.get("x", 0)) + cd_w / 2
            # Require orientation match (or unknown source orientation).
            cd_orient = cd.get("orientation") or (
                "horizontal" if cd_w >= cd_h else "vertical"
            )
            if cd_orient != el_orient:
                continue
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
    # Any ornament image whose vertical center is within 30px of any (>=1) text
    # element is misplaced (landed inside menu content, not in a section gap).
    # NOTE: ornaments whose semantic_label starts with "separator/" are explicit
    # decorative dividers — we trust those even if a stray text line sits nearby.
    for el in els:
        if id(el) in drop_ids:
            continue
        if el.get("type") != "image" or el.get("subtype") != "ornament":
            continue
        sl = el.get("semantic_label") or ""
        if sl.startswith("separator/"):
            continue  # benefit of the doubt — labelled as a real divider
        # R19.3: narrow exemption — only exempt provenance-tagged synth
        # header flourishes, AND only when there's no body text within 30 px
        # below the ornament (else the swash would slice across an item row).
        prov = el.get("provenance") or ""
        if prov == "r19_6_synth_header_flourish":
            el_cy_tmp = _cy(el)
            el_b_tmp = el.get("bbox") or {}
            el_y2_tmp = float(el_b_tmp.get("y", 0)) + float(el_b_tmp.get("h", 0))
            body_below = [
                t for t in text_els
                if 0 < (_cy(t) - el_y2_tmp) < 30
                and (t.get("subtype") not in ("category_header",))
            ]
            if not body_below:
                continue
            # body text within 30 px below — drop the synth flourish.
            drop_ids.add(id(el))
            continue
        el_cy = _cy(el)
        nearby_text = [t for t in text_els if abs(_cy(t) - el_cy) < 30]
        if nearby_text:
            # Exempt ornaments whose ONLY upward neighbours are header-like text.
            # A swash placed under a header inevitably has the next item line
            # within 30 px below — that's normal and should not trigger a drop.
            def _is_header_like(t):
                if t.get("subtype") == "category_header":
                    return True
                style = t.get("style") or {}
                if style.get("font_family") in ("decorative-script", "display"):
                    return True
                return False
            # If at least one nearby neighbour is header-like AND the rest are
            # below the ornament (not on top of it), keep the ornament.
            header_neighbours_above = [
                t for t in nearby_text
                if _is_header_like(t) and _cy(t) < el_cy
            ]
            body_neighbours_above = [
                t for t in nearby_text
                if not _is_header_like(t) and _cy(t) < el_cy
            ]
            if header_neighbours_above and not body_neighbours_above:
                # Header above the ornament with no body text overlapping → keep.
                continue
            if all(_is_header_like(t) for t in nearby_text):
                continue
            drop_ids.add(id(el))

    # ── Fix 2b (R2-2): drop tiny unlabeled ornament fragments anywhere on page ──
    # Letter-cluster artefacts from OpenCV dilation slip past the text-proximity
    # window when they sit in vertical gaps between menu content and warning text.
    # Real ornaments are either S3-labeled (semantic_label set) or PyMuPDF
    # vector strokes (subtype != "ornament").
    for el in els:
        if id(el) in drop_ids:
            continue
        if el.get("type") != "image" or el.get("subtype") != "ornament":
            continue
        if el.get("semantic_label"):
            continue  # named ornaments are trusted
        bd = el.get("bbox") or {}
        if float(bd.get("w", 0)) < 100 and float(bd.get("h", 0)) < 40:
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

    # ── Fix 4: drop ornament/image fragments fully contained inside a logo bbox ──
    # When the logo is a script word like "Château ANNA MARIA", _inject_pdf_graphics
    # detects each letter-flourish as its own ornament. The logo image_data already
    # contains those flourishes — keeping them here re-renders them on top.
    logos = [e for e in els if e.get("type") == "logo" and id(e) not in drop_ids]
    if logos:
        def _contains(outer, inner) -> bool:
            ob = outer.get("bbox") or {}
            ib = inner.get("bbox") or {}
            ox1, oy1 = float(ob.get("x", 0)), float(ob.get("y", 0))
            ox2 = ox1 + float(ob.get("w", 0))
            oy2 = oy1 + float(ob.get("h", 0))
            ix1, iy1 = float(ib.get("x", 0)), float(ib.get("y", 0))
            ix2 = ix1 + float(ib.get("w", 0))
            iy2 = iy1 + float(ib.get("h", 0))
            # Treat as "inside" if 90%+ of the inner bbox area sits within outer.
            iw = max(0.0, min(ox2, ix2) - max(ox1, ix1))
            ih = max(0.0, min(oy2, iy2) - max(oy1, iy1))
            inter = iw * ih
            inner_area = max(1.0, (ix2 - ix1) * (iy2 - iy1))
            return inter / inner_area >= 0.9
        for el in els:
            if id(el) in drop_ids:
                continue
            if el.get("type") != "image":
                continue
            for logo in logos:
                if _contains(logo, el):
                    drop_ids.add(id(el))
                    break

    removed_in_logo = len(drop_ids) - removed_dup - removed_floral - removed_large

    if drop_ids:
        template.elements = [e for e in els if id(e) not in drop_ids]
        print(f"[cleanup] removed {removed_dup} cross-type duplicates, "
              f"{removed_floral} text-area ornaments, "
              f"{removed_large} oversized ornament crops, "
              f"{removed_in_logo} fragments inside logo "
              f"({len(template.elements)} elements remain)")

    # ── R6-2: drop empty collage_box elements (no image_data, no semantic_label) ──
    # These are noise from earlier label-clearing — they render as nothing
    # (or worse, a debug bbox) and only confuse the renderer.
    els = template.elements
    before_empty = len(els)
    els = [
        e for e in els
        if not (
            e.get("type") == "image"
            and e.get("subtype") == "collage_box"
            and not e.get("image_data")
            and not e.get("semantic_label")
        )
    ]
    removed_empty_collage = before_empty - len(els)
    if removed_empty_collage:
        template.elements = els
        print(f"[cleanup] R6-2: removed {removed_empty_collage} empty collage_box element(s) "
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


def _synthesize_header_flourishes(template, side_img, canvas_w: int, canvas_h: int, claude_layout: Optional[dict] = None) -> None:
    """
    For every category_header text element in the template, inject an S3
    `ornament/floral_swash_centered` (or `ornament/floral_swash_left` for
    left-aligned headers) image element 14 px below it. This is keyed off
    the pixel-accurate PyMuPDF text positions, not Claude's approximate
    Vision wavy-line bboxes, so it works regardless of TOL_Y matching.

    Skip if there is already an image element with image_data within 40 px
    vertically of where the flourish would go (don't double-up).

    R19.3: Only inject if (a) Claude vision saw an ornament/graphic near the
    header OR (b) an OpenCV blob match exists near the header. Also skip if
    another category_header sits within 350 px below in the same column
    (tight grids like "Add On" where the swash would slice across rows).
    Tag injected flourishes with provenance `r19_6_synth_header_flourish` so
    _cleanup_duplicate_graphics can narrowly exempt only the ones we placed.
    """
    from s3_asset_library import resolve_asset

    headers = [
        e for e in template.elements
        if e.get("type") == "text" and e.get("subtype") == "category_header"
    ]
    if not headers:
        return

    image_els = [e for e in template.elements if e.get("type") == "image"]

    # R19.3: collect Claude-vision ornaments / graphic blobs (subtype includes
    # any 'ornament', 'separator', 'decorative_*', 'wavy_line', 'swash', etc.)
    # so we can require evidence-based injection.
    claude_ornaments: list[dict] = []
    if claude_layout is not None:
        for ce in (claude_layout.get("elements", []) or []):
            sub = (ce.get("subtype") or "").lower()
            stype = (ce.get("type") or "").lower()
            if stype in ("ornament", "decorative_divider", "graphic", "image") or \
               any(k in sub for k in ("ornament", "swash", "wavy", "scroll",
                                       "diamond", "vine", "calligraph",
                                       "decorative", "separator", "flourish")):
                bd = ce.get("bbox")
                if isinstance(bd, dict):
                    claude_ornaments.append(ce)
    # OpenCV blob graphics already live in template.elements as image type
    # without semantic_label or as separator. Use those bboxes as evidence too.
    blob_evidence = [
        e for e in template.elements
        if e.get("type") in ("image", "separator")
        and not (e.get("semantic_label") or "").startswith("ornament/floral_swash")
    ]

    def _has_ornament_near(hx_c: float, hy_c: float, max_dx: float = 600, max_dy: float = 50) -> bool:
        # max_dy is asymmetric — we look ±50 px vertically around the header
        # center (sources put a wavy line right under the header text).
        for ce in claude_ornaments:
            bd = ce.get("bbox") or {}
            ex = float(bd.get("x", 0)) + float(bd.get("w", 0)) / 2
            ey = float(bd.get("y", 0)) + float(bd.get("h", 0)) / 2
            if abs(ex - hx_c) < max_dx and abs(ey - hy_c) < max_dy:
                return True
        for be in blob_evidence:
            bd = be.get("bbox") or {}
            ex = float(bd.get("x", 0)) + float(bd.get("w", 0)) / 2
            ey = float(bd.get("y", 0)) + float(bd.get("h", 0)) / 2
            if abs(ex - hx_c) < max_dx and abs(ey - hy_c) < max_dy:
                return True
        return False

    def _another_header_below(this_header: dict, max_dy: float = 350) -> bool:
        bd = this_header.get("bbox") or {}
        my_y = float(bd.get("y", 0)) + float(bd.get("h", 0))
        my_col = this_header.get("column")
        my_cx = float(bd.get("x", 0)) + float(bd.get("w", 0)) / 2
        for o in headers:
            if o is this_header:
                continue
            ob = o.get("bbox") or {}
            oy = float(ob.get("y", 0))
            ocol = o.get("column")
            ocx = float(ob.get("x", 0)) + float(ob.get("w", 0)) / 2
            # Same column (by index when present, else by x-proximity within 200 px)
            same_col = (my_col is not None and ocol is not None and my_col == ocol) or \
                       (abs(my_cx - ocx) < 200)
            if same_col and 0 < (oy - my_y) < max_dy:
                return True
        return False

    for h in headers:
        bd = h.get("bbox") or {}
        hx = float(bd.get("x", 0))
        hy = float(bd.get("y", 0))
        hw = float(bd.get("w", 0))
        hh = float(bd.get("h", 0))
        if hw <= 0 or hh <= 0:
            continue

        flourish_cy_target = hy + hh + 18 + 50  # R6-3: 50 ≈ half the swash height (100 px)
        flourish_cx = hx + hw / 2

        # Skip if an image element (any) already sits within 40 px vertical of target
        already_there = any(
            abs((float((e.get("bbox") or {}).get("y", 0))
                 + float((e.get("bbox") or {}).get("h", 0)) / 2) - flourish_cy_target) < 40
            and abs((float((e.get("bbox") or {}).get("x", 0))
                     + float((e.get("bbox") or {}).get("w", 0)) / 2) - flourish_cx) < hw
            for e in image_els
        )
        if already_there:
            continue

        # R19.3: gate 1 — require ornament evidence near the header centre.
        # Search window is the area below the header (where a swash would sit).
        hx_c = hx + hw / 2
        hy_below_c = hy + hh + 30
        if not _has_ornament_near(hx_c, hy_below_c, max_dx=max(hw * 1.5, 400), max_dy=80):
            continue

        # R19.3: gate 2 — skip when the next category_header sits within
        # 350 px below in the same column (tight section like "Add On").
        if _another_header_below(h, max_dy=350):
            continue

        # Pick centered vs left swash by text_align
        align = (h.get("style") or {}).get("text_align", "left")
        slug = "ornament/floral_swash_centered" if align == "center" else "ornament/floral_swash_left"
        png = resolve_asset(slug)
        if not png:
            continue

        import base64
        from PIL import Image as _Image
        import io as _io
        try:
            pil = _Image.open(_io.BytesIO(png))
            aspect = pil.size[0] / max(1, pil.size[1])
        except Exception:
            aspect = 6.0

        # R6-3: bump target height 60→100 and add minimum width so short headers
        # ("FRANCE", w≈80) still get a substantive swash (~280 px). y-offset
        # raised 14→18 to leave breathing room below the header baseline.
        target_h = 100.0
        target_w = target_h * aspect
        # Cap to ≤ header width × 2.5 or ≥ 280 px floor — gives short headers
        # a reasonable swash without bleeding across columns for wide headers.
        target_w = min(target_w, max(hw * 2.5, 280.0))
        target_h = target_w / aspect

        new_el = {
            "id": f"img_synth_swash_{int(hx)}_{int(hy)}",
            "type": "image",
            "subtype": "ornament",
            "semantic_label": slug,
            "provenance": "r19_6_synth_header_flourish",
            "bbox": {
                "x": max(0.0, flourish_cx - target_w / 2),
                "y": hy + hh + 18,
                "w": target_w,
                "h": target_h,
            },
            "image_data": base64.b64encode(png).decode(),
        }
        template.elements.append(new_el)
        print(f"[pipeline] synth header flourish under '{h.get('content','?')}' at y={new_el['bbox']['y']:.0f}")


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
        # Fix 3 (subtype-safe labels): a collage_box must NEVER carry an
        # ornament/* or separator/* label — those labels imply a small decorative
        # asset and break the renderer (collage_box is a full-width panel).
        # Clear the bad label rather than drop the element.
        effective_label = sl or None
        if is_collage and effective_label and effective_label.startswith(("ornament/", "separator/")):
            print(f"[pipeline] dropping bad collage_box label '{effective_label}' → None")
            effective_label = None
        hybrid_graphics.append({
            "type": "image",
            "subtype": "collage_box" if is_collage else "badge",
            "semantic_label": effective_label,
            "bbox": {
                "x": float(ce_bd.get("x", 0)), "y": float(ce_bd.get("y", 0)),
                "w": max(1.0, float(ce_bd.get("w", 0))), "h": max(1.0, float(ce_bd.get("h", 0))),
            },
        })
        print(f"[pipeline] PDF Claude {'collage_box' if is_collage else 'badge'}: {effective_label or subtype}")

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

    # R7-C: Known brand badges (Food Network, Diners' Choice) get misplaced by
    # Claude vision — it estimates them at y≈1859/2095 just below the menu items
    # when the actual placement is reserved white-space in the lower-right brand
    # zone (y≈2300-2700). Snap their y-coordinate down when Claude places them
    # inside the menu-text band. The > canvas_h * 0.15 displacement threshold
    # protects against false snapping on menus that legitimately have these
    # badges higher up.
    # Tuned against the source layout for Château-style menus:
    # Food Network gray badge occupies y ≈ 73-85% of canvas, Diners' Choice
    # gray badge occupies y ≈ 85-95%. These zones are TOP of badge → target.
    _BRAND_BADGE_LOWER_ZONE = {
        "badge/food_network":              (0.73, 0.85),
        "badge/opentable_diners_choice":   (0.85, 0.95),
    }
    for el in graphic_els:
        if el.get("subtype") != "badge":
            continue
        sl = el.get("semantic_label", "")
        if sl not in _BRAND_BADGE_LOWER_ZONE:
            continue
        bd = el.get("bbox") or {}
        current_y = float(bd.get("y", 0))
        cx = float(bd.get("x", 0)) + float(bd.get("w", 0)) / 2
        zone_min, zone_max = _BRAND_BADGE_LOWER_ZONE[sl]
        target_y_min = canvas_h * zone_min
        # Only snap large/standalone variants — small inline ones inside an
        # As-Seen-On panel live on the LEFT side (cx < 60% canvas_w) and
        # should stay where they are.
        is_right_side = cx > canvas_w * 0.55
        if is_right_side and current_y < target_y_min and (target_y_min - current_y) > canvas_h * 0.05:
            bd["y"] = target_y_min
            print(f"[pipeline] R7-C badge snap: {sl} y {current_y:.0f} → {target_y_min:.0f}")

    # R14: Auto-inject missing complement brand badge. The Château menu family
    # ALWAYS has Food Network + Diners' Choice as a stacked pair on the right.
    # Claude vision is inconsistent and sometimes returns only one of the two.
    # When we see one in the lower-right zone, synthesise the other so the
    # final render always includes both.
    _has_brand_in_lower_right = {"badge/food_network": False, "badge/opentable_diners_choice": False}
    for el in graphic_els:
        if el.get("subtype") != "badge":
            continue
        sl = el.get("semantic_label", "")
        if sl not in _has_brand_in_lower_right:
            continue
        bd = el.get("bbox") or {}
        cx = float(bd.get("x", 0)) + float(bd.get("w", 0)) / 2
        cy = float(bd.get("y", 0)) + float(bd.get("h", 0)) / 2
        if cx > canvas_w * 0.55 and cy > canvas_h * 0.6:
            _has_brand_in_lower_right[sl] = True
    _any_brand_in_lower_right = any(_has_brand_in_lower_right.values())
    if _any_brand_in_lower_right:
        for sl, present in _has_brand_in_lower_right.items():
            if present:
                continue
            # Synthesise the missing companion at its canonical zone.
            graphic_els.append({
                "type": "image",
                "subtype": "badge",
                "semantic_label": sl,
                "bbox": {  # placeholder — final size/x parked below in resize block
                    "x": canvas_w * 0.78,
                    "y": canvas_h * (0.73 if sl == "badge/food_network" else 0.85),
                    "w": 250.0,
                    "h": 250.0,
                },
            })
            print(f"[pipeline] R14 inject missing brand badge: {sl} (pair complement)")

    # R16: As-Seen-On panel complement injection. The Château family's "As seen on:"
    # collage_box (bottom-left) ALWAYS contains badges for food_network, youtube
    # and hulu. Claude vision sometimes returns only 1 or 2 of the 3. When we see
    # a collage_box at left-bottom AND at least one inline brand badge, ensure
    # the missing companions are placed inside the panel too.
    _AS_SEEN_ON_BRANDS = ("badge/food_network", "badge/youtube", "badge/hulu")
    panel = None
    for el in graphic_els:
        if el.get("type") == "image" and el.get("subtype") == "collage_box":
            bd = el.get("bbox") or {}
            cx = float(bd.get("x", 0)) + float(bd.get("w", 0)) / 2
            cy = float(bd.get("y", 0)) + float(bd.get("h", 0)) / 2
            if cx < canvas_w * 0.55 and cy > canvas_h * 0.55:
                panel = (el, bd)
                break
    if panel is not None:
        panel_el, panel_bd = panel
        panel_x = float(panel_bd.get("x", 0))
        panel_y = float(panel_bd.get("y", 0))
        panel_w = float(panel_bd.get("w", 200))
        panel_h = float(panel_bd.get("h", 200))
        # Which brands are already inside (or near) the panel?
        present_inline_els: dict[str, dict] = {}
        for el in graphic_els:
            if el.get("subtype") != "badge":
                continue
            sl = el.get("semantic_label", "")
            if sl not in _AS_SEEN_ON_BRANDS:
                continue
            bd = el.get("bbox") or {}
            ex = float(bd.get("x", 0)) + float(bd.get("w", 0)) / 2
            ey = float(bd.get("y", 0)) + float(bd.get("h", 0)) / 2
            # Treat as "inline panel" if x is in the left zone (not the big-gray right zone).
            if ex < canvas_w * 0.55:
                present_inline_els[sl] = el
        present_inline = set(present_inline_els)
        # Inject missing brands as inline-sized (90×90) within the panel bounds.
        missing = [b for b in _AS_SEEN_ON_BRANDS if b not in present_inline]
        if missing and present_inline:  # only fire if at least one was present (proves it's an As-Seen-On menu)
            # R19.5: real row-layout. Allocate one slot per badge across the
            # panel width; centre the badge inside each slot. No more vertical
            # stack with i*5 offsets that overlap when more than one is missing.
            total = len(missing) + len(present_inline)
            slot_w = panel_w / max(total, 1)
            size = 90.0
            for i, sl in enumerate(missing):
                slot_idx = len(present_inline) + i
                slot_x = panel_x + slot_idx * slot_w + (slot_w - size) / 2
                slot_y = panel_y + (panel_h - size) / 2
                graphic_els.append({
                    "type": "image",
                    "subtype": "badge",
                    "semantic_label": sl,
                    "bbox": {"x": slot_x, "y": slot_y, "w": size, "h": size},
                })
                print(f"[pipeline] R16 inject missing as-seen-on badge: {sl}")

        # R19.5: per-panel resolver. After injection, sort ALL badges whose
        # centre lives inside the panel bbox by x and lay them out evenly in
        # a single row. This corrects Claude bboxes with bogus aspect ratios
        # (e.g. badge/youtube reported as 216×90 inside a 90×90 slot) and the
        # overlap that follows.
        # Force square 90×90 for the brand badges that ship square in source.
        # Leave badge/youtube at its natural aspect (it really is wide).
        _SQUARE_BRANDS = {
            "badge/food_network",
            "badge/hulu",
            "badge/best_of",
            "badge/opentable_diners_choice",
        }
        panel_x2 = panel_x + panel_w
        panel_y2 = panel_y + panel_h
        in_panel: list[dict] = []
        for el in graphic_els:
            if el.get("subtype") != "badge":
                continue
            sl = el.get("semantic_label", "")
            if sl not in _AS_SEEN_ON_BRANDS:
                continue
            bd = el.get("bbox") or {}
            cx_e = float(bd.get("x", 0)) + float(bd.get("w", 0)) / 2
            cy_e = float(bd.get("y", 0)) + float(bd.get("h", 0)) / 2
            if panel_x <= cx_e <= panel_x2 and panel_y <= cy_e <= panel_y2:
                in_panel.append(el)
        if in_panel:
            in_panel.sort(key=lambda e: float((e.get("bbox") or {}).get("x", 0)))
            n = len(in_panel)
            slot_w = panel_w / n
            for idx, el in enumerate(in_panel):
                sl = el.get("semantic_label", "")
                bd = el.get("bbox") or {}
                # Force square for the listed brands; keep natural aspect otherwise.
                if sl in _SQUARE_BRANDS:
                    bd["w"] = 90.0
                    bd["h"] = 90.0
                else:
                    # Cap height at 90 and keep stored aspect.
                    cur_w = float(bd.get("w", 90)) or 90.0
                    cur_h = float(bd.get("h", 90)) or 90.0
                    asp = cur_w / max(1.0, cur_h)
                    bd["h"] = 90.0
                    bd["w"] = 90.0 * asp
                bw = float(bd["w"])
                bh = float(bd["h"])
                # Centre badge in its slot horizontally; centre vertically in panel.
                bd["x"] = panel_x + idx * slot_w + (slot_w - bw) / 2
                bd["y"] = panel_y + (panel_h - bh) / 2
                el["bbox"] = bd
            print(f"[pipeline] R19.5 panel resolver: laid out {n} as-seen-on badges in row")

    # S3 asset resolution (wavy lines, ornaments, known badge PNGs)
    # After fetching, normalize bbox to the asset's natural proportions.
    # R8-fix: BIG brand badges in the lower-right zone (Food Network / Diners'
    # Choice gray standalone variants) should NOT use the small-colored S3 asset
    # — those variants are visually different (large + gray + decorative ring).
    # Skip S3 for those and let the pixel-crop fallback below capture the actual
    # gray pixels from the source PDF.
    _BRAND_LABELS = {"badge/food_network", "badge/opentable_diners_choice"}
    for el in graphic_els:
        sl = el.get("semantic_label")
        if not sl:
            continue
        bd = el.get("bbox") or {}
        cx = float(bd.get("x", 0)) + float(bd.get("w", 0)) / 2
        cy = float(bd.get("y", 0)) + float(bd.get("h", 0)) / 2
        # Brand-badge in lower-right zone → skip S3, use pixel crop from source.
        # ALSO enforce the standalone big-gray badge size (~250 px) AND park the
        # bbox at the canonical zone position rather than trusting Claude's
        # reported y (Claude often reports the badge inside the menu-text band).
        if sl in _BRAND_LABELS and cy > canvas_h * 0.6 and cx > canvas_w * 0.55:
            # Source badges are ~350 px wide on a 2200-wide canvas. Scale with
            # canvas width so this works for other dimensions too.
            target = max(200.0, min(380.0, float(canvas_w) * 0.16))
            zone_min, zone_max = _BRAND_BADGE_LOWER_ZONE.get(sl, (0.7, 0.9))
            # Park badge top at zone_min Y, right-aligned with ~50 px margin from
            # the right edge of canvas (that's where source has them).
            badge_y = canvas_h * zone_min
            badge_x = max(0.0, float(canvas_w) - target - 60.0)
            bd["w"] = target
            bd["h"] = target
            bd["x"] = badge_x
            bd["y"] = badge_y
            print(f"[pipeline] PDF brand-badge zone-park: {sl} → ({badge_x:.0f},{badge_y:.0f}) {target:.0f}×{target:.0f}")
            continue
        s3_bytes = resolve_asset(sl)
        if s3_bytes:
            el["image_data"] = base64.b64encode(s3_bytes).decode()
            _apply_s3_natural_bbox(el, s3_bytes, canvas_w, canvas_h)
            print(f"[pipeline] PDF S3 asset: {sl}")

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

    # R2-2: Drop tiny unlabeled ornament crops — these are dilated letter blobs from
    # warning text / fine print, not real decorators. Real ornaments are either
    # S3-labeled or come from PyMuPDF vector strokes.
    graphic_els = [
        el for el in graphic_els
        if not (
            el.get("type") == "image"
            and el.get("subtype") == "ornament"
            and not el.get("semantic_label")
            and float((el.get("bbox") or {}).get("w", 0)) < 100
            and float((el.get("bbox") or {}).get("h", 0)) < 40
        )
    ]

    # R7-A: Multi-logo crop — iterate every logo element and build a
    # {logo_index: image_data} dict so each LogoElement can carry its own crop.
    # Backward-compat `logo_image_data` (single) still set from the first logo
    # so older callers / image-path code paths keep working.
    logo_image_data: Optional[str] = None
    logo_image_data_by_idx: dict[int, str] = {}
    for el in graphic_els:
        if el.get("type") != "logo":
            continue
        idx = int(el.get("logo_index", 0))
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
            data_b64 = base64.b64encode(buf.getvalue()).decode()
            logo_image_data_by_idx[idx] = data_b64
            if logo_image_data is None:
                logo_image_data = data_b64
            print(f"[pipeline] PDF logo #{idx} cropped: {x2-x1}×{y2-y1}px")

    graphic_els = _enforce_single_logo(graphic_els)

    # R2-3: final scrub — no collage_box may carry an ornament/* or separator/* label,
    # regardless of which code path assigned it.
    for el in graphic_els:
        if el.get("type") == "image" and el.get("subtype") == "collage_box":
            sl = el.get("semantic_label") or ""
            if sl.startswith("ornament/") or sl.startswith("separator/"):
                print(f"[pipeline] scrub bad collage_box label: {sl} → None")
                el["semantic_label"] = None
                # Also nullify image_data since it was fetched for the wrong asset
                if el.get("image_data") and sl:
                    # Asset was an ornament PNG stretched into a collage strip — drop it
                    # so the renderer falls back to the source-image pixel crop or nothing.
                    el["image_data"] = None

    # Build validated model instances and append to the existing template element list
    tmp = build_template_from_claude(
        {"elements": graphic_els, "background_color": "#ffffff"},
        source_file="",
        canvas_w=canvas_w,
        canvas_h=canvas_h,
        logo_image_data=logo_image_data,
        logo_image_data_dict=logo_image_data_by_idx or None,
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
                # R7-defensive: normalise every bbox value + column to numeric types.
                # Claude vision occasionally emits strings inside the bbox dict
                # ("y": "100") which crash later sort keys with str/int comparison.
                for _el in claude_layout.get("elements", []) or []:
                    _bd = _el.get("bbox")
                    if isinstance(_bd, dict):
                        for _k in ("x", "y", "w", "h"):
                            try:
                                _bd[_k] = float(_bd.get(_k, 0) or 0)
                            except (TypeError, ValueError):
                                _bd[_k] = 0.0
                    _col = _el.get("column")
                    if _col is not None and not isinstance(_col, int):
                        try:
                            _el["column"] = int(_col)
                        except (TypeError, ValueError):
                            _el["column"] = 0

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
                classified = classify_blocks(raw_blocks, canvas_h=canvas_h, canvas_w=canvas_w)

                # R4-1: Cross-validate analyzer's category_header decisions against Claude vision's
                # category list. Claude has full visual context (knows what's a section header vs an
                # item) while the analyzer only sees per-block font/position. Two-way correction:
                # (a) demote analyzer's category_header to item_name when Claude doesn't recognize
                #     it AND the block isn't script-font (script font = trust analyzer)
                # (b) promote analyzer's item_name to category_header when Claude DID list it
                if claude_layout and claude_layout.get("menu_data"):
                    claude_cat_names = set()
                    for c in claude_layout["menu_data"].get("categories", []) or []:
                        nm = (c.get("name") or "").strip().lower()
                        if nm:
                            claude_cat_names.add(nm)

                    def _is_script_font(blk) -> bool:
                        raw = (getattr(blk, "font_family_raw", "") or "").lower()
                        if blk.font_family in ("decorative-script", "display"):
                            return True
                        if any(k in raw for k in ("signature", "script", "vibes", "brittany", "calligraph", "cursive")):
                            return True
                        return False

                    reclassified = []
                    upgrades = downgrades = 0
                    for block, sem in classified:
                        text_lc = block.text.strip().lower()
                        # Strong match: exact or near-exact equality. The earlier
                        # `cn in text_lc` substring path was too permissive — e.g.
                        # cn='breakfast' matched 'the chateau breakfast 15' and
                        # kept an item promoted to a category. Now require
                        # exact match OR substring with similar lengths (±50%)
                        # so that 'BORDEAUX, FRANCE (Left Bank)' still matches
                        # cn='bordeaux france (left bank)' but a long-item-name
                        # containing a short cat-name doesn't.
                        def _close(a: str, b: str) -> bool:
                            # a = analyzer text (lowercase), b = Claude category (lowercase).
                            # Asymmetric:
                            # • exact match → safe
                            # • a is a substring of b → analyzer text is a partial of the
                            #   Claude category name (e.g. analyzer "france" vs Claude
                            #   "bordeaux, france (left bank)") → safe, treat as a match.
                            # • b is a substring of a → Claude category embedded inside a
                            #   longer analyzer string (e.g. Claude "breakfast" vs analyzer
                            #   "the chateau breakfast 15") → DANGEROUS, would falsely
                            #   promote an item to a category. Only allow when Claude's
                            #   substring is "most of" the analyzer text (≥75%).
                            if a == b:
                                return True
                            if a in b:
                                return True
                            if b in a and len(b) >= len(a) * 0.75:
                                return True
                            return False
                        in_claude = text_lc in claude_cat_names or any(
                            _close(text_lc, cn) for cn in claude_cat_names
                        )
                        new_sem = sem
                        if sem == "category_header" and not in_claude and not _is_script_font(block):
                            new_sem = "item_name"
                            downgrades += 1
                        elif sem in ("item_name", "other_text") and in_claude and len(text_lc) >= 3:
                            new_sem = "category_header"
                            upgrades += 1
                        reclassified.append((block, new_sem))
                    if upgrades or downgrades:
                        print(f"[pipeline] R4-1 reclassify: {upgrades} promoted, {downgrades} demoted (claude_cats={len(claude_cat_names)})")
                    classified = reclassified

                menu_data = build_menu_data(
                    classified=classified,
                    col_assignments=col_assignments,
                    source_file=stem,
                    side=side_label,
                    num_separators=len(lines),
                )

                # R3-1: If analyzer didn't assign a restaurant_name (or assigned one that looks
                # like a section header), fall back to Claude vision's menu_data.restaurant_name.
                # Reason: PDF logos are graphics (no extractable text), so the analyzer often
                # picks the biggest header text instead. Claude's vision pass reads the logo image.
                if claude_layout is not None:
                    claude_md = claude_layout.get("menu_data", {}) or {}
                    claude_rn = (claude_md.get("restaurant_name") or "").strip()
                    current = (menu_data.restaurant_name or "").strip() if menu_data.restaurant_name else ""

                    # Names of detected category headers (so we don't accept "White Wines" as restaurant)
                    header_names = {
                        (c.name or "").strip().lower() for c in menu_data.categories
                    }
                    current_lc = current.lower()
                    claude_rn_lc = claude_rn.lower()

                    # R5-B: use the smarter _is_generic_name() helper so compound titles
                    # like "White Wines / Red Wines Wine Menu" are treated as generic too.
                    should_use_claude = (
                        claude_rn
                        and not _is_generic_name(claude_rn)                 # Claude pick must be real
                        and claude_rn_lc not in header_names                # not a section header
                        and (
                            not current                                     # analyzer found nothing
                            or current_lc in header_names                   # analyzer picked a category
                            or _is_generic_name(current)                    # analyzer picked a generic title
                        )
                    )
                    if should_use_claude:
                        print(f"[pipeline] R3-1: restaurant_name '{current}' → '{claude_rn}' (Claude vision)")
                        menu_data.restaurant_name = claude_rn

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
                    # R2-1: synthesise header flourishes directly under category_header
                    # text elements — uses pixel-accurate PyMuPDF text positions so it
                    # works even when Claude Vision's wavy-line bboxes are too far off
                    # for _enrich_template_separators_from_claude's tight TOL_Y to match.
                    _synthesize_header_flourishes(template, side_img, canvas_w, canvas_h, claude_layout=claude_layout)
                    # R2-1: drop unlabelled decorative_divider separators left behind —
                    # they render as empty bbox outlines because they never got an
                    # image_data or semantic_label assignment from enrichment.
                    template.elements = [
                        e for e in template.elements
                        if not (
                            e.get("type") == "separator"
                            and e.get("subtype") == "decorative_divider"
                            and not e.get("image_data")
                            and not e.get("semantic_label")
                        )
                    ]
                    # Remove cross-type positional duplicates and misplaced floral ornaments
                    _cleanup_duplicate_graphics(template)

                    # R17: HAPPY HOUR decorative-box injection. Bar & Patio source
                    # has a HAPPY HOUR sun-burst graphic at the bottom-right with
                    # decorative wordmark. Claude vision extracts the inner text
                    # ("DAILY", "3-5PM", "BAR menu", "$7 select wines"…) but skips
                    # the wordmark itself. Detect the cluster and inject a pixel-
                    # crop image element covering the surrounding decorative box.
                    _hh_kw = ("daily", "3-5pm", "3-5 pm", "happy hour")
                    _hh_strong = ("happy hour",)
                    hh_text_els = []
                    for el in template.elements:
                        if el.get("type") != "text":
                            continue
                        # Skip restaurant_name / page-title elements (they often
                        # contain words like "bar menu" that aren't happy-hour markers).
                        if el.get("subtype") == "restaurant_name":
                            continue
                        c = (el.get("content") or "").lower().strip()
                        if any(k in c for k in _hh_kw):
                            hh_text_els.append(el)
                    # Spatial cluster check: keep only matches that are within
                    # 600 px of each other (the HAPPY HOUR box is compact).
                    if len(hh_text_els) >= 2:
                        # Find the densest cluster — keep elements within 600 px
                        # of the median y. If 'happy hour' itself appears,
                        # always include it as the anchor.
                        ys_all = [float(e["bbox"]["y"]) for e in hh_text_els]
                        median_y = sorted(ys_all)[len(ys_all) // 2]
                        hh_text_els = [e for e in hh_text_els if abs(float(e["bbox"]["y"]) - median_y) < 600]
                    if len(hh_text_els) >= 2:
                        xs = [(e["bbox"]["x"], e["bbox"]["x"] + e["bbox"]["w"]) for e in hh_text_els]
                        ys = [(e["bbox"]["y"], e["bbox"]["y"] + e["bbox"]["h"]) for e in hh_text_els]
                        # Pad generously on the LEFT to include the stylized
                        # "HAPPY HOUR" sun-burst wordmark (sits to the left of
                        # the inner content text in the source).
                        # R19.2: 260 left pad cropped the leading "H" of "HAPPY".
                        # Bump to 420; sun-rays also extend above so y-top → 100.
                        # Clamp to the right half of the canvas so the wider
                        # left-pad doesn't bleed into item-name text.
                        cluster_x1 = min(x[0] for x in xs) - 420
                        cluster_x2 = max(x[1] for x in xs) + 30
                        cluster_y1 = min(y[0] for y in ys) - 100
                        cluster_y2 = max(y[1] for y in ys) + 40
                        # R19.2: clamp to right half of canvas so the wider
                        # left-pad never captures menu item text in the centre/left.
                        cluster_x1 = max(int(canvas_w * 0.45), int(cluster_x1))
                        cluster_x1 = max(0, int(cluster_x1))
                        cluster_y1 = max(0, int(cluster_y1))
                        cluster_x2 = min(canvas_w, int(cluster_x2))
                        cluster_y2 = min(canvas_h, int(cluster_y2))
                        if cluster_x2 > cluster_x1 + 50 and cluster_y2 > cluster_y1 + 50:
                            # Verify cluster sits in lower-right or lower portion of canvas
                            cx = (cluster_x1 + cluster_x2) / 2
                            cy = (cluster_y1 + cluster_y2) / 2
                            if cy > canvas_h * 0.5:
                                try:
                                    crop = side_img.crop((cluster_x1, cluster_y1, cluster_x2, cluster_y2))
                                    buf = io.BytesIO()
                                    crop.convert("RGB").save(buf, format="PNG")
                                    import hashlib as _hashlib
                                    hh_id = f"img_hhbox_{_hashlib.md5(f'hh_{cluster_x1}_{cluster_y1}'.encode()).hexdigest()[:8]}"
                                    template.elements.insert(0, {
                                        "id": hh_id,
                                        "type": "image",
                                        "subtype": "collage_box",
                                        "semantic_label": None,
                                        "bbox": {
                                            "x": float(cluster_x1), "y": float(cluster_y1),
                                            "w": float(cluster_x2 - cluster_x1),
                                            "h": float(cluster_y2 - cluster_y1),
                                        },
                                        "image_data": base64.b64encode(buf.getvalue()).decode(),
                                    })
                                    print(f"[pipeline] R17: injected HAPPY HOUR box pixel crop "
                                          f"({cluster_x1},{cluster_y1}) {cluster_x2-cluster_x1}×{cluster_y2-cluster_y1}")
                                except Exception as exc:
                                    print(f"[pipeline] R17 happy-hour inject failed: {exc}")

                # Decorator scan removed (Fix 2 — Option A): the second Anthropic
                # decorator scan re-injected ornaments AFTER _cleanup_duplicate_graphics
                # had already dropped them, anchored only to other (possibly misplaced)
                # graphics. The extract_layout_surya_som pipeline is now the single
                # source of decorators. Function definition is kept (deprecated) for
                # callers that may still want a focused scan, but it is NOT invoked here.

                # After graphics injection, reflect logo presence in menu_data
                if any(el.get("type") == "logo" for el in template.elements):
                    menu_data.logo_detected = True

            elif claude_layout is not None:
                # === IMAGE VISION PATH ===
                # Hybrid Strategy:
                # 1. Use Claude Vision for high-accuracy text labeling (the text elements)
                # 2. Use OpenCV for pixel-accurate physical shapes (lines, blobs, templates)
                # 3. Use hybrid_engine to validate shapes using text context
                
                # Normalise bbox values to float on every Claude element BEFORE
                # they reach any downstream code (hybrid_engine, sort keys, etc.).
                # Claude vision occasionally returns string values inside bbox dicts
                # which crash str/int comparisons (e.g. SRQ_BRUNCH_MENUS.png).
                def _coerce_bbox_inplace(elements_list):
                    for _el in elements_list or []:
                        _bd = _el.get("bbox")
                        if isinstance(_bd, dict):
                            for _k in ("x", "y", "w", "h"):
                                try:
                                    _bd[_k] = float(_bd.get(_k, 0) or 0)
                                except (TypeError, ValueError):
                                    _bd[_k] = 0.0
                        _col = _el.get("column")
                        if _col is not None and not isinstance(_col, int):
                            try:
                                _el["column"] = int(_col)
                            except (TypeError, ValueError):
                                _el["column"] = 0
                _coerce_bbox_inplace(claude_layout.get("elements", []))

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

                def _safe_col(v):
                    try:
                        return int(v) if v is not None else 0
                    except (TypeError, ValueError):
                        return 0
                num_cols = max(
                    (_safe_col(el.get("column", 0)) for el in elements if el.get("type") == "text"),
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

                # R7-extra: reject any "logo" element whose bbox is > 35% of the canvas
                # area. Claude vision occasionally returns the entire menu image as a
                # single logo on sparse / unusual layouts (e.g. EARLY BIRD MENU SRQ_2,
                # kidsthanksgiving_menu). Without this check the runaway logo image
                # gets drawn over every menu item by the renderer.
                _canvas_area = max(1.0, float(canvas_w) * float(canvas_h))
                pre_filter = elements
                kept = []
                dropped_logos = 0
                for el in pre_filter:
                    if el.get("type") == "logo":
                        bd = el.get("bbox") or {}
                        a = float(bd.get("w", 0)) * float(bd.get("h", 0))
                        if a > _canvas_area * 0.35:
                            dropped_logos += 1
                            print(f"[pipeline] drop runaway logo bbox "
                                  f"{bd.get('w', 0):.0f}×{bd.get('h', 0):.0f} "
                                  f"({100 * a / _canvas_area:.1f}% of canvas)")
                            continue
                    kept.append(el)
                if dropped_logos:
                    elements = kept

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

                # R6-2 (image-path defensive backport): drop empty collage_box
                # elements that have neither image_data nor semantic_label — they
                # render as nothing (or a debug bbox) and only confuse the renderer.
                template.elements = [
                    el for el in template.elements
                    if not (
                        el.get("type") == "image"
                        and el.get("subtype") == "collage_box"
                        and not el.get("image_data")
                        and not el.get("semantic_label")
                    )
                ]

                # R9-image: drop unlabelled ornament elements that aren't clearly
                # in the page margin. OpenCV graphic-blob detection in the image
                # branch over-fires on letter clusters and produces pixel crops
                # of partial text (e.g. group_menu showed 95+ phantom "PPE" /
                # "ENTR" fragments). Real decorations on these menus live in
                # the outer 12% of the page (left/right margins). Anything
                # unlabelled in the content area is noise.
                _text_centers = [
                    (
                        float((e.get("bbox") or {}).get("x", 0)) + float((e.get("bbox") or {}).get("w", 0)) / 2,
                        float((e.get("bbox") or {}).get("y", 0)) + float((e.get("bbox") or {}).get("h", 0)) / 2,
                    )
                    for e in template.elements if e.get("type") == "text"
                ]
                kept = []
                dropped = 0
                for el in template.elements:
                    if el.get("type") == "image" and el.get("subtype") == "ornament" and not el.get("semantic_label"):
                        bd = el.get("bbox") or {}
                        w = float(bd.get("w", 0))
                        h = float(bd.get("h", 0))
                        x = float(bd.get("x", 0))
                        y = float(bd.get("y", 0))
                        cx = x + w / 2
                        cy = y + h / 2
                        # Margin zone: leftmost / rightmost 10% of canvas, OR top / bottom 6%.
                        in_left_margin   = cx < canvas_w * 0.10
                        in_right_margin  = cx > canvas_w * 0.90
                        in_top_margin    = cy < canvas_h * 0.06
                        in_bottom_margin = cy > canvas_h * 0.94
                        in_margin = in_left_margin or in_right_margin or in_top_margin or in_bottom_margin
                        # Always allow LARGE ornaments (real decorative banners > 25% canvas wide)
                        is_large = w > canvas_w * 0.25 or h > canvas_h * 0.20
                        # Drop tiny ones outright.
                        if w < 100 and h < 40:
                            dropped += 1
                            continue
                        if not in_margin and not is_large:
                            # Content-area unlabelled ornament — drop.
                            dropped += 1
                            continue
                        # Even in margin, drop if directly overlapping a text element.
                        if in_margin and any(abs(cx - tx) < 80 and abs(cy - ty) < 40 for tx, ty in _text_centers):
                            dropped += 1
                            continue
                    kept.append(el)
                if dropped:
                    print(f"[pipeline] R9-image: dropped {dropped} unlabelled ornament fragments (content-area or tiny)")
                    template.elements = kept
            else:
                # === IMAGE OCR FALLBACK ===
                # Claude Vision unavailable — fall back to Surya/basic OCR for image files.
                raw_blocks = extract_blocks_image(side_img, page_idx)
                lines = detect_separators(side_img)

                raw_blocks = sorted(raw_blocks, key=lambda b: (round(b.y / 10) * 10, b.x))
                col_assignments = detect_columns(raw_blocks, canvas_w)
                classified = classify_blocks(raw_blocks, canvas_h=canvas_h, canvas_w=canvas_w)

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

    # R4-2: Cross-page restaurant_name propagation. Each page is processed in
    # isolation, so wine-list page 2 might end up with "Wine Menu" while page 1
    # correctly identifies "The Château Anna Maria". Find the first non-generic
    # restaurant_name across all pages and apply it to any page that's missing one
    # or carries a generic placeholder.
    if len(results) > 1:
        canonical_name = None
        for r in results:
            try:
                with open(r["menu_data"]) as f:
                    md = json.load(f)
                rn = (md.get("restaurant_name") or "").strip()
                # R5-B: use _is_generic_name() instead of exact-match set lookup.
                if rn and not _is_generic_name(rn):
                    canonical_name = rn
                    break
            except Exception:
                continue
        if canonical_name:
            for r in results:
                try:
                    with open(r["menu_data"]) as f:
                        md = json.load(f)
                    rn = (md.get("restaurant_name") or "").strip()
                    # R5-B: use _is_generic_name() so compound page-titles get overwritten.
                    if _is_generic_name(rn):
                        md["restaurant_name"] = canonical_name
                        with open(r["menu_data"], "w") as f:
                            json.dump(md, f, indent=2)
                        print(f"[pipeline] R4-2: propagated restaurant_name '{canonical_name}' → page {r.get('page')}")
                except Exception as exc:
                    print(f"[pipeline] R4-2 propagation skipped for {r}: {exc}")

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
