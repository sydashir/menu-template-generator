import io
import os
import json
import base64

# Must be set before torch is imported so the MPS driver respects it from startup.
# Disables the MPS high-watermark limit — prevents OOM on the second Surya chunk.
os.environ.setdefault("PYTORCH_MPS_HIGH_WATERMARK_RATIO", "0.0")

import anthropic
from dotenv import load_dotenv
from PIL import Image

from models import MenuData, MenuCategory, MenuItem

load_dotenv()

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic | None:
    global _client
    if _client is not None:
        return _client
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    _client = anthropic.Anthropic(api_key=key)
    return _client



_LAYOUT_PROMPT = """\
You are analyzing a restaurant menu image to extract a pixel-perfect layout template.
Image dimensions: {width}x{height} pixels.

Extract EVERY visible element — text, separators/lines, and logo — with exact bounding boxes.
Return ONLY valid JSON (no markdown, no explanation):
{{
  "background_color": "<#rrggbb dominant background color of the menu>",
  "elements": [
    {{
      "type": "text",
      "subtype": "<restaurant_name|category_header|item_name|item_description|item_price|tagline|address|phone|other_text>",
      "content": "<exact text as written>",
      "bbox": {{"x": <float>, "y": <float>, "w": <float>, "h": <float>}},
      "style": {{
        "font_size": <float_pt>,
        "font_weight": "<normal|bold>",
        "font_style": "<normal|italic>",
        "font_family": "<sans-serif|serif|decorative-script|display|monospace>",
        "color": "<#rrggbb>",
        "text_align": "<left|center|right>"
      }},
      "column": <0_or_1>
    }},
    {{
      "type": "separator",
      "subtype": "<horizontal_line|vertical_line|decorative_divider|border|ornament>",
      "orientation": "<horizontal|vertical>",
      "bbox": {{"x": <float>, "y": <float>, "w": <float>, "h": <float>}},
      "style": {{"color": "<#rrggbb>", "stroke_width": <float>, "stroke_style": "<solid|dashed|dotted>"}}
    }},
    {{
      "type": "logo",
      "bbox": {{"x": <float>, "y": <float>, "w": <float>, "h": <float>}},
      "position_hint": "<top_center|top_left|top_right>"
    }}
  ],
  "menu_data": {{
    "restaurant_name": "<string|null>",
    "tagline": "<string|null>",
    "address": "<string|null>",
    "phone": "<string|null>",
    "num_columns": <1|2>,
    "categories": [
      {{
        "name": "<name>",
        "column": <0|1>,
        "items": [{{"name": "<name>", "description": "<desc|null>", "price": "<price|null>"}}]
      }}
    ]
  }}
}}

Rules:
- bbox: x=left edge, y=top edge, w=width, h=height — all in image pixels
- background_color: sample the dominant background color of the canvas (e.g. #fce4ec for pink, #1a1a1a for dark, #ffffff for white)
- Include ALL text elements visible (nothing omitted)
- Section headers written in cursive/handwriting/decorative script are category_header — do NOT skip them
- Include ALL separator/divider/line/border/ornament elements
- Include logo if present (graphical image element, not text)
- column=0 for left or single column, column=1 for right column
- font_size in approximate pt (pixel height * 0.75)
- font_family: "decorative-script" for cursive/handwritten/calligraphy fonts; "serif" for classic serif; "sans-serif" for modern clean fonts; "display" for large decorative non-script headers
- text_align: detect from layout — "center" if element is centered, "left" or "right" otherwise
"""


_MAX_IMG_DIM = 1920  # resize images larger than this before sending to API

