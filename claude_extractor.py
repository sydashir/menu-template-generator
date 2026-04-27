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

try:
    import cv2
    import numpy as np
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False

from models import MenuData, MenuCategory, MenuItem

load_dotenv()

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic | None:
    global _client
    if _client is not None:
        return _client
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip().strip('"').strip("'")
    if not key:
        return None
    kwargs: dict = {"api_key": key}
    # Allow routing through a proxy via ANTHROPIC_BASE_URL env var
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    if base_url:
        kwargs["base_url"] = base_url
    # Allow HTTP proxy via HTTPS_PROXY env var (httpx respects this automatically)
    _client = anthropic.Anthropic(**kwargs)
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
                    "restaurant_name": {
                        "type": "string",
                        "description": "The name of the restaurant. If not explicitly in text, extract from the logo/branding."
                    },
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
            },
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
            model="claude-sonnet-4-6",
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
            model="claude-sonnet-4-6",
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
    Refined Over-Capture & Ghosting Prevention:
    1. Maximizes accuracy score by keeping unique words.
    2. Kills ghosting by merging overlapping elements with identical text.
    3. Prioritizes Surya OCR coordinates for the 'pixel-perfect' replica.
    """
    text_elements = []
    result = []

    for el in elements:
        t = el.get("type")
        if t != "text":
            result.append(el)
            continue
        content = (el.get("content") or "").strip()
        if content:
            text_elements.append(el)

    def get_word_set(s):
        return set("".join(filter(str.isalnum, w.lower())) for w in str(s).split())

    # Sort text elements: prioritize those with more words or larger areas (likely Surya)
    # Actually, Surya blocks in our pipeline don't have a 'source' tag inside the element dict here,
    # but they are usually processed first.
    
    deduped = []
    for el in text_elements:
        bd = el.get("bbox") or {}
        content = str(el.get("content", ""))
        words = get_word_set(content)
        col = el.get("column", 0)
        
        duplicate_idx = None
        for i, ex_el in enumerate(deduped):
            ex_bd = ex_el.get("bbox") or {}
            ex_content = str(ex_el.get("content", ""))
            ex_words = get_word_set(ex_content)
            ex_col = ex_el.get("column", 0)
            
            # Case 0: Identical word set -> merge regardless of column or IoU
            # (Fixes "ghosting" where Claude hallucinates a copy or misassigns column)
            if words == ex_words:
                # IMPORTANT: Prefer earlier elements (Surya blocks) for coordinates.
                # Claude's decorative copies are added later and often shifted.
                # Only replace if current is a category_header and ex is not.
                if el.get("subtype") == "category_header" and ex_el.get("subtype") != "category_header":
                    # Keep current but try to snap it later
                    deduped[i] = el
                else:
                    # Keep existing (Surya)
                    pass
                duplicate_idx = i
                break

            iou = _bbox_iou(bd, ex_bd)
            
            # Case 1: Subset text + significant overlap
            if iou > 0.2:
                if words.issubset(ex_words):
                    duplicate_idx = i
                    break
                if ex_words.issubset(words):
                    # Current element is more complete, replace the existing one
                    ex_el["content"] = content
                    ex_el["bbox"] = bd 
                    duplicate_idx = i
                    break

            # Case 2: Extreme overlap (>80%) - likely same visual spot
            if iou > 0.8:
                # Union of words to protect accuracy score
                new_words_list = content.split()
                ex_words_list = ex_content.split()
                unique_new = [w for w in new_words_list if "".join(filter(str.isalnum, w.lower())) not in ex_words]
                if unique_new:
                    ex_el["content"] = ex_content + " " + " ".join(unique_new)
                duplicate_idx = i
                break

            # Case 3: Same column, partial text (subset/superset), vertically close.
            # Catches "the Table" (OCR) vs "For the Table" (decorative) at different y.
            cy = bd.get("y", 0) + bd.get("h", 0) / 2
            ex_cy = ex_bd.get("y", 0) + ex_bd.get("h", 0) / 2
            if col == ex_col and abs(cy - ex_cy) < 250:
                if words and ex_words and (words.issubset(ex_words) or ex_words.issubset(words)):
                    if len(words) >= len(ex_words):
                        # Current has more words — it's the complete version, replace
                        deduped[i] = el
                    # else: existing has more words — keep it, discard current
                    duplicate_idx = i
                    break

        if duplicate_idx is None:
            deduped.append(el)

    result.extend(deduped)
    return result


_TOP_HINTS = {"top_center", "top_left", "top_right"}


def _enforce_single_logo(elements: list) -> list:
    """
    Merge multiple detected logo elements into a single logo with a union
    bounding box. Claude often fragments a complex logo (emblem + decorative
    frame + brand text graphic) into 2-3 separate logo elements. Rather than
    discarding the extras, compute the bbox that covers all nearby fragments
    so the full visual logo region is preserved as one entity.

    Fragments within 2x the anchor logo's largest dimension are merged.
    Truly distant logos (rare multi-logo menus) are reclassified as ornaments.
    """
    logos = [(i, e) for i, e in enumerate(elements) if e.get("type") == "logo"]
    if len(logos) <= 1:
        return elements

    # Anchor = top-positioned or largest area
    def _logo_score(idx_el):
        _, el = idx_el
        hint = (el.get("position_hint") or "").lower()
        bd = el.get("bbox") or {}
        area = bd.get("w", 0) * bd.get("h", 0)
        return (1 if hint in _TOP_HINTS else 0, area)

    anchor_idx, anchor_logo = sorted(logos, key=_logo_score, reverse=True)[0]
    anchor_bd = anchor_logo.get("bbox") or {}
    anchor_size = max(anchor_bd.get("w", 100), anchor_bd.get("h", 100))
    proximity_threshold = anchor_size * 2.0
    anchor_cx = anchor_bd.get("x", 0) + anchor_bd.get("w", 0) / 2
    anchor_cy = anchor_bd.get("y", 0) + anchor_bd.get("h", 0) / 2

    # Grow union bbox to include all nearby fragments
    union_x1 = anchor_bd.get("x", 0)
    union_y1 = anchor_bd.get("y", 0)
    union_x2 = union_x1 + anchor_bd.get("w", 0)
    union_y2 = union_y1 + anchor_bd.get("h", 0)
    merged_indices = {anchor_idx}

    for i, el in logos:
        if i == anchor_idx:
            continue
        bd = el.get("bbox") or {}
        cx = bd.get("x", 0) + bd.get("w", 0) / 2
        cy = bd.get("y", 0) + bd.get("h", 0) / 2
        dist = ((cx - anchor_cx) ** 2 + (cy - anchor_cy) ** 2) ** 0.5
        if dist <= proximity_threshold:
            merged_indices.add(i)
            union_x1 = min(union_x1, bd.get("x", 0))
            union_y1 = min(union_y1, bd.get("y", 0))
            union_x2 = max(union_x2, bd.get("x", 0) + bd.get("w", 0))
            union_y2 = max(union_y2, bd.get("y", 0) + bd.get("h", 0))

    # Position hint from topmost fragment (most accurate for placement)
    topmost = min(
        ((i, e) for i, e in logos if i in merged_indices),
        key=lambda x: (x[1].get("bbox") or {}).get("y", 0),
    )[1]

    result = []
    logo_inserted = False
    for i, el in enumerate(elements):
        if el.get("type") != "logo":
            result.append(el)
        elif i in merged_indices:
            if not logo_inserted:
                merged_logo = dict(anchor_logo)
                merged_logo["bbox"] = {
                    "x": union_x1, "y": union_y1,
                    "w": union_x2 - union_x1, "h": union_y2 - union_y1,
                }
                merged_logo["position_hint"] = topmost.get("position_hint", "top_center")
                result.append(merged_logo)
                logo_inserted = True
        else:
            # Distant logo — reclassify as image/badge (separate graphical element to crop)
            bd = el.get("bbox") or {}
            result.append({
                "type": "image",
                "subtype": "badge",
                "bbox": bd,
            })
    return result


def _preprocess_for_ocr(img: Image.Image) -> Image.Image:
    """
    Adaptive preprocessing for Surya OCR only. Claude always receives the original.
    - Dark/photo backgrounds (e.g. valentines menu): adaptive threshold → clean B&W text
    - Watermark/illustration backgrounds (e.g. kids menu): CLAHE contrast boost
    - Clean white backgrounds: returned unchanged
    """
    if not _CV2_AVAILABLE:
        return img

    arr = np.array(img.convert("RGB"))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape

    # Sample corners to determine background type
    margin = max(30, min(60, h // 10, w // 10))
    corners = np.concatenate([
        gray[:margin, :margin].flatten(),
        gray[:margin, -margin:].flatten(),
        gray[-margin:, :margin].flatten(),
        gray[-margin:, -margin:].flatten(),
    ])
    bg_brightness = float(np.median(corners))

    if bg_brightness < 160:
        # Dark background — adaptive threshold produces clean black-on-white for Surya
        binary = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 31, 15
        )
        rgb = cv2.cvtColor(binary, cv2.COLOR_GRAY2RGB)
        print(f"[preprocess] dark bg ({bg_brightness:.0f}) → adaptive threshold")
        return Image.fromarray(rgb)
    elif bg_brightness < 235:
        # Mid-brightness (watermarks, illustrations, tinted) — CLAHE contrast boost
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(16, 16))
        enhanced = clahe.apply(gray)
        rgb = cv2.cvtColor(enhanced, cv2.COLOR_GRAY2RGB)
        print(f"[preprocess] mid-brightness bg ({bg_brightness:.0f}) → CLAHE")
        return Image.fromarray(rgb)
    else:
        return img  # clean white — no preprocessing needed


_HYBRID_SYSTEM_PROMPT = """\
You are a High-Precision Restaurant Menu Layout Extractor. Follow this EXACT process order.

