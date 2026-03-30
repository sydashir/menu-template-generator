import base64
import hashlib
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from models import (
    BBox, TextStyle, LineStyle,
    TextElement, LogoElement, SeparatorElement,
    Template, TemplateMeta, CanvasMeta,
    RawBlock, RawLine, SemanticType, SeparatorSubtype,
)


def _make_id(prefix: str, *parts) -> str:
    """Deterministic ID from content — same input always produces same ID."""
    raw = "_".join(str(p) for p in parts)
    return f"{prefix}_{hashlib.md5(raw.encode()).hexdigest()[:8]}"


def _infer_font_size(block: RawBlock, max_font: float) -> float:
    # Cap at 72pt; scale everything relative to the largest font
    ratio = block.font_size / max(max_font, 1)
    if ratio > 0.85:
        return 36.0
    if ratio > 0.65:
        return 24.0
    if ratio > 0.45:
        return 18.0
    if ratio > 0.30:
        return 14.0
    return 11.0


def _infer_alignment(block: RawBlock, canvas_w: float) -> str:
    center = block.x + block.w / 2
    if center > canvas_w * 0.40 and center < canvas_w * 0.60:
        return "center"
    if block.x > canvas_w * 0.50:
        return "right"
    return "left"


def build_template(
    classified: List[Tuple[RawBlock, SemanticType]],
    col_assignments: List[int],
    lines: List[RawLine],
    canvas_w: int,
    canvas_h: int,
    source_file: str,
    page: int = 1,
    side: str = "full",
    logo_info: Optional[dict] = None,
) -> Template:
    elements = []
    max_font = max((b.font_size for b, _ in classified), default=12.0)

    for (block, sem), col in zip(classified, col_assignments):
        font_size = _infer_font_size(block, max_font)
        alignment = _infer_alignment(block, canvas_w)

        elem = TextElement(
            id=_make_id("text", block.text, round(block.x), round(block.y)),
            subtype=sem,
            bbox=BBox(x=round(block.x, 1), y=round(block.y, 1),
                      w=round(block.w, 1), h=round(block.h, 1)),
            content=block.text.strip(),
            style=TextStyle(
                font_size=font_size,
                font_weight="bold" if block.is_bold or sem in ("restaurant_name", "category_header") else "normal",
                font_style="italic" if block.is_italic else "normal",
                text_align=alignment,
            ),
            column=col,
        )
        elements.append(elem.model_dump())

    for line in lines:
        subtype: SeparatorSubtype = (
            "horizontal_line" if line.orientation == "horizontal" else "vertical_line"
        )
        w = abs(line.x2 - line.x1) if line.orientation == "horizontal" else 2.0
        h = abs(line.y2 - line.y1) if line.orientation == "vertical" else 2.0
        sep = SeparatorElement(
            id=_make_id("sep", line.x1, line.y1, line.orientation),
            subtype=subtype,
            orientation=line.orientation,
            bbox=BBox(
                x=round(min(line.x1, line.x2), 1),
                y=round(min(line.y1, line.y2), 1),
                w=round(w, 1),
                h=round(h, 1),
            ),
            style=LineStyle(stroke_width=2.0),
        )
        elements.append(sep.model_dump())

    if logo_info:
        logo_data = None
        if "image_bytes" in logo_info:
            logo_data = base64.b64encode(logo_info["image_bytes"]).decode()
        logo = LogoElement(
            id=_make_id("logo", round(logo_info["x"]), round(logo_info["y"])),
            bbox=BBox(
                x=round(logo_info["x"], 1), y=round(logo_info["y"], 1),
                w=round(logo_info["w"], 1), h=round(logo_info["h"], 1),
            ),
            image_data=logo_data,
        )
        elements.append(logo.model_dump())

    # Sort top-to-bottom, left-to-right for readability
    elements.sort(key=lambda e: (e["bbox"]["y"], e["bbox"]["x"]))

    return Template(
        metadata=TemplateMeta(
            source_file=source_file,
            page=page,
            side=side,
            generated_at=datetime.now(timezone.utc).isoformat(),
            num_columns=max(col_assignments, default=0) + 1,
        ),
        canvas=CanvasMeta(width=canvas_w, height=canvas_h),
        elements=elements,
    )