_TOOL_SCHEMA = {
    "name": "extract_menu_layout",
    "description": (
        "Extract every visible element from a restaurant menu image with pixel-accurate "
        "bounding boxes. Captures text (semantic type, style), separators/dividers, and logos. "
        "Also extracts structured menu data (categories, items, prices)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "elements": {
                "type": "array",
                "description": "All visible elements in top-to-bottom order. Include every text block, separator/divider/border/ornament, and any logo region.",
                "items": {
                    "anyOf": [
                        {
                            "type": "object",
                            "description": "A text element.",
                            "properties": {
                                "type": {"type": "string", "enum": ["text"]},
                                "subtype": {
                                    "type": "string",
                                    "enum": ["restaurant_name","category_header","item_name",
                                             "item_description","item_price","tagline",
                                             "address","phone","other_text"]
                                },
                                "content": {"type": "string"},
                                "bbox": {
                                    "type": "object",
                                    "properties": {
                                        "x": {"type": "number"}, "y": {"type": "number"},
                                        "w": {"type": "number"}, "h": {"type": "number"}
                                    },
                                    "required": ["x","y","w","h"]
                                },
                                "style": {
                                    "type": "object",
                                    "properties": {
                                        "font_size": {"type": "number"},
                                        "font_weight": {"type": "string", "enum": ["normal","bold"]},
                                        "font_style": {"type": "string", "enum": ["normal","italic"]},
                                        "font_family": {"type": "string", "enum": ["sans-serif","serif","decorative-script","display","monospace"]},
                                        "color": {"type": "string"},
                                        "text_align": {"type": "string", "enum": ["left","center","right"]}
                                    },
                                    "required": ["font_size","font_weight","font_style","font_family","color","text_align"]
                                },
                                "column": {"type": "integer", "enum": [0, 1]}
                            },
                            "required": ["type","subtype","content","bbox","style","column"]
                        },
                        {
                            "type": "object",
                            "description": "A separator, divider, line, border, or ornament.",
                            "properties": {
                                "type": {"type": "string", "enum": ["separator"]},
                                "subtype": {
                                    "type": "string",
                                    "enum": ["horizontal_line","vertical_line",
                                             "decorative_divider","border","ornament"]
                                },
                                "orientation": {"type": "string", "enum": ["horizontal","vertical"]},
                                "bbox": {
                                    "type": "object",
                                    "properties": {
                                        "x": {"type": "number"}, "y": {"type": "number"},
                                        "w": {"type": "number"}, "h": {"type": "number"}
                                    },
                                    "required": ["x","y","w","h"]
                                },
                                "style": {
                                    "type": "object",
                                    "properties": {
                                        "color": {"type": "string"},
                                        "stroke_width": {"type": "number"},
                                        "stroke_style": {"type": "string", "enum": ["solid","dashed","dotted"]}
                                    },
                                    "required": ["color","stroke_width","stroke_style"]
                                }
                            },
                            "required": ["type","subtype","orientation","bbox","style"]
                        },
                        {
                            "type": "object",
                            "description": "A graphical logo or image region (not text).",
                            "properties": {
                                "type": {"type": "string", "enum": ["logo"]},
                                "bbox": {
                                    "type": "object",
                                    "properties": {
                                        "x": {"type": "number"}, "y": {"type": "number"},
                                        "w": {"type": "number"}, "h": {"type": "number"}
                                    },
                                    "required": ["x","y","w","h"]
                                },
                                "position_hint": {
                                    "type": "string",
                                    "description": "Approximate location, e.g. top_center, top_left, top_right, bottom_center, center, middle_left, etc."
                                }
                            },
                            "required": ["type","bbox","position_hint"]
                        }
                    ]
                }
            },
            "menu_data": {
                "type": "object",
                "properties": {
                    "restaurant_name": {"type": ["string","null"]},
                    "tagline": {"type": ["string","null"]},
                    "address": {"type": ["string","null"]},
                    "phone": {"type": ["string","null"]},
                    "num_columns": {"type": "integer", "enum": [1, 2]},
                    "categories": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "column": {"type": "integer", "enum": [0, 1]},
                                "items": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "name": {"type": "string"},
                                            "description": {"type": ["string","null"]},
                                            "price": {
                                                "type": ["string","null"],
                                                "description": "No $ prefix. Range like 18/21. MP for market price. null if none."
                                            }
                                        },
                                        "required": ["name","description","price"]
                                    }
                                }
                            },
                            "required": ["name","column","items"]
                        }
                    }
                },
                "required": ["restaurant_name","tagline","address","phone","num_columns","categories"]
            }
        },
        "properties": {
            "background_color": {
                "type": "string",
                "description": "Dominant background color of the menu canvas as #rrggbb hex (e.g. #fce4ec for pink, #1a1a1a for dark, #ffffff for white)."
            }
        },
        "required": ["elements","menu_data","background_color"]
    }
}

_TOOL_SYSTEM_PROMPT = """\
You are a precise restaurant menu layout extractor. Use the extract_menu_layout tool to capture every visible element with pixel-accurate bounding boxes.

Key rules:
- background_color: sample the dominant background color of the canvas — output as #rrggbb hex
- bbox: x=left edge px, y=top edge px, w=width px, h=height px (in provided image dimensions)
- Include ALL text elements — nothing omitted, even small footer text
- Section headers in cursive/handwriting/decorative script must be captured as category_header — never skip them
- Include ALL separator/divider/line/border/ornament elements
- Include logo if present (graphical image, emblem, crest, illustration — not text-based branding). If unsure, include it.
- font_size: pixel_height * 0.75 (approximate pt)
- font_family: "decorative-script" for cursive/handwritten/calligraphy; "serif" for classic serif; "sans-serif" for modern clean; "display" for large decorative non-script headers
- text_align: center if element center is in middle 20% of canvas width, else left/right
- Price strings: strip $ prefix. Keep range format like 18/21. Use MP for market price. null if no price.
- column: 0 for left or single column, 1 for right column\
"""