You receive TWO images of the same menu at identical pixel dimensions:
- IMAGE 1: Clean original — use to READ text accurately and measure positions
- IMAGE 2: Same image with numbered Set-of-Marks boxes — use to match OCR block IDs [1, 2, 3] 
  and Graphical Candidate IDs [G1, G2, G3].

═══ PROCESS ORDER (follow sequentially) ═══

STEP 1 — SKELETON SCAN (do before anything else):
Visually scan IMAGE 1 for ALL section/category headers in ANY font style —
cursive, script, handwritten, bold serif, or decorative.
...
STEP 2 — LABEL OCR BLOCKS:
For each numbered block [1, 2, 3] in the list: assign subtype, column (0=left, 1=right),
font_family, and corrected_text if OCR garbled the text.

STEP 3 — LABEL GRAPHICAL CANDIDATES:
For each Magenta block [G1, G2, G3] in IMAGE 2: identify its type in graphic_labels.
These are candidates detected by a vision pre-pass. Use graphic_labels to assign 
subtype (badge, ornament, collage_box) and semantic_label.

STEP 4 — DECORATIVE ELEMENTS:
For each section header from Step 1 that has NO corresponding numbered OCR block:
  a) Find the OCR block immediately BELOW it in the same column from the provided list.
  b) Set bbox bottom edge (y + h) = that block's y − 4px. Estimate height visually.
  c) Measure x and width directly from IMAGE 1.
  If no OCR block is below it in its column: estimate from IMAGE 1 visual position.
Include ALL headers from Step 1 — completeness here is the #1 accuracy driver.

STEP 5 — LOGO BBOX:
Draw ONE unified bbox covering the COMPLETE restaurant branding block:
  - All name lines (including small "the", large name, location line)
  - Decorative ornament lines (e.g. "——— SARASOTA ———")
  - Any graphic element (emblem, crest, swash, circular frame behind text)
  Add generous ~15px padding on all sides. Never split into parts.
  The MAIN logo is the primary restaurant branding — usually at the top of the menu.
  Secondary logos (footer variants, location badges) go in graphic_elements, NOT here.

STEP 6 — GRAPHIC ELEMENTS (non-text graphical regions):
CRITICAL: Scan IMAGE 1 specifically for graphical elements that look like badges, circles, 
or bordered boxes. 

RECOGNIZING BADGES & BOXES (Zero Tolerance for Missing):
1. "As seen on:" panel (bottom left) — This is a bordered box with multiple small logos 
   (Food Network, YouTube, Hulu). You MUST report the ENTIRE bordered box as one 
   collage_box. Do NOT just label the text inside it.
2. Circular Badges (right side) — Look for the large "food network" circle and the 
   "Diners' Choice" circle. These are graphical badges. You MUST capture the entire 
   circle as a badge, even if OCR found text inside it.
3. Ornaments — Symmetrical calligraphic swashes below section headers.

