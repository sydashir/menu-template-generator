import base64
import hashlib
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from models import (
    BBox, TextStyle, LineStyle,
    TextElement, LogoElement, SeparatorElement, ImageElement,
    Template, TemplateMeta, CanvasMeta, FontAsset,
    RawBlock, RawLine, SemanticType, SeparatorSubtype,
)


def _make_id(prefix: str, *parts) -> str:
    """Deterministic ID from content — same input always produces same ID."""
    raw = "_".join(str(p) for p in parts)
    return f"{prefix}_{hashlib.md5(raw.encode()).hexdigest()[:8]}"


def _infer_font_size(block: RawBlock, max_font: float) -> float:
    # Preserve extracted font size for highest-fidelity template reconstruction.
    # (The caller already provides values in output canvas-space units.)
    return round(max(1.0, float(block.font_size)), 2)


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
    background_color: str = "#ffffff",
    fonts: Optional[List[dict]] = None,
) -> Template:
    elements = []
    max_font = max((b.font_size for b, _ in classified), default=12.0)
    # Family names we have a registered TTF for — text in these fonts
    # uses the canonical name; anything else falls back to the 5-way category.
    registered_families = {f["family"] for f in (fonts or [])}

    for (block, sem), col in zip(classified, col_assignments):
        font_size = _infer_font_size(block, max_font)
        alignment = _infer_alignment(block, canvas_w)

        # Prefer the embedded font's real name when we have its binary —
        # makes the renderer pick the @font-face we register from font_assets.
        chosen_family = block.font_family
        if block.font_family_raw and block.font_family_raw in registered_families:
            chosen_family = block.font_family_raw

        elem = TextElement(
            id=_make_id("text", block.text, round(block.x), round(block.y)),
            subtype=sem,
            bbox=BBox(x=round(block.x, 1), y=round(block.y, 1),
                      w=round(block.w, 1), h=round(block.h, 1)),
            content=block.text.strip(),
            style=TextStyle(
                font_size=font_size,
                font_weight="bold" if block.is_bold else "normal",
                font_style="italic" if (block.is_italic or block.font_family == "decorative-script") else "normal",
                font_family=chosen_family,
                color=block.color,
                text_align=alignment,
            ),
            column=col,
        )
        elements.append(elem.model_dump())

    for line in lines:
        # Preserve actual height for thick band separators (ornaments, section bars)
        if line.orientation == "horizontal":
            w = abs(line.x2 - line.x1)
            h = max(2.0, abs(line.y2 - line.y1))
        else:
            w = max(2.0, abs(line.x2 - line.x1))
            h = abs(line.y2 - line.y1)
        # Classify thick bands as decorative_divider; thin lines as horizontal/vertical_line
        if line.orientation == "horizontal":
            subtype: SeparatorSubtype = "decorative_divider" if h > 4.0 else "horizontal_line"
        else:
            subtype = "vertical_line"
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
            style=LineStyle(
                color=line.color or "#000000",
                stroke_width=round(h if line.orientation == "horizontal" else 2.0, 1),
            ),
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
        canvas=CanvasMeta(width=canvas_w, height=canvas_h, background_color=background_color),
        elements=elements,
        fonts=[FontAsset(**f) for f in (fonts or [])],
    )


_VALID_TEXT_SUBTYPES = {
    "restaurant_name", "category_header", "item_name", "item_description",
    "item_price", "tagline", "address", "phone", "other_text",
}
_VALID_SEP_SUBTYPES = {
    "horizontal_line", "vertical_line", "decorative_divider", "border", "ornament",
}