def extract_full_layout_via_claude(img: Image.Image) -> dict | None:
    """
    Extract full layout from a menu image via Claude Vision.
    Returns dict with 'elements' (positioned) and 'menu_data', or None if unavailable.
    Bboxes in returned data are in the original image's pixel coordinates.
    """
    client = _get_client()
    if client is None:
        return None

    orig_w, orig_h = img.size

    # Downscale large images to reduce API payload size, scale bboxes back after
    send_img = img
    scale_x = scale_y = 1.0
    if max(orig_w, orig_h) > _MAX_IMG_DIM:
        ratio = _MAX_IMG_DIM / max(orig_w, orig_h)
        new_w, new_h = max(1, int(orig_w * ratio)), max(1, int(orig_h * ratio))
        send_img = img.resize((new_w, new_h), Image.LANCZOS)
        scale_x = orig_w / new_w
        scale_y = orig_h / new_h

    sw, sh = send_img.size
    buf = io.BytesIO()
    send_img.convert("RGB").save(buf, format="JPEG", quality=85)
    media_type = "image/jpeg"
    image_b64 = base64.standard_b64encode(buf.getvalue()).decode("utf-8")
    prompt = _LAYOUT_PROMPT.format(width=sw, height=sh)

    try:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=16384,  # 32768 triggers Anthropic SDK streaming requirement error
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
    except anthropic.RateLimitError as e:
        print(f"[claude] rate_limit: {e}")
        return None
    except anthropic.APIError as e:
        print(f"[claude] api_error ({type(e).__name__}): {e}")
        return None

    if response.stop_reason == "max_tokens":
        print("[claude] warning: response truncated at max_tokens — output may be incomplete")
        return None

    if not response.content or not hasattr(response.content[0], "text"):
        print(f"[claude] unexpected response structure: stop_reason={response.stop_reason!r}, content={response.content!r}")
        return None
    text = response.content[0].text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1].lstrip("json").strip() if len(parts) > 1 else text
    try:
        data = json.loads(text)
        if not ("elements" in data and isinstance(data["elements"], list)):
            print(f"[claude] bad_structure: keys={list(data.keys())}")
            return None
        # Scale bboxes back to original image pixel coordinates
        if scale_x != 1.0 or scale_y != 1.0:
            for el in data["elements"]:
                bd = el.get("bbox")
                if bd:
                    bd["x"] = bd.get("x", 0) * scale_x
                    bd["y"] = bd.get("y", 0) * scale_y
                    bd["w"] = bd.get("w", 0) * scale_x
                    bd["h"] = bd.get("h", 0) * scale_y
        return data
    except json.JSONDecodeError as e:
        print(f"[claude] json_error: {e} | raw[:200]={text[:200]!r}")
        return None


def extract_full_layout_via_tool_use(img: Image.Image) -> dict | None:
    """
    Extract full menu layout via Anthropic Tool Use (structured outputs).
    Returns same dict structure as extract_full_layout_via_claude(), or None on failure.
    block.input is already a Python dict — no JSON parsing needed.
    Bboxes are in original image pixel coordinates.
    """
    client = _get_client()
    if client is None:
        return None

    orig_w, orig_h = img.size
    send_img = img
    scale_x = scale_y = 1.0
    if max(orig_w, orig_h) > _MAX_IMG_DIM:
        ratio = _MAX_IMG_DIM / max(orig_w, orig_h)
        new_w, new_h = max(1, int(orig_w * ratio)), max(1, int(orig_h * ratio))
        send_img = img.resize((new_w, new_h), Image.LANCZOS)
        scale_x = orig_w / new_w
        scale_y = orig_h / new_h

    sw, sh = send_img.size
    buf = io.BytesIO()
    send_img.convert("RGB").save(buf, format="JPEG", quality=85)
    image_b64 = base64.standard_b64encode(buf.getvalue()).decode("utf-8")

    try:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=16384,  # 32768 triggers Anthropic SDK streaming requirement error
            system=_TOOL_SYSTEM_PROMPT,
            tools=[_TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": "extract_menu_layout"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                    {"type": "text", "text": f"Extract the complete layout of this restaurant menu. Image dimensions: {sw}x{sh} pixels."},
                ],
            }],
        )
    except anthropic.RateLimitError as e:
        print(f"[claude_tool] rate_limit: {e}")
        return None
    except anthropic.APIError as e:
        print(f"[claude_tool] api_error ({type(e).__name__}): {e}")
        return None

    if response.stop_reason == "max_tokens":
        print("[claude_tool] warning: truncated at max_tokens")
        return None

    data = None
    for block in response.content:
        if block.type == "tool_use" and block.name == "extract_menu_layout":
            data = block.input
            break

    if data is None:
        print(f"[claude_tool] no tool_use block; stop_reason={response.stop_reason!r}")
        return None

    if not isinstance(data, dict):
        print(f"[claude_tool] block.input is not a dict: {type(data).__name__}")
        return None

    if not isinstance(data.get("elements"), list):
        print("[claude_tool] bad_structure: 'elements' missing or not a list")
        return None

    if scale_x != 1.0 or scale_y != 1.0:
        for el in data["elements"]:
            bd = el.get("bbox")
            if bd:
                bd["x"] = bd.get("x", 0) * scale_x
                bd["y"] = bd.get("y", 0) * scale_y
                bd["w"] = bd.get("w", 0) * scale_x
                bd["h"] = bd.get("h", 0) * scale_y

    return data


