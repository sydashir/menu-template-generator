"""
Core pipeline — ties extractor, separator, analyzer, and builder together.
Called by both the FastAPI app and any direct script usage.
"""

import base64
import io
import json
from pathlib import Path
from typing import Optional

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
    extract_full_layout_via_claude,
    extract_full_layout_via_tool_use,
    extract_layout_surya_som,
    merge_layouts,
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
                # Claude Vision succeeded — use its spatial data directly, skip OCR
                elements = claude_layout.get("elements", [])
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
                        x1 = max(0, int(bd.get("x", 0)))
                        y1 = max(0, int(bd.get("y", 0)))
                        x2 = min(canvas_w, int(bd.get("x", 0) + bd.get("w", 0)))
                        y2 = min(canvas_h, int(bd.get("y", 0) + bd.get("h", 0)))
                        if x2 > x1 and y2 > y1:
                            crop = side_img.crop((x1, y1, x2, y2))
                            buf = io.BytesIO()
                            crop.convert("RGB").save(buf, format="PNG")
                            logo_image_data = base64.b64encode(buf.getvalue()).decode()
                            print(f"[pipeline] logo cropped: {x2-x1}×{y2-y1}px")
                        break

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

_CHUNK_THRESHOLD_H = 1600  # chunk images taller than this (pixels)


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
    Run Surya+SoM and Claude Vision tool-use concurrently, then merge.
    Surya provides pixel-perfect coordinates (Precision Engine).
    Claude Vision fills in logos/separators Surya misses (Holistic Engine).
    Falls back to dual Claude Vision if Surya is unavailable.
    """
    from concurrent.futures import ThreadPoolExecutor

    # Try parallel ensemble (Surya+SoM + Claude Vision)
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_precision = ex.submit(extract_layout_surya_som, img)
        f_holistic = ex.submit(extract_full_layout_via_tool_use, img)
        precision = f_precision.result()
        holistic = f_holistic.result()

    if precision is not None:
        # Surya is the text backbone — only pull logos and separators from Claude
        # holistic to avoid text duplication from dual extraction.
        holistic_non_text = None
        if holistic is not None:
            non_text = [e for e in holistic.get("elements", []) if e.get("type") != "text"]
            holistic_non_text = {**holistic, "elements": non_text}
        return merge_layouts(precision, holistic_non_text, math_first=True)

    # Surya unavailable or returned nothing — fall back to dual Claude Vision
    print("[pipeline] Surya unavailable — falling back to dual Claude Vision")
    prompt_layout = extract_full_layout_via_claude(img)
    return merge_layouts(prompt_layout, holistic)  # holistic already fetched above


def _process_side_image(img) -> dict | None:
    """
    Full image extraction pipeline with automatic chunking for tall/dense menus.
    Menus taller than _CHUNK_THRESHOLD_H are split in half (with overlap),
    processed independently, then merged — preventing token truncation structurally.
    """
    _, h = img.size
    if h > _CHUNK_THRESHOLD_H:
        print(f"[pipeline] tall image ({h}px > {_CHUNK_THRESHOLD_H}) — chunking into halves")
        top_chunk, bottom_chunk, bottom_offset_y = _chunk_image(img)
        top_layout = _run_image_ensemble(top_chunk)
        bottom_layout = _run_image_ensemble(bottom_chunk)
        return _merge_chunk_layouts(top_layout, bottom_layout, bottom_offset_y)
    return _run_image_ensemble(img)


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