If a region contains BOTH text and a graphical container (like a circle or a box), 
you MUST include it in graphic_labels (if it has a G# box) OR graphic_elements. 
Failing to capture these containers makes the menu look empty and incomplete.

BBOX RULES:
- ALL coordinates in IMAGE 1 pixel space
- Anchor decorative element bboxes to nearby OCR block y-coordinates for precision
- font_family: "decorative-script" for cursive/calligraphy, "serif", "sans-serif", "display"
- column: 0=left or single column, 1=right column
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
                    "restaurant_name": {
                        "type": "string",
                        "description": "The name of the restaurant. If not explicitly in OCR, extract from visual branding/logo."
                    },
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
            "graphic_elements": {
                "type": "array",
                "description": (
                    "Non-text graphical regions to crop and embed as images. "
                    "Do NOT include the main restaurant logo (use logo_bbox for that). "
                    "Types: ornament (swash below section header), badge (brand circle/icon), "
                    "collage_box (bordered multi-logo panel like 'As seen on' box)."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "subtype": {
                            "type": "string",
                            "enum": ["ornament", "badge", "collage_box"],
                        },
                        "semantic_label": {
                            "type": ["string", "null"],
                            "description": (
                                "Canonical slug identifying the asset for S3 library lookup. "
                                "Use EXACTLY one of these slugs when you recognise the element, "
                                "otherwise set null:\n"
                                "Badges: badge/food_network | badge/opentable_diners_choice | "
                                "badge/youtube | badge/hulu | badge/tripadvisor | badge/yelp | "
                                "badge/michelin | badge/zagat | badge/best_of\n"
                                "Ornaments: ornament/floral_swash_centered | ornament/floral_swash_left | "
                                "ornament/calligraphic_rule | ornament/diamond_rule | "
                                "ornament/vine_separator | ornament/scroll_divider\n"
                                "Separators: separator/wavy_line | separator/double_line | separator/dotted_ornament"
                            ),
                        },
                        "bbox": {
                            "type": "object",
                            "properties": {
                                "x": {"type": "number"}, "y": {"type": "number"},
                                "w": {"type": "number"}, "h": {"type": "number"},
                            },
                            "required": ["x", "y", "w", "h"],
                        },
                    },
                    "required": ["subtype", "bbox"],
                },
            },
            "graphic_labels": {
                "type": "array",
                "description": "Label the detected graphical candidate boxes (G1, G2, etc.) from IMAGE 2.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "The G# ID from IMAGE 2 (e.g. G1, G2)."},
                        "subtype": {
                            "type": "string",
                            "enum": ["ornament", "badge", "collage_box"],
                        },
                        "semantic_label": {
                            "type": ["string", "null"],
                            "description": "Canonical slug (e.g. badge/food_network, badge/opentable_diners_choice).",
                        },
                    },
                    "required": ["id", "subtype"],
                },
            },
        },
        "required": ["background_color", "ocr_labels", "decorative_elements", "menu_data", "graphic_labels"],
    },
}


# ---------------------------------------------------------------------------
# Phase 3 — Surya OCR + Set-of-Marks (Precision Engine)
# ---------------------------------------------------------------------------

_surya_det_model = None
_surya_det_processor = None
_surya_rec_model = None
_surya_rec_processor = None
_surya_det_predictor = None        # surya >=0.17 API
_surya_rec_predictor = None        # surya >=0.17 API
_surya_foundation_predictor = None # surya >=0.17 API
_surya_api_version = None          # "new" | "old"


def _load_surya_models() -> bool:
    """Lazy-load and cache Surya OCR models. Returns True if successful."""
    global _surya_det_model, _surya_det_processor, _surya_rec_model, _surya_rec_processor
    global _surya_det_predictor, _surya_rec_predictor, _surya_foundation_predictor, _surya_api_version
    if _surya_api_version is not None:
        return True

    # Skip all HuggingFace network HEAD requests — models are already cached locally.
    # Without this, every startup wastes ~90s on 5-retry DNS failures per model file.
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
    # Batch recognition lines together for faster MPS throughput (default is often 1)
    os.environ.setdefault("RECOGNITION_BATCH_SIZE", "32")
    os.environ.setdefault("DETECTOR_BATCH_SIZE", "1")

    try:
        # Enable MPS (Apple Silicon GPU) if available
        try:
            import torch as _torch
            if _torch.backends.mps.is_available():
                os.environ.setdefault("TORCH_DEVICE", "mps")
                os.environ.setdefault("RECOGNITION_DEVICE", "mps")
                os.environ.setdefault("DETECTOR_DEVICE", "mps")
                print("[surya] MPS (Apple Silicon GPU) enabled")
        except Exception:
            pass

        # Try new API first (surya >= 0.17)
        try:
            from surya.foundation import FoundationPredictor
            from surya.recognition import RecognitionPredictor
            from surya.detection import DetectionPredictor
            import torch
            
            device = "mps" if torch.backends.mps.is_available() else "cpu"
            print(f"[surya] loading models on {device}...")
            
            _surya_foundation_predictor = FoundationPredictor(device=device)
            _surya_rec_predictor = RecognitionPredictor(_surya_foundation_predictor)
            _surya_det_predictor = DetectionPredictor(device=device)
            _surya_api_version = "new"
            print(f"[surya] models ready on {device} (API v0.17+)")
            return True
        except ImportError:
            pass

        # Fall back to old API (surya 0.4.x)
        from surya.model.detection.segformer import (
            load_model as _det_model,
            load_processor as _det_proc,
        )
        from surya.model.recognition.model import load_model as _rec_model
        from surya.model.recognition.processor import load_processor as _rec_proc
        import torch
        
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        print(f"[surya] loading old models on {device}...")
        
        _surya_det_model = _det_model()
        if device == "mps":
            _surya_det_model = _surya_det_model.to("mps")
            
        _surya_det_processor = _det_proc()
        _surya_rec_model = _rec_model()
        if device == "mps":
            _surya_rec_model = _surya_rec_model.to("mps")
            
        _surya_rec_processor = _rec_proc()
        _surya_api_version = "old"
        print(f"[surya] models ready on {device} (API v0.4.x)")
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
        if _surya_api_version == "new":
            results = _surya_rec_predictor([img], det_predictor=_surya_det_predictor)
        else:
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