def _bbox_iou(a: dict, b: dict) -> float:
    """Intersection-over-Union for two {x, y, w, h} bbox dicts."""
    ax1, ay1 = a.get("x", 0), a.get("y", 0)
    ax2, ay2 = ax1 + a.get("w", 0), ay1 + a.get("h", 0)
    bx1, by1 = b.get("x", 0), b.get("y", 0)
    bx2, by2 = bx1 + b.get("w", 0), by1 + b.get("h", 0)
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    union = a.get("w", 0) * a.get("h", 0) + b.get("w", 0) * b.get("h", 0) - inter
    return inter / union if union > 0 else 0.0


def _dedup_text_elements(elements: list) -> list:
    """
    Remove duplicate text elements — elements are duplicates only if they share
    the same content + subtype AND their bboxes overlap (IoU > 0.1).
    This preserves legitimately same-named items in different positions (e.g. same
    dish in two columns) while eliminating near-duplicate bboxes from two Claude passes.
    When duplicates are found, keep the one with the larger bbox area.
    Also filters out empty-content text elements.
    Non-text elements (separators, logos) are passed through unchanged.
    """
    text_elements = []
    result = []

    for el in elements:
        if el.get("type") != "text":
            result.append(el)
            continue
        content = (el.get("content") or "").strip()
        if not content:
            continue  # drop ghost empty elements
        text_elements.append(el)

    # For each text element, check if it's a positional duplicate of an already-kept element
    kept = []
    for el in text_elements:
        content = (el.get("content") or "").strip().lower()
        subtype = el.get("subtype", "")
        bd = el.get("bbox") or {}
        area = bd.get("w", 0) * bd.get("h", 0)

        duplicate_idx = None
        for i, kept_el in enumerate(kept):
            if (kept_el.get("subtype", "") == subtype
                    and (kept_el.get("content") or "").strip().lower() == content):
                kept_bd = kept_el.get("bbox") or {}
                if _bbox_iou(bd, kept_bd) > 0.1:
                    duplicate_idx = i
                    break

        if duplicate_idx is not None:
            existing_bd = kept[duplicate_idx].get("bbox") or {}
            existing_area = existing_bd.get("w", 0) * existing_bd.get("h", 0)
            if area > existing_area:
                kept[duplicate_idx] = el  # replace with larger bbox version
        else:
            kept.append(el)

    result.extend(kept)
    return result


_TOP_HINTS = {"top_center", "top_left", "top_right"}


def _enforce_single_logo(elements: list) -> list:
    """
    A menu page has exactly one logo. If multiple logo elements are present,
    keep the one with a top-position hint (top_left/top_center/top_right) or
    the one with the largest bbox area. All others are reclassified as
    separator/ornament — they are typically ornamental line clusters near the
    logo that Claude misidentifies as a second logo.
    """
    logos = [(i, e) for i, e in enumerate(elements) if e.get("type") == "logo"]
    if len(logos) <= 1:
        return elements

    # Score each logo: prefer top-positioned hint, then larger area
    def _logo_score(idx_el):
        _, el = idx_el
        hint = (el.get("position_hint") or "").lower()
        bd = el.get("bbox") or {}
        area = bd.get("w", 0) * bd.get("h", 0)
        return (1 if hint in _TOP_HINTS else 0, area)

    logos_sorted = sorted(logos, key=_logo_score, reverse=True)
    keep_idx = logos_sorted[0][0]

    result = []
    for i, el in enumerate(elements):
        if el.get("type") == "logo" and i != keep_idx:
            # Reclassify as separator/ornament
            bd = el.get("bbox") or {}
            result.append({
                "type": "separator",
                "subtype": "ornament",
                "orientation": "horizontal",
                "bbox": bd,
                "style": {"color": "#000000", "stroke_width": 1.5, "stroke_style": "solid"},
            })
        else:
            result.append(el)
    return result