def _safe_float(val, default: float) -> float:
    """Return float(val) or default if val is None/invalid."""
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def build_template_from_claude(
    claude_data: dict,
    source_file: str,
    page: int = 1,
    side: str = "full",
    canvas_w: int = 0,
    canvas_h: int = 0,
    logo_image_data: Optional[str] = None,
    background_color: str = "#ffffff",
    fonts: Optional[List[dict]] = None,
) -> Template:
    """Build Template directly from Claude's full layout extraction (elements with bboxes)."""
    elements = []
    col_vals = []

    for raw_el in claude_data.get("elements", []):
        el_type = raw_el.get("type")
        bd = raw_el.get("bbox") or {}
        try:
            bbox = BBox(
                x=round(_safe_float(bd.get("x"), 0), 1),
                y=round(_safe_float(bd.get("y"), 0), 1),
                w=round(max(1.0, _safe_float(bd.get("w"), 1)), 1),
                h=round(max(1.0, _safe_float(bd.get("h"), 1)), 1),
            )
        except (TypeError, ValueError):
            continue

        if not isinstance(raw_el, dict):
            continue

        try:
            if el_type == "text":
                style_raw = raw_el.get("style")
                sd = style_raw if isinstance(style_raw, dict) else {}
                col = min(1, max(0, int(_safe_float(raw_el.get("column"), 0))))
                col_vals.append(col)
                raw_subtype = raw_el.get("subtype", "other_text")
                subtype = raw_subtype if raw_subtype in _VALID_TEXT_SUBTYPES else "other_text"
                elem = TextElement(
                    id=_make_id("text", raw_el.get("content", ""), round(bbox.x), round(bbox.y)),
                    subtype=subtype,
                    bbox=bbox,
                    content=str(raw_el.get("content") or "").strip(),
                    style=TextStyle(
                        font_size=round(max(1.0, _safe_float(sd.get("font_size"), 12)), 2),
                        font_weight=sd.get("font_weight") if sd.get("font_weight") in ("normal", "bold") else "normal",
                        font_style=sd.get("font_style") if sd.get("font_style") in ("normal", "italic") else "normal",
                        font_family=sd.get("font_family") if sd.get("font_family") in ("sans-serif", "serif", "decorative-script", "display", "monospace") else "sans-serif",
                        color=sd.get("color") or "#000000",
                        text_align=sd.get("text_align") if sd.get("text_align") in ("left", "center", "right") else "left",
                    ),
                    column=col,
                )
                elements.append(elem.model_dump())

            elif el_type == "separator":
                style_raw = raw_el.get("style")
                sd = style_raw if isinstance(style_raw, dict) else {}
                orientation = raw_el.get("orientation", "horizontal")
                if orientation not in ("horizontal", "vertical"):
                    orientation = "horizontal"
                raw_subtype = raw_el.get("subtype", "horizontal_line")
                sep_subtype = raw_subtype if raw_subtype in _VALID_SEP_SUBTYPES else (
                    "horizontal_line" if orientation == "horizontal" else "vertical_line"
                )
                sep = SeparatorElement(
                    id=_make_id("sep", round(bbox.x), round(bbox.y), orientation),
                    subtype=sep_subtype,
                    orientation=orientation,
                    bbox=bbox,
                    style=LineStyle(
                        color=sd.get("color") or "#000000",
                        stroke_width=_safe_float(sd.get("stroke_width"), 1.5),
                        stroke_style=sd.get("stroke_style") if sd.get("stroke_style") in ("solid", "dashed", "dotted") else "solid",
                    ),
                    image_data=raw_el.get("image_data"),
                    semantic_label=raw_el.get("semantic_label"),
                )
                elements.append(sep.model_dump())

            elif el_type == "logo":
                logo = LogoElement(
                    id=_make_id("logo", round(bbox.x), round(bbox.y)),
                    bbox=bbox,
                    image_data=logo_image_data,
                    position_hint=raw_el.get("position_hint") or "top_center",
                )
                elements.append(logo.model_dump())

            elif el_type == "image":
                _valid_img_subs = {"badge", "ornament", "collage_box"}
                raw_sub = raw_el.get("subtype", "badge")
                img_subtype = raw_sub if raw_sub in _valid_img_subs else "ornament"
                img_el = ImageElement(
                    id=_make_id("img", round(bbox.x), round(bbox.y)),
                    subtype=img_subtype,
                    bbox=bbox,
                    image_data=raw_el.get("image_data"),
                    semantic_label=raw_el.get("semantic_label"),
                )
                elements.append(img_el.model_dump())

        except Exception as e:
            print(f"[builder] skipped element type={raw_el.get('type')!r} subtype={raw_el.get('subtype')!r}: {e}")
            continue

    elements.sort(key=lambda e: (e["bbox"]["y"], e["bbox"]["x"]))
    num_cols = max(col_vals, default=0) + 1

    return Template(
        metadata=TemplateMeta(
            source_file=source_file,
            page=page,
            side=side,
            generated_at=datetime.now(timezone.utc).isoformat(),
            num_columns=num_cols,
        ),
        canvas=CanvasMeta(width=canvas_w, height=canvas_h, background_color=background_color),
        elements=elements,
        fonts=[FontAsset(**f) for f in (fonts or [])],
    )
