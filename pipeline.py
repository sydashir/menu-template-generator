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

load_dotenv()

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
    _mask_logo_elements,
    _refine_logo_bbox_by_pixels,
)

SUPPORTED_PDF = {".pdf"}
SUPPORTED_IMG = {".jpg", ".jpeg", ".png", ".webp"}


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

    for img, page_idx in pages:
        sides = []
        if is_double_sided(img):
            front, back = split_double_sided(img)
            sides = [(front, "front"), (back, "back")]
        else:
            sides = [(img, "full")]

        for side_img, side_label in sides:
            canvas_w, canvas_h = side_img.size

            # --- image extraction: parallel ensemble + chunking ---
            # Images only — PDFs use PyMuPDF vector extraction (never Claude Vision).
            claude_layout = None
            if ext in SUPPORTED_IMG:
                claude_layout = _process_side_image(side_img)
                if claude_layout is not None:
                    print(f"[pipeline] layout: {len(claude_layout.get('elements', []))} elements")

            # --- logo (PDF only, used as fallback if Claude missed it) ---
            logo_info = None
            if ext in SUPPORTED_PDF and side_label in ("full", "front"):
                logo_info = detect_logo_pdf(file_path, page_idx)
                # If no embedded image found, the logo may be vector-drawn.
                # Run Surya+SoM on the rendered page to detect it visually.
                if logo_info is None:
                    print("[pipeline] PDF: no embedded logo found — probing render for vector logo")
                    _pdf_vision = _process_side_image(side_img)
                    if _pdf_vision is not None:
                        _logo_el = next(
                            (e for e in _pdf_vision.get("elements", []) if e.get("type") == "logo"),
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

            if claude_layout is not None:
                # Claude Vision succeeded — use its spatial data directly for text/logos.
                # Use OpenCV detect_separators for EXACT separator coordinates, ignoring Claude's.
                img_lines = detect_separators(side_img)
                # Filter out Claude's separator elements — OpenCV gives exact coordinates
                elements = [e for e in claude_layout.get("elements", []) if e.get("type") != "separator"]
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
                claude_layout["elements"] = elements

                num_cols = max(
                    (el.get("column", 0) for el in elements if el.get("type") == "text"),
                    default=0,
                ) + 1
                md_raw = claude_layout.get("menu_data", {})
                menu_data = build_menu_data_from_claude(
                    md_raw,
                    source_file=p.name,
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

                # Mask OpenCV separators inside logo
                elements = _mask_logo_elements(elements)
                claude_layout["elements"] = elements

                template = build_template_from_claude(
                    claude_layout,
                    source_file=p.name,
                    page=page_idx + 1,
                    side=side_label,
                    canvas_w=canvas_w,
                    canvas_h=canvas_h,
                    logo_image_data=logo_image_data,
                    background_color=claude_layout.get("background_color", "#ffffff"),
                )
            else:
                # Fallback: OCR (images) or PDF vector extraction
                if ext in SUPPORTED_PDF:
                    raw_blocks = pdf_blocks_by_page[page_idx] if page_idx < len(pdf_blocks_by_page) else []
                    if side_label == "front":
                        raw_blocks = [b for b in raw_blocks if b.x < canvas_w]
                    elif side_label == "back":
                        raw_blocks = [
                            b._replace(x=b.x - canvas_w) if hasattr(b, "_replace")
                            else _shift_block(b, canvas_w)
                            for b in raw_blocks if b.x >= canvas_w
                        ]
                else:
                    raw_blocks = extract_blocks_image(side_img, page_idx)

                if ext in SUPPORTED_PDF:
                    lines = extract_separators_pdf(
                        file_path=file_path,
                        page_idx=page_idx,
                        side_label=side_label,
                        side_canvas_w=canvas_w if side_label in ("front", "back") else None,
                    )
                    if not lines:
                        lines = detect_separators(side_img)
                else:
                    lines = detect_separators(side_img)

                raw_blocks = sorted(raw_blocks, key=lambda b: (round(b.y / 10) * 10, b.x))
                col_assignments = detect_columns(raw_blocks, canvas_w)
                classified = classify_blocks(raw_blocks, canvas_h=canvas_h)

                menu_data = build_menu_data(
                    classified=classified,
                    col_assignments=col_assignments,
                    source_file=p.name,
                    side=side_label,
                    num_separators=len(lines),
                )
                template = build_template(
                    classified=classified,
                    col_assignments=col_assignments,
                    lines=lines,
                    canvas_w=canvas_w,
                    canvas_h=canvas_h,
                    source_file=p.name,
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