def detect_graphical_candidates(img: Image.Image) -> list[dict]:
    """
    Use OpenCV to find candidate regions for badges (circles) and collage boxes (rectangles).
    Returns list of {'bbox': [x1, y1, x2, y2], 'type': 'candidate_badge'|'candidate_box'}.
    """
    if not _CV2_AVAILABLE:
        return []

    # Convert to grayscale and threshold
    arr = np.array(img.convert("RGB"))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    
    # Use a larger blur to remove text and focus on shapes
    blurred = cv2.GaussianBlur(gray, (7, 7), 0)
    
    # Adaptive threshold to find edges/borders
    thresh = cv2.adaptiveThreshold(blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
                                 cv2.THRESH_BINARY_INV, 11, 2)
    
    # Close gaps in shapes
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
    
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    candidates = []
    h, w = gray.shape
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < 2000 or area > (h * w * 0.4): # Skip too small or too large
            continue
            
        x, y, cw, ch = cv2.boundingRect(cnt)
        aspect_ratio = float(cw) / ch
        
        # 1. Check for circularity (Badges)
        peri = cv2.arcLength(cnt, True)
        circularity = 4 * np.pi * area / (peri * peri) if peri > 0 else 0
        
        if 0.7 < circularity < 1.2 and 0.8 < aspect_ratio < 1.2:
            candidates.append({
                "bbox": [float(x), float(y), float(x + cw), float(y + ch)],
                "type": "candidate_badge"
            })
            continue

        # 2. Check for rectangularity (Boxes)
        rect = cv2.minAreaRect(cnt)
        box_area = rect[1][0] * rect[1][1]
        extent = area / box_area if box_area > 0 else 0
        
        if extent > 0.8: # Very solid rectangle
            candidates.append({
                "bbox": [float(x), float(y), float(x + cw), float(y + ch)],
                "type": "candidate_box"
            })

    # Non-max suppression for overlapping candidates
    if not candidates:
        return []
        
    candidates.sort(key=lambda x: (x["bbox"][2]-x["bbox"][0]) * (x["bbox"][3]-x["bbox"][1]), reverse=True)
    final = []
    for cand in candidates:
        is_covered = False
        cb = {"x": cand["bbox"][0], "y": cand["bbox"][1], 
              "w": cand["bbox"][2]-cand["bbox"][0], "h": cand["bbox"][3]-cand["bbox"][1]}
        for f in final:
            fb = {"x": f["bbox"][0], "y": f["bbox"][1], 
                  "w": f["bbox"][2]-f["bbox"][0], "h": f["bbox"][3]-f["bbox"][1]}
            if _bbox_iou(cb, fb) > 0.5:
                is_covered = True
                break
        if not is_covered:
            final.append(cand)
            
    return final