_HYBRID_SYSTEM_PROMPT = """\
You are analyzing a restaurant menu image. Text blocks have been pre-extracted by OCR with exact pixel positions.
Your job is to LABEL the OCR blocks semantically and identify elements OCR missed (cursive/script headings).

Rules:
- background_color: dominant background as #rrggbb
- logo_bbox: restaurant logo graphic/emblem at top of page. Pixel bbox (tight around the image/emblem only, NOT the restaurant name text).
- ocr_labels: assign a subtype + column + font_family to EVERY block ID in the list. Do NOT skip any.
  - corrected_text: if the OCR text is wrong (common with cursive/decorative fonts — e.g. "Talls" instead of "Wines by the glass"), provide the correct reading. null if the OCR text is correct.
- font_family choices: "decorative-script" for cursive/handwritten, "serif" for classic serif, "sans-serif" for modern clean, "display" for large non-script decorative.
- decorative_elements: script/cursive section headers VISIBLE in the image that OCR entirely missed (no numbered box covers them). Provide pixel bbox + content.
- menu_data: full structured menu — all categories and items.
- Do NOT produce bboxes for text already covered by a numbered OCR box.
"""

_HYBRID_TOOL_SCHEMA = {
    "name": "label_menu_layout",
    "description": "Label OCR text blocks semantically and identify decorative elements OCR missed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "background_color": {"type": "string"},
            "logo_bbox": {
                "type": ["object", "null"],
                "properties": {
                    "x": {"type": "number"}, "y": {"type": "number"},
                    "w": {"type": "number"}, "h": {"type": "number"},
                },
                "required": ["x", "y", "w", "h"],
            },
            "ocr_labels": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "subtype": {
                            "type": "string",
                            "enum": ["restaurant_name", "category_header", "item_name",
                                     "item_description", "item_price", "tagline",
                                     "address", "phone", "other_text"],
                        },
                        "column": {"type": "integer", "enum": [0, 1]},
                        "font_family": {
                            "type": "string",
                            "enum": ["sans-serif", "serif", "decorative-script", "display"],
                        },
                        "corrected_text": {
                            "type": ["string", "null"],
                            "description": "Corrected reading if OCR misread the text (e.g. decorative/cursive font). null if OCR text is correct.",
                        },
                    },
                    "required": ["id", "subtype", "column", "font_family"],
                },
            },
            "decorative_elements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "subtype": {
                            "type": "string",
                            "enum": ["restaurant_name", "category_header", "item_name",
                                     "item_description", "item_price", "tagline",
                                     "address", "phone", "other_text"],
                        },
                        "font_family": {
                            "type": "string",
                            "enum": ["sans-serif", "serif", "decorative-script", "display"],
                        },
                        "bbox": {
                            "type": "object",
                            "properties": {
                                "x": {"type": "number"}, "y": {"type": "number"},
                                "w": {"type": "number"}, "h": {"type": "number"},
                            },
                            "required": ["x", "y", "w", "h"],
                        },
                        "column": {"type": "integer", "enum": [0, 1]},
                        "text_align": {"type": "string", "enum": ["left", "center", "right"]},
                    },
                    "required": ["content", "subtype", "font_family", "bbox", "column"],
                },
            },
            "menu_data": {
                "type": "object",
                "properties": {
                    "restaurant_name": {"type": ["string", "null"]},
                    "tagline": {"type": ["string", "null"]},
                    "address": {"type": ["string", "null"]},
                    "phone": {"type": ["string", "null"]},
                    "num_columns": {"type": "integer", "enum": [1, 2]},
                    "categories": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "column": {"type": "integer", "enum": [0, 1]},
                                "items": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "name": {"type": "string"},
                                            "description": {"type": ["string", "null"]},
                                            "price": {"type": ["string", "null"]},
                                        },
                                        "required": ["name", "description", "price"],
                                    },
                                },
                            },
                            "required": ["name", "column", "items"],
                        },
                    },
                },
                "required": ["restaurant_name", "tagline", "address", "phone",
                             "num_columns", "categories"],
            },
        },
        "required": ["background_color", "ocr_labels", "decorative_elements", "menu_data"],
    },
}


# ---------------------------------------------------------------------------
# Phase 3 — Surya OCR + Set-of-Marks (Precision Engine)
# ---------------------------------------------------------------------------

_surya_det_model = None
_surya_det_processor = None
_surya_rec_model = None
_surya_rec_processor = None


