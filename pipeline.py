"""
Core pipeline — ties extractor, separator, analyzer, and builder together.
Called by both the FastAPI app and any direct script usage.
"""

import json
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from extractor import (
    load_pages, extract_blocks_pdf, extract_blocks_image,
    detect_logo_pdf, is_double_sided, split_double_sided,
)
from separator import detect_separators
from analyzer import detect_columns, classify_blocks, build_menu_data
from builder import build_template
from claude_extractor import extract_menu_via_claude, build_menu_data_from_claude

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

    for img, page_idx in pages:
        sides = []
        if is_double_sided(img):
            front, back = split_double_sided(img)
            sides = [(front, "front"), (back, "back")]
        else:
            sides = [(img, "full")]

        for side_img, side_label in sides:
            canvas_w, canvas_h = side_img.size

            # --- text extraction ---
            if ext in SUPPORTED_PDF:
                pdf_blocks_by_page = extract_blocks_pdf(file_path)
                raw_blocks = pdf_blocks_by_page[page_idx] if page_idx < len(pdf_blocks_by_page) else []
                # If it was a double-sided split, filter blocks by x range
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

            # --- separator detection ---
            lines = detect_separators(side_img)

            # --- logo ---
            logo_info = None
            if ext in SUPPORTED_PDF and side_label in ("full", "front"):
                logo_info = detect_logo_pdf(file_path, page_idx)

            # Sort blocks in reading order before hierarchy analysis
            raw_blocks = sorted(raw_blocks, key=lambda b: (round(b.y / 10) * 10, b.x))

            # --- layout analysis ---
            col_assignments = detect_columns(raw_blocks, canvas_w)
            classified = classify_blocks(raw_blocks, canvas_h=canvas_h)

            # --- build outputs ---
            stem = file_stem or p.stem
            suffix = f"_p{page_idx + 1}" if len(pages) > 1 else ""
            side_suffix = f"_{side_label}" if side_label != "full" else ""
            base_name = f"{stem}{suffix}{side_suffix}"

            # For image files, prefer Claude vision if API key is set
            claude_data = None
            if ext in SUPPORTED_IMG:
                claude_data = extract_menu_via_claude(side_img)

            if claude_data is not None:
                num_cols = max(col_assignments, default=0) + 1
                menu_data = build_menu_data_from_claude(
                    claude_data,
                    source_file=p.name,
                    side=side_label,
                    num_separators=len(lines),
                    num_columns=num_cols,
                )
            else:
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