def _draw_som_annotations(img: Image.Image, blocks: list, graphic_candidates: list = None) -> Image.Image:
    """Draw semi-transparent numbered bounding boxes on a copy of img (Set-of-Marks).
    Alpha-blended fill ensures decorative/cursive text underneath remains visible to Claude."""
    from PIL import ImageDraw
    base = img.copy().convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    # 1. Draw Surya OCR blocks (Colored)
    for i, block in enumerate(blocks):
        color = _SOM_PALETTE[i % len(_SOM_PALETTE)]
        r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
        x1, y1, x2, y2 = block["bbox"]
        # Semi-transparent fill (~24% opacity) — underlying text remains readable
        draw.rectangle([x1, y1, x2, y2], fill=(r, g, b, 60), outline=(r, g, b, 220), width=2)
        lbl = str(i + 1)
        lbl_w, lbl_h = len(lbl) * 9 + 6, 18
        lbl_x = x1
        lbl_y = max(0, y1 - lbl_h - 1)
        draw.rectangle([lbl_x, lbl_y, lbl_x + lbl_w, lbl_y + lbl_h], fill=(r, g, b, 220))
        draw.text((lbl_x + 3, lbl_y + 2), lbl, fill=(255, 255, 255, 255))
        
    # 2. Draw Graphic Candidates (Magenta)
    if graphic_candidates:
        magenta = (255, 0, 255) # Magenta for graphic candidates
        for i, cand in enumerate(graphic_candidates):
            x1, y1, x2, y2 = cand["bbox"]
            # Outline only for graphics, no fill to avoid obscuring details
            draw.rectangle([x1, y1, x2, y2], outline=(*magenta, 255), width=4)
            lbl = f"G{i + 1}"
            lbl_w, lbl_h = len(lbl) * 12 + 8, 22
            lbl_x = x1
            lbl_y = max(0, y1 - lbl_h - 2)
            draw.rectangle([lbl_x, lbl_y, lbl_x + lbl_w, lbl_y + lbl_h], fill=(*magenta, 255))
            draw.text((lbl_x + 4, lbl_y + 3), lbl, fill=(255, 255, 255, 255))

    result = Image.alpha_composite(base, overlay)
    return result.convert("RGB")


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

    # Preprocess for Surya OCR only (dark/watermark backgrounds).
    # Claude always receives the clean original for accurate visual reading.
    surya_img = _preprocess_for_ocr(img)
    surya_blocks = extract_blocks_surya(surya_img)
    if len(surya_blocks) < 3:
        print("[surya_som] too few blocks — skipping")
        return None

    # Detect potential graphical regions (badges, boxes) to help Claude find them
    graphic_candidates = detect_graphical_candidates(img)
    print(f"[surya_som] {len(graphic_candidates)} graphical candidates found")

    # Apply Set-of-Marks (SoM) annotations to the ORIGINAL image before sending to Claude.
    # Annotations on clean original — not on preprocessed — so Claude sees actual visual.
    annotated_img = _draw_som_annotations(img, surya_blocks, graphic_candidates)
    
    orig_w, orig_h = img.size
    send_img = annotated_img
    clean_send = img  # clean version sent alongside annotated for accurate text/logo reading
    scale_x = scale_y = 1.0
    if max(orig_w, orig_h) > _MAX_IMG_DIM:
        ratio = _MAX_IMG_DIM / max(orig_w, orig_h)
        new_w = max(1, int(orig_w * ratio))
        new_h = max(1, int(orig_h * ratio))
        send_img = annotated_img.resize((new_w, new_h), Image.LANCZOS)
        clean_send = img.resize((new_w, new_h), Image.LANCZOS)  # same dims as annotated
        scale_x = orig_w / new_w
        scale_y = orig_h / new_h

    # Encode annotated image (IMAGE 2 — for OCR block spatial reference)
    buf = io.BytesIO()
    send_img.convert("RGB").save(buf, format="JPEG", quality=85)
    annotated_b64 = base64.standard_b64encode(buf.getvalue()).decode()

    # Encode clean image (IMAGE 1 — for accurate text reading and decorative element location)
    clean_buf = io.BytesIO()
    clean_send.convert("RGB").save(clean_buf, format="JPEG", quality=85)
    clean_b64 = base64.standard_b64encode(clean_buf.getvalue()).decode()

    # Block list for Claude — absolute pixel coords in Claude's image space (sw×sh)
    # so Claude can anchor decorative element bboxes precisely relative to OCR blocks.
    lines = []
    for i, b in enumerate(surya_blocks):
        x1, y1, x2, y2 = b["bbox"]
        # Convert from original (upscaled) image coords → Claude's send_img coords
        cx1 = x1 / scale_x if scale_x != 1.0 else x1
        cy1 = y1 / scale_y if scale_y != 1.0 else y1
        cw  = (x2 - x1) / scale_x if scale_x != 1.0 else (x2 - x1)
        ch  = (y2 - y1) / scale_y if scale_y != 1.0 else (y2 - y1)
        text = b["text"] if len(b["text"]) <= 80 else b["text"][:77] + "..."
        lines.append(
            f"[{i + 1}] \"{text}\" — x={cx1:.0f} y={cy1:.0f} w={cw:.0f} h={ch:.0f}"
        )
    block_list = "\n".join(lines)

    # Add graphical candidate list
    g_lines = []
    for i, g in enumerate(graphic_candidates):
        x1, y1, x2, y2 = g["bbox"]
        cx1, cy1 = x1 / scale_x, y1 / scale_y
        cw, ch = (x2 - x1) / scale_x, (y2 - y1) / scale_y
        g_lines.append(f"[G{i + 1}] {g['type']} — x={cx1:.0f} y={cy1:.0f} w={cw:.0f} h={ch:.0f}")
    graphic_list = "\n".join(g_lines)

    sw, sh = send_img.size
    user_msg = (
        f"Both images are {sw}×{sh}px. All bbox values must be in this pixel space.\n\n"
        "Follow the 6-step process from the system prompt exactly.\n\n"
        "═══ STEP 1 — SKELETON SCAN ═══\n"
        "Scan IMAGE 1 now. Identify every section/category header.\n\n"
        "═══ STEP 2 — OCR BLOCKS ═══\n"
        f"Surya OCR extracted {len(surya_blocks)} blocks:\n"
        f"{block_list}\n\n"
        "═══ STEP 3 — GRAPHICAL CANDIDATES ═══\n"
        "Pre-pass detected potential graphical regions (Magenta boxes G1, G2, etc.):\n"
        f"{graphic_list or '(none)'}\n"
        "Identify these in the graphic_labels tool field.\n\n"
        "═══ STEP 4 — DECORATIVE ELEMENTS ═══\n"
        "SECTION HEADERS: For every cursive/script header from Step 1 with no numbered OCR block.\n\n"
        "═══ STEP 5 — LOGO BBOX ═══\n"
        "Draw ONE bbox tightly around the PRIMARY restaurant branding in IMAGE 1.\n\n"
        "═══ STEP 6 — GRAPHIC ELEMENTS ═══\n"
        "Scan IMAGE 1 for ANY OTHER non-text graphical regions not already labeled.\n"
        "CRITICAL — use exact semantic_label slugs:\n"
        "  Food Network → badge/food_network\n"
        "  OpenTable / Diners' Choice → badge/opentable_diners_choice\n"
        "  YouTube → badge/youtube   Hulu → badge/hulu\n"
        "  TripAdvisor → badge/tripadvisor   Yelp → badge/yelp\n"
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=16384,
            system=_HYBRID_SYSTEM_PROMPT,
            tools=[_HYBRID_TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": "label_menu_layout"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "IMAGE 1 — Clean original (use for reading text and locating logo/decorative elements):"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": clean_b64}},
                    {"type": "text", "text": "IMAGE 2 — Annotated with numbered boxes (use for OCR block identification only):"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": annotated_b64}},
                    {"type": "text", "text": user_msg},
                ],
            }],
        )
    except (anthropic.RateLimitError, anthropic.APIError) as exc:
        # Re-raise to trigger the Gemini fallback in pipeline.py
        raise exc

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
        # Infer position_hint from actual bbox position rather than hardcoding top_center
        _lb_cx = lb.get("x", 0) + lb.get("w", 0) / 2
        _lb_y  = lb.get("y", 0)
        _py = "top" if _lb_y < orig_h * 0.4 else ("middle" if _lb_y < orig_h * 0.7 else "bottom")
        _px = "left" if _lb_cx < orig_w * 0.35 else ("right" if _lb_cx > orig_w * 0.65 else "center")
        elements.append({"type": "logo", "bbox": lb, "position_hint": f"{_py}_{_px}"})

    # Process graphic_labels (G# mappings)
    graphic_label_map = {lbl.get("id"): lbl for lbl in data.get("graphic_labels", []) if lbl.get("id")}
    for i, g in enumerate(graphic_candidates):
        lbl = graphic_label_map.get(f"G{i+1}")
        if lbl:
            x1, y1, x2, y2 = g["bbox"]
            elements.append({
                "type": "image",
                "subtype": lbl.get("subtype", "ornament"),
                "semantic_label": lbl.get("semantic_label"),
                "bbox": {
                    "x": float(x1), "y": float(y1),
                    "w": float(x2 - x1), "h": float(y2 - y1),
                },
            })

    # Graphic elements (ornaments, badges, collage boxes) — crop-and-embed in pipeline
    # semantic_label is forwarded so pipeline.py can resolve clean assets from S3.
    for ge in data.get("graphic_elements", []):
        bd = ge.get("bbox") or {}
        if scale_x != 1.0 or scale_y != 1.0:
            bd = {
                "x": bd.get("x", 0) * scale_x, "y": bd.get("y", 0) * scale_y,
                "w": bd.get("w", 0) * scale_x,  "h": bd.get("h", 0) * scale_y,
            }
        if bd.get("w", 0) > 5 and bd.get("h", 0) > 5:
            elements.append({
                "type": "image",
                "subtype": ge.get("subtype", "ornament"),
                "semantic_label": ge.get("semantic_label"),  # e.g. 'badge/food_network'
                "bbox": {
                    "x": float(bd.get("x", 0)), "y": float(bd.get("y", 0)),
                    "w": max(1.0, float(bd.get("w", 0))), "h": max(1.0, float(bd.get("h", 0))),
                },
            })

    print(f"[surya_som] built {len(elements)} elements "
          f"(ocr={len(surya_blocks)}, decorative={len(data.get('decorative_elements', []))}, "
          f"graphics={len(data.get('graphic_elements', []))})")
    
    # --- Post-processing (The 'Precision Engine' cleanup) ---
    # 1. Deduplicate text (Word-match merge Surya vs Claude's hallucinated decorative copies)
    elements = _dedup_text_elements(elements)

    # 2. Snap decorative headers to content below and center in column
    elements = _snap_decorative_headers(elements, orig_w=orig_w)

    # 3. Mask any text/separators inside the logo area
    elements = _mask_logo_elements(elements)

    # 4. Verification pass — second Claude call with overlay image to catch missed headers
    elements = _verification_pass(img, elements, orig_w, orig_h)

    # 5. Re-snap after any newly added elements from verification pass
    elements = _snap_decorative_headers(elements, orig_w=orig_w)

    return {
        "elements": elements,
        "menu_data": data.get("menu_data", {}),
        "background_color": data.get("background_color", "#ffffff"),
    }