def _load_surya_models() -> bool:
    """Lazy-load and cache Surya OCR models. Returns True if successful."""
    global _surya_det_model, _surya_det_processor, _surya_rec_model, _surya_rec_processor
    if _surya_det_model is not None:
        return True
    try:
        # Enable MPS (Apple Silicon GPU) if available — ~5-10x faster than CPU
        try:
            import torch as _torch
            if _torch.backends.mps.is_available():
                os.environ.setdefault("TORCH_DEVICE", "mps")
                os.environ.setdefault("RECOGNITION_DEVICE", "mps")
                os.environ.setdefault("DETECTOR_DEVICE", "mps")
                print("[surya] MPS (Apple Silicon GPU) enabled")
        except Exception:
            pass

        from surya.model.detection.segformer import (
            load_model as _det_model,
            load_processor as _det_proc,
        )
        from surya.model.recognition.model import load_model as _rec_model
        from surya.model.recognition.processor import load_processor as _rec_proc
        print("[surya] loading models (first run — may download ~1 GB)…")
        _surya_det_model = _det_model()
        _surya_det_processor = _det_proc()
        _surya_rec_model = _rec_model()
        _surya_rec_processor = _rec_proc()
        print("[surya] models ready")
        return True
    except Exception as exc:
        print(f"[surya] model load failed: {exc}")
        return False


def extract_blocks_surya(img: Image.Image) -> list:
    """
    Run Surya OCR on img, return line-level blocks as:
      [{"text": str, "bbox": [x1, y1, x2, y2], "confidence": float}, ...]
    Returns empty list if Surya is not installed or inference fails.
    """
    if not _load_surya_models():
        return []
    try:
        from surya.ocr import run_ocr
        results = run_ocr(
            [img], [["en"]],
            _surya_det_model, _surya_det_processor,
            _surya_rec_model, _surya_rec_processor,
        )
        blocks = []
        for line in results[0].text_lines:
            text = (line.text or "").strip()
            if not text:
                continue
            # Support both bbox=[x1,y1,x2,y2] and polygon=[[x,y],...]
            if hasattr(line, "bbox") and line.bbox:
                x1, y1, x2, y2 = line.bbox
            elif hasattr(line, "polygon") and line.polygon:
                xs = [p[0] for p in line.polygon]
                ys = [p[1] for p in line.polygon]
                x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
            else:
                continue
            blocks.append({
                "text": text,
                "bbox": [float(x1), float(y1), float(x2), float(y2)],
                "confidence": float(getattr(line, "confidence", 1.0)),
            })
        print(f"[surya] {len(blocks)} lines extracted")
        return blocks
    except Exception as exc:
        print(f"[surya] inference error: {exc}")
        return []
    finally:
        # Release MPS cache after every inference (success or failure) so the
        # next chunk doesn't OOM from accumulated allocations.
        try:
            import torch as _torch
            if _torch.backends.mps.is_available():
                _torch.mps.empty_cache()
        except Exception:
            pass


_SOM_PALETTE = [
    "#e74c3c", "#3498db", "#2ecc71", "#f39c12", "#9b59b6",
    "#1abc9c", "#e67e22", "#27ae60", "#2980b9", "#8e44ad",
]


def _draw_som_annotations(img: Image.Image, blocks: list) -> Image.Image:
    """Draw colored numbered bounding boxes on a copy of img (Set-of-Marks)."""
    from PIL import ImageDraw
    annotated = img.copy().convert("RGB")
    draw = ImageDraw.Draw(annotated)
    for i, block in enumerate(blocks):
        color = _SOM_PALETTE[i % len(_SOM_PALETTE)]
        x1, y1, x2, y2 = block["bbox"]
        draw.rectangle([x1, y1, x2, y2], outline=color, width=3)
        lbl = str(i + 1)
        lbl_w, lbl_h = len(lbl) * 9 + 6, 18
        lbl_x = x1
        lbl_y = max(0, y1 - lbl_h - 1)
        draw.rectangle([lbl_x, lbl_y, lbl_x + lbl_w, lbl_y + lbl_h], fill=color)
        draw.text((lbl_x + 3, lbl_y + 2), lbl, fill="white")
    return annotated