def _dedup_separators(elements: list) -> list:
    """
    Deduplicate separator elements using proximity rather than IoU.
    IoU fails for thin lines — a 2px separator shifted by 4px from JPEG/resize
    artifacts has IoU=0 and both copies survive the merge. This function treats
    two separators as duplicates when:
      - Same orientation
      - Perpendicular-axis centers within max(1.5 * thickness, 12) px
      - >= 60% parallel-axis overlap
    Keeps the one with the greater span (length or thickness).
    Non-separator elements pass through unchanged.
    """
    seps, non_seps = [], []
    for el in elements:
        (seps if el.get("type") == "separator" else non_seps).append(el)

    kept: list = []
    for sep in seps:
        bd = sep.get("bbox") or {}
        orient = sep.get("orientation", "horizontal")
        is_dup = False

        for j, ks in enumerate(kept):
            kd = ks.get("bbox") or {}
            if ks.get("orientation") != orient:
                continue

            if orient == "horizontal":
                perp_tol = max(1.5 * max(bd.get("h", 1), 1), 12)
                cy  = bd.get("y", 0) + bd.get("h", 0) / 2
                kcy = kd.get("y", 0) + kd.get("h", 0) / 2
                if abs(cy - kcy) > perp_tol:
                    continue
                x1, x2   = bd.get("x", 0), bd.get("x", 0) + bd.get("w", 0)
                kx1, kx2  = kd.get("x", 0), kd.get("x", 0) + kd.get("w", 0)
                overlap   = max(0.0, min(x2, kx2) - max(x1, kx1))
                min_span  = min(bd.get("w", 1), kd.get("w", 1))
                if overlap / max(min_span, 1) >= 0.6:
                    is_dup = True
                    if bd.get("w", 0) > kd.get("w", 0):
                        kept[j] = sep
                    break
            else:  # vertical
                perp_tol = max(1.5 * max(bd.get("w", 1), 1), 12)
                cx  = bd.get("x", 0) + bd.get("w", 0) / 2
                kcx = kd.get("x", 0) + kd.get("w", 0) / 2
                if abs(cx - kcx) > perp_tol:
                    continue
                y1, y2   = bd.get("y", 0), bd.get("y", 0) + bd.get("h", 0)
                ky1, ky2  = kd.get("y", 0), kd.get("y", 0) + kd.get("h", 0)
                overlap   = max(0.0, min(y2, ky2) - max(y1, ky1))
                min_span  = min(bd.get("h", 1), kd.get("h", 1))
                if overlap / max(min_span, 1) >= 0.6:
                    is_dup = True
                    if bd.get("h", 0) > kd.get("h", 0):
                        kept[j] = sep
                    break

        if not is_dup:
            kept.append(sep)

    return non_seps + kept


def _mask_logo_elements(elements: list) -> list:
    """
    Mask text/separator elements whose center falls inside an expanded logo zone.
    The zone extends 60px beyond the logo bbox on all sides to catch logo branding
    fragments (decorative script, thin rules, etc.) that Claude places just outside
    the reported logo_bbox but are visually part of the logo area.
    Only applies the horizontal clearance on the LOGO'S SIDE (left-aligned logos
    don't mask center-page text at the same y-band).
    """
    logos = [e for e in elements if e.get("type") == "logo"]
    if not logos:
        return elements

    result = []
    for el in elements:
        if el.get("type") == "logo":
            result.append(el)
            continue

        bd = el.get("bbox")
        if not bd:
            result.append(el)
            continue

        # Never mask structural section headers or graphic elements — they are not logo fragments.
        if el.get("subtype") == "category_header" or el.get("type") == "image":
            result.append(el)
            continue

        cx = bd.get("x", 0) + bd.get("w", 0) / 2
        cy = bd.get("y", 0) + bd.get("h", 0) / 2

        inside_logo = False
        for logo in logos:
            lbd = logo.get("bbox")
            if not lbd:
                continue
            lw = lbd.get("w", 0)
            lh = lbd.get("h", 0)
            lx = lbd.get("x", 0)
            ly = lbd.get("y", 0)
            # Expand logo clearance zone: catch misplaced logo text/fragments.
            # Downward clearance is SMALL (50px max) — prevents masking section headers
            # like "Course One" that legitimately sit just below the logo.
            clear_y = min(lh * 0.25, 50.0)
            clear_x = min(lw * 0.4, 80.0)
            lx1 = lx - clear_x
            ly1 = ly - 30
            lx2 = lx + lw + clear_x
            ly2 = ly + lh + clear_y
            if lx1 <= cx <= lx2 and ly1 <= cy <= ly2:
                inside_logo = True
                break

        if not inside_logo:
            result.append(el)
    return result