def extract_layout_surya_som(img: Image.Image) -> dict | None:
    """
    Precision Engine: Surya OCR (exact pixel coordinates) + Set-of-Marks visual
    prompting → Claude assigns semantic labels only, never generates coordinates.

    Eliminates coordinate hallucination for all readable text.
    Decorative/script elements unreachable by OCR are still captured via Claude's
    decorative_elements field (approximate bbox — acceptable for section headers).

    Returns standard claude_layout dict, or None if Surya unavailable / API fails.
    """
    client = _get_client()
    if client is None:
        return None

    surya_blocks = extract_blocks_surya(img)
    if len(surya_blocks) < 3:
        print("[surya_som] too few blocks — skipping")
        return None

    # Annotate image with SoM boxes before sending to Claude
    annotated = _draw_som_annotations(img, surya_blocks)

    orig_w, orig_h = img.size
    send_img = annotated
    scale_x = scale_y = 1.0
    if max(orig_w, orig_h) > _MAX_IMG_DIM:
        ratio = _MAX_IMG_DIM / max(orig_w, orig_h)
        new_w = max(1, int(orig_w * ratio))
        new_h = max(1, int(orig_h * ratio))
        send_img = annotated.resize((new_w, new_h), Image.LANCZOS)
        scale_x = orig_w / new_w
        scale_y = orig_h / new_h

    buf = io.BytesIO()
    send_img.convert("RGB").save(buf, format="JPEG", quality=85)
    image_b64 = base64.standard_b64encode(buf.getvalue()).decode()

    # Compact block list for Claude — position context only, no coordinates to reproduce
    lines = []
    for i, b in enumerate(surya_blocks):
        x1, y1 = b["bbox"][0], b["bbox"][1]
        pct_x = round(x1 / orig_w * 100)
        pct_y = round(y1 / orig_h * 100)
        text = b["text"] if len(b["text"]) <= 80 else b["text"][:77] + "..."
        lines.append(f"[{i + 1}] \"{text}\" — top:{pct_y}%, left:{pct_x}%")
    block_list = "\n".join(lines)

    sw, sh = send_img.size
    user_msg = (
        f"Menu image: {sw}×{sh}px. Numbered bounding boxes are drawn on the image.\n\n"
        f"OCR blocks ({len(surya_blocks)}):\n{block_list}\n\n"
        "Label every block by its ID number. Add decorative_elements for any cursive/script "
        "headings visible in the image that the OCR boxes did not capture."
    )

    try:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=8192,
            system=_HYBRID_SYSTEM_PROMPT,
            tools=[_HYBRID_TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": "label_menu_layout"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                    {"type": "text", "text": user_msg},
                ],
            }],
        )
    except anthropic.RateLimitError as exc:
        print(f"[surya_som] rate_limit: {exc}")
        return None
    except anthropic.APIError as exc:
        print(f"[surya_som] api_error: {exc}")
        return None

    if response.stop_reason == "max_tokens":
        print("[surya_som] truncated at max_tokens — falling back")
        return None

    data = None
    for block in response.content:
        if block.type == "tool_use" and block.name == "label_menu_layout":
            data = block.input
            break

    if data is None or not isinstance(data, dict):
        print(f"[surya_som] no tool_use block; stop_reason={response.stop_reason!r}")
        return None

    # Build standard claude_layout elements
    elements: list[dict] = []
    label_map = {lbl.get("id"): lbl for lbl in data.get("ocr_labels", []) if lbl.get("id")}

    for i, b in enumerate(surya_blocks):
        lbl = label_map.get(i + 1, {})
        x1, y1, x2, y2 = b["bbox"]
        elem_h = max(1.0, y2 - y1)
        # Use Claude's corrected reading if OCR got the text wrong (e.g. decorative fonts)
        content = lbl.get("corrected_text") or b["text"]
        elements.append({
            "type": "text",
            "subtype": lbl.get("subtype", "other_text"),
            "content": content,
            "bbox": {"x": x1, "y": y1, "w": max(1.0, x2 - x1), "h": elem_h},
            "style": {
                "font_size": round(elem_h * 0.75, 1),
                "font_weight": "normal",
                "font_style": "normal",
                "font_family": lbl.get("font_family", "serif"),
                "color": "#1a1a1a",
                "text_align": "left",
            },
            "column": int(lbl.get("column", 0)),
        })

    # Decorative elements use Claude's approximate bboxes — fine for section headers
    for dec in data.get("decorative_elements", []):
        bd = dec.get("bbox") or {}
        if scale_x != 1.0 or scale_y != 1.0:
            bd = {
                "x": bd.get("x", 0) * scale_x, "y": bd.get("y", 0) * scale_y,
                "w": bd.get("w", 0) * scale_x,  "h": bd.get("h", 0) * scale_y,
            }
        elem_h = max(1.0, float(bd.get("h", 30)))
        elements.append({
            "type": "text",
            "subtype": dec.get("subtype", "category_header"),
            "content": dec.get("content", ""),
            "bbox": {"x": float(bd.get("x", 0)), "y": float(bd.get("y", 0)),
                     "w": max(1.0, float(bd.get("w", 100))), "h": elem_h},
            "style": {
                "font_size": round(elem_h * 0.75, 1),
                "font_weight": "normal",
                "font_style": "italic",
                "font_family": dec.get("font_family", "decorative-script"),
                "color": "#1a1a1a",
                "text_align": dec.get("text_align", "center"),
            },
            "column": int(dec.get("column", 0)),
        })

    # Logo: Claude's approximate bbox, exact crop happens later in pipeline
    lb = data.get("logo_bbox")
    if isinstance(lb, dict) and lb.get("w", 0) > 0 and lb.get("h", 0) > 0:
        if scale_x != 1.0 or scale_y != 1.0:
            lb = {"x": lb.get("x", 0) * scale_x, "y": lb.get("y", 0) * scale_y,
                  "w": lb.get("w", 0) * scale_x,  "h": lb.get("h", 0) * scale_y}
        elements.append({"type": "logo", "bbox": lb, "position_hint": "top_center"})

    print(f"[surya_som] built {len(elements)} elements "
          f"(ocr={len(surya_blocks)}, decorative={len(data.get('decorative_elements', []))})")
    return {
        "elements": elements,
        "menu_data": data.get("menu_data", {}),
        "background_color": data.get("background_color", "#ffffff"),
    }


def merge_layouts(primary: dict | None, secondary: dict | None,
                  math_first: bool = False) -> dict | None:
    """
    Merge two layout extraction results for maximum element coverage.
    primary (prompt-based) is the main source — its elements are kept as-is.
    secondary (tool-use) elements are added only when they don't overlap with
    any primary element (IoU < 0.3), ensuring nothing unique is lost.
    Logos are special: any logo found by either method is included (there are
    very few on a menu and missing one is unacceptable).
    menu_data comes from whichever result has more categories.
    Returns None only if both inputs are None.
    """
    if primary is None and secondary is None:
        return None
    if primary is None:
        return secondary
    if secondary is None:
        return primary

    merged = list(primary.get("elements", []))
    primary_bboxes = [e["bbox"] for e in merged if e.get("bbox")]
    primary_has_logo = any(e.get("type") == "logo" for e in merged)

    for el in secondary.get("elements", []):
        bbox = el.get("bbox")
        if not bbox:
            continue

        if el.get("type") == "logo":
            # Add logo from secondary only if it doesn't overlap an existing logo
            # (handles menus with multiple logos at different positions)
            existing_logo_bboxes = [e["bbox"] for e in merged if e.get("type") == "logo" and e.get("bbox")]
            max_iou = max((_bbox_iou(bbox, lb) for lb in existing_logo_bboxes), default=0.0)
            if max_iou < 0.3:
                merged.append(el)
                primary_has_logo = True
            continue

        # Add non-logo element only if it doesn't overlap existing ones
        max_iou = max((_bbox_iou(bbox, pb) for pb in primary_bboxes), default=0.0)
        if max_iou < 0.3:
            merged.append(el)
            primary_bboxes.append(bbox)

    # Use menu_data from whichever source extracted more categories.
    # When math_first=True (Surya primary), prefer secondary (Claude Vision holistic
    # understanding) for menu_data since Claude has better semantic comprehension.
    p_md = primary.get("menu_data", {})
    s_md = secondary.get("menu_data", {})
    if math_first:
        menu_data = s_md if len(s_md.get("categories", [])) >= len(p_md.get("categories", [])) else p_md
    else:
        menu_data = p_md if len(p_md.get("categories", [])) >= len(s_md.get("categories", [])) else s_md

    # Use background_color from whichever source has it (prefer primary)
    background_color = (
        primary.get("background_color")
        or secondary.get("background_color")
        or "#ffffff"
    )

    # Content-based dedup: if two text elements share content+subtype, keep the larger bbox
    merged = _dedup_text_elements(merged)

    # Logo cap: a menu page has exactly one logo. Keep the first top-positioned logo;
    # reclassify any additional logos as separator/ornament (they're typically ornamental
    # line groupings near the logo that Claude misidentifies as a second logo).
    merged = _enforce_single_logo(merged)

    return {"elements": merged, "menu_data": menu_data, "background_color": background_color}


def build_menu_data_from_claude(
    data: dict,
    source_file: str,
    side: str,
    num_separators: int,
    num_columns: int,
    logo_detected: bool = False,
) -> MenuData:
    categories = []
    for cat_d in data.get("categories", []):
        cat = MenuCategory(name=cat_d.get("name", ""), column=cat_d.get("column", 0))
        for item_d in cat_d.get("items", []):
            cat.items.append(MenuItem(
                name=item_d.get("name", ""),
                description=item_d.get("description"),
                price=item_d.get("price"),
            ))
        categories.append(cat)

    return MenuData(
        source_file=source_file,
        side=side,
        restaurant_name=data.get("restaurant_name"),
        tagline=data.get("tagline"),
        address=data.get("address"),
        phone=data.get("phone"),
        categories=categories,
        logo_detected=logo_detected,
        num_separators=num_separators,
        num_columns=num_columns,
        layout_notes=(
            f"{num_columns}-column layout, {len(categories)} sections detected via Claude vision."
        ),
    )