def _refine_logo_bbox_by_pixels(
    img: Image.Image,
    rough_bbox: dict,
    canvas_w: int,
    canvas_h: int,
) -> "dict | None":
    """
    Refine Claude's rough logo bbox to the true pixel extent of the logo graphic.

    Expands a search zone around the rough bbox, thresholds ink pixels (auto-detects
    dark-on-light vs light-on-dark), applies morphological closing to bridge intra-logo
    gaps (letter spacing, thin ornaments), then returns the bounding rect of all ink pixels
    mapped back to original coordinates.

    Returns a refined {x, y, w, h} dict, or None if cv2 is unavailable or result is too small.
    """
    if not _CV2_AVAILABLE:
        return None

    rx = float(rough_bbox.get("x", 0))
    ry = float(rough_bbox.get("y", 0))
    rw = float(rough_bbox.get("w", 0))
    rh = float(rough_bbox.get("h", 0))

    if rw < 10 or rh < 10:
        return None

    # Search zone: expand conservatively on x (logo width usually correct),
    # generously on y (to capture ornaments/swash below the text).
    # Small x-expansion prevents including adjacent elements like "Brunch" cursive.
    pad_x = rw * 0.15
    pad_y = rh * 0.6
    sx1 = max(0, int(rx - pad_x))
    sy1 = max(0, int(ry - pad_y))
    sx2 = min(canvas_w, int(rx + rw + pad_x))
    sy2 = min(canvas_h, min(int(ry + rh + pad_y), int(canvas_h * 0.40)))

    if sx2 <= sx1 + 10 or sy2 <= sy1 + 10:
        return None

    zone = img.crop((sx1, sy1, sx2, sy2))
    arr = np.array(zone.convert("RGB"))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    zh, zw = gray.shape

    # Detect background brightness from zone corners
    margin = max(5, min(20, zh // 8, zw // 8))
    corners = np.concatenate([
        gray[:margin, :margin].flatten(),
        gray[:margin, -margin:].flatten(),
        gray[-margin:, :margin].flatten(),
        gray[-margin:, -margin:].flatten(),
    ])
    bg_brightness = float(np.median(corners))

    # Threshold: separate ink from background
    if bg_brightness > 180:
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    else:
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # Morphological closing: bridges gaps within the logo (letter spacing, thin ornaments)
    ksize = max(7, min(zh // 15, zw // 15))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ksize, ksize))
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)

    coords = cv2.findNonZero(closed)
    if coords is None:
        return None

    ix, iy, iw, ih = cv2.boundingRect(coords)

    # Sanity: result must be at least 40% of rough bbox size in each dimension
    if iw < rw * 0.4 or ih < rh * 0.4:
        return None

    pad = 8
    abs_x = max(0, sx1 + ix - pad)
    abs_y = max(0, sy1 + iy - pad)
    abs_w = min(canvas_w - abs_x, iw + pad * 2)
    abs_h = min(canvas_h - abs_y, ih + pad * 2)

    print(f"[logo_pixel] ({abs_x:.0f},{abs_y:.0f}) {abs_w:.0f}×{abs_h:.0f}px  "
          f"(rough was x={rx:.0f},y={ry:.0f} {rw:.0f}×{rh:.0f})")
    return {"x": float(abs_x), "y": float(abs_y), "w": float(abs_w), "h": float(abs_h)}


def _render_extraction_overlay(img: Image.Image, elements: list) -> Image.Image:
    """
    Draw extracted element bboxes as colored outlines on a copy of img for verification.
    category_header → green, logo → red, other text → blue.
    """
    from PIL import ImageDraw
    overlay = img.copy().convert("RGB")
    draw = ImageDraw.Draw(overlay)
    color_map = {
        "logo": (220, 50, 50),
        "category_header": (30, 180, 30),
        "separator": (255, 140, 0),
    }
    for el in elements:
        bd = el.get("bbox") or {}
        x, y, w, h = bd.get("x", 0), bd.get("y", 0), bd.get("w", 0), bd.get("h", 0)
        if w < 1 or h < 1:
            continue
        subtype = el.get("subtype", el.get("type", ""))
        color = color_map.get(subtype) or color_map.get(el.get("type", "")) or (80, 80, 220)
        draw.rectangle([x, y, x + w, y + h], outline=color, width=2)
    return overlay


def _verification_pass(
    img: Image.Image,
    elements: list,
    canvas_w: int,
    canvas_h: int,
) -> list:
    """
    Second Claude call: send the clean image + an overlay of already-extracted elements.
    Claude spots content visible in the image but NOT covered by any extraction box.
    Missing elements are appended to the list and returned.

    Focused on section/category headers — the most critical miss type.
    """
    client = _get_client()
    if client is None:
        return elements

    # Build overlay image showing what we already extracted
    overlay_img = _render_extraction_overlay(img, elements)

    # Resize both to fit within 1400px
    max_dim = 1400
    scale = min(1.0, max_dim / max(img.width, img.height))
    send_w = max(1, int(img.width * scale))
    send_h = max(1, int(img.height * scale))
    clean_resized = img.resize((send_w, send_h), Image.LANCZOS)
    overlay_resized = overlay_img.resize((send_w, send_h), Image.LANCZOS)

    def _enc(pil_img: Image.Image) -> str:
        buf = io.BytesIO()
        pil_img.convert("RGB").save(buf, format="JPEG", quality=88)
        return base64.standard_b64encode(buf.getvalue()).decode()

    clean_b64 = _enc(clean_resized)
    overlay_b64 = _enc(overlay_resized)

    # Summarise already-extracted text for reference
    extracted_texts = sorted(
        {el.get("content", "").strip() for el in elements if el.get("type") == "text" and el.get("content")},
    )
    text_list = "\n".join(f"  • {t}" for t in extracted_texts[:80]) or "  (none)"

    verify_prompt = (
        f"These two images are {send_w}×{send_h}px.\n"
        "IMAGE 1 is the clean original menu.\n"
        "IMAGE 2 is the same image with COLORED BOXES showing already-extracted elements "
        "(green = section headers, blue = text, red = logo, orange = separators).\n\n"
        "TASK: Compare IMAGE 1 vs IMAGE 2 carefully.\n"
        "Find ANY section/category headers or important structural text that is VISIBLE in "
        "IMAGE 1 but has NO green or blue box covering it in IMAGE 2.\n\n"
        "Already extracted text (do NOT re-add these):\n"
        f"{text_list}\n\n"
        "Focus especially on:\n"
        "1. Cursive/italic headers: 'Course One', 'Breakfast', 'Lunch', 'Starters', "
        "'Broths & Greens', 'Main Dishes', 'Salads', 'For the Table', 'Wines by the glass', etc.\n"
        "2. The FIRST item in each column (bold item name at the top of each section) — "
        "these are commonly missed when they sit close to a cursive header.\n"
        "3. Price numbers in ANY tabular section (Add On, Sides, drinks table). "
        "For each ROW in a price table: the item name AND the price number must BOTH have boxes. "
        "If a price number (e.g. 3, 4, 7, 8, 4.50) has NO box, it is missing.\n"
        "4. Any text visible in IMAGE 1 that has no box at all in IMAGE 2.\n\n"
        f"Return ONLY a JSON array (use image coords 0–{send_w} x, 0–{send_h} y):\n"
        "[\n"
        "  {\"content\": \"Course One\", \"subtype\": \"category_header\", "
        "\"font_family\": \"decorative-script\", \"column\": 0, "
        "\"bbox\": {\"x\": 80, \"y\": 230, \"w\": 180, \"h\": 45}}\n"
        "]\n"
        "Return [] if nothing is missing. Return ONLY the JSON array, no explanation."
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "IMAGE 1 — Clean original:"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": clean_b64}},
                    {"type": "text", "text": "IMAGE 2 — Extraction overlay:"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": overlay_b64}},
                    {"type": "text", "text": verify_prompt},
                ],
            }],
        )

        raw = (response.content[0].text or "").strip() if response.content else "[]"
        # Extract JSON array
        import re as _re
        match = _re.search(r"\[.*\]", raw, _re.DOTALL)
        if not match:
            print("[verify_pass] no JSON array in response")
            return elements

        missing = json.loads(match.group())
        if not missing:
            print("[verify_pass] nothing missing — extraction complete")
            return elements

        print(f"[verify_pass] found {len(missing)} missing elements")
        inv_scale = 1.0 / scale  # map back to original image coords

        for dec in missing:
            bd = dec.get("bbox") or {}
            abs_bd = {
                "x": float(bd.get("x", 0)) * inv_scale,
                "y": float(bd.get("y", 0)) * inv_scale,
                "w": float(bd.get("w", 100)) * inv_scale,
                "h": float(bd.get("h", 30)) * inv_scale,
            }
            elem_h = max(1.0, abs_bd["h"])
            elements.append({
                "type": "text",
                "subtype": dec.get("subtype", "category_header"),
                "content": dec.get("content", ""),
                "bbox": abs_bd,
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

        return elements

    except Exception as exc:
        print(f"[verify_pass] failed: {exc}")
        return elements


def _snap_decorative_headers(elements: list, orig_w: float = 1200) -> list:
    """
    Post-processing: anchor cursive section headers to just above the first
    Surya-detected (non-decorative) text block directly below them in the same column.

    Claude's decorative element y-estimates can be off by 50-150px.  Surya's OCR
    blocks are pixel-accurate.  This function snaps each decorative header so its
    bottom sits 6px above the nearest content block below it, eliminating the
    visual overlap between section headers and the "choose one" / item lines.

    Only adjusts y (and h if the estimated height is unreasonably large).
    Also centers headers in their assigned column if they are 'category_header's.
    """
    # Collect non-decorative text blocks sorted by (column, y)
    content_blocks = [
        e for e in elements
        if e.get("type") == "text"
        and e.get("style", {}).get("font_family") not in ("decorative-script", "display")
        and e.get("subtype") != "category_header"
    ]

    result = list(elements)
    for i, el in enumerate(result):
        if el.get("type") != "text":
            continue
        
        is_decorative = el.get("style", {}).get("font_family") in ("decorative-script", "display")
        is_header = el.get("subtype") == "category_header"
        
        if not (is_decorative or is_header):
            continue

        el_col = el.get("column", 0)
        el_bd = el.get("bbox", {})
        el_cy = el_bd.get("y", 0) + (el_bd.get("h", 40) / 2) # use center y
        el_h = el_bd.get("h", 40)

        # Find the first content block in the same column that starts below
        # this decorative element.
        candidates = [
            b for b in content_blocks
            if b.get("column", 0) == el_col
            and b.get("bbox", {}).get("y", 0) > el_cy
        ]
        if not candidates:
            continue

        first_below = min(candidates, key=lambda b: b["bbox"]["y"])
        first_y = first_below["bbox"]["y"]

        gap = 6.0 # Slightly larger gap for better visual breathing room
        # Cap height: decorative headers are rarely taller than 80px
        capped_h = min(el_h, 80.0)
        new_y = first_y - gap - capped_h

        if new_y < 0:
            new_y = max(0.0, first_y - gap - capped_h)

        # --- Horizontal Snapping ---
        # Center headers within the ACTUAL x-span of their column's content blocks.
        # Using equal column widths (orig_w / num_cols) is wrong for menus with
        # an asymmetric layout (e.g., narrow left box + wide right section).
        new_x = el_bd.get("x", 0)
        if el.get("style", {}).get("text_align") == "center" or is_header:
            col_blocks_for_span = [
                b for b in content_blocks if b.get("column", 0) == el_col
            ]
            if col_blocks_for_span:
                # True center based on actual OCR block positions in this column
                col_x_min = min(b["bbox"]["x"] for b in col_blocks_for_span)
                col_x_max = max(b["bbox"]["x"] + b["bbox"].get("w", 0) for b in col_blocks_for_span)
                col_center = (col_x_min + col_x_max) / 2
            else:
                # Fallback: equal division when no content blocks found
                max_col = max((e.get("column", 0) for e in elements if e.get("type") == "text"), default=0)
                num_cols = max_col + 1
                col_w = orig_w / num_cols
                col_center = (el_col * col_w) + (col_w / 2)
            new_x = col_center - (el_bd.get("w", 100) / 2)

        if abs(new_y - el_bd.get("y", 0)) > 2 or abs(new_x - el_bd.get("x", 0)) > 2:
            result[i] = dict(el)
            result[i]["bbox"] = dict(el_bd)
            result[i]["bbox"]["y"] = float(new_y)
            result[i]["bbox"]["x"] = float(new_x)
            result[i]["bbox"]["h"] = float(capped_h)
            if result[i].get("style"):
                result[i]["style"] = dict(result[i]["style"])
                result[i]["style"]["font_size"] = round(capped_h * 0.75, 1)
                result[i]["style"]["text_align"] = "center"

    return result


def merge_layouts(primary: dict | None, secondary: dict | None,
                  math_first: bool = False, orig_w: float = 1200) -> dict | None:
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
            existing_logo_bboxes = [e["bbox"] for e in merged if e.get("type") == "logo" and e.get("bbox")]
            max_iou = max((_bbox_iou(bbox, lb) for lb in existing_logo_bboxes), default=0.0)
            if max_iou < 0.3:
                merged.append(el)
                primary_has_logo = True
            continue

        if math_first:
            # In Surya-primary mode, only include holistic text that has zero/trivial
            # spatial overlap with any Surya box (IoU < 0.05). These are elements Surya
            # missed entirely — e.g., large cursive section headers in decorative script
            # fonts. Holistic elements that overlap Surya boxes are dropped; Surya's
            # pixel-accurate coordinates win for those regions.
            max_iou_check = max((_bbox_iou(bbox, pb) for pb in primary_bboxes), default=0.0)
            if max_iou_check >= 0.05:
                continue
            # Falls through to the IoU add-check below

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

    # Proximity-based separator dedup: catches near-duplicate thin lines from parallel
    # extraction passes that IoU alone misses (2px shift → IoU=0 but same visual line).
    merged = _dedup_separators(merged)

    # Logo union: merge nearby logo fragments into a single union bbox rather than
    # discarding — Claude often splits a complex logo (emblem + frame) into parts.
    merged = _enforce_single_logo(merged)

    # Post-processing: snap shifted decorative headers to their content below.
    # Eliminates overlap between cursive headers and item text.
    merged = _snap_decorative_headers(merged, orig_w=orig_w)

    # Logo masking: delete text/separators inside the detected logo area to prevent duplicates
    merged = _mask_logo_elements(merged)

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
        cat = MenuCategory(
            name=str(cat_d.get("name") or ""),
            column=int(cat_d.get("column") or 0)
        )
        for item_d in cat_d.get("items", []):
            cat.items.append(MenuItem(
                name=str(item_d.get("name") or ""),
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
