import io
import os
import json
import base64

# Must be set before torch is imported so the MPS driver respects it from startup.
# Disables the MPS high-watermark limit — prevents OOM on the second Surya chunk.
os.environ.setdefault("PYTORCH_MPS_HIGH_WATERMARK_RATIO", "0.0")

import anthropic
from typing import List, Dict, Any
from dotenv import load_dotenv
from PIL import Image

try:
    import cv2
    import numpy as np
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False

from models import MenuData, MenuCategory, MenuItem

load_dotenv(override=True)

_client: anthropic.Anthropic | None = None
_LOGO_TEMPLATES: dict[str, np.ndarray] = {}

def _load_logo_templates():
    global _LOGO_TEMPLATES
    if not _CV2_AVAILABLE:
        return
    asset_dir = os.path.join(os.path.dirname(__file__), "local_assets")
    if not os.path.exists(asset_dir):
        return
    
    # Target logos/badges for template matching
    target_stems = [
        "youtube", "yelp", "tripadvisor", "food_network", "hulu", 
        "michelin", "zagat", "opentable_diners_choice", "best_of"
    ]
    
    for stem in target_stems:
        path = os.path.join(asset_dir, f"{stem}.png")
        if os.path.exists(path):
            tpl = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
            if tpl is not None:
                _LOGO_TEMPLATES[stem] = tpl
    if _LOGO_TEMPLATES:
        print(f"[claude_extractor] Loaded {len(_LOGO_TEMPLATES)} logo templates from local_assets")

_load_logo_templates()


def match_badges(img: Image.Image) -> List[Dict[str, Any]]:
    """
    Use OpenCV template matching to find known badges (Tripadvisor, Yelp, etc.) in the menu.
    Returns a list of detected badge elements with semantic labels and exact bboxes.
    """
    if not _CV2_AVAILABLE or not _LOGO_TEMPLATES:
        return []
    
    # Convert PIL to OpenCV grayscale
    gray_img = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2GRAY)
    results = []
    
    # Threshold for template matching confidence
    THRESHOLD = 0.75
    
    for stem, tpl in _LOGO_TEMPLATES.items():
        th, tw = tpl.shape[:2]
        
        # Template matching
        res = cv2.matchTemplate(gray_img, tpl, cv2.TM_CCOEFF_NORMED)
        loc = np.where(res >= THRESHOLD)
        
        # Group near-duplicate detections
        rects = []
        for pt in zip(*loc[::-1]):
            rects.append([int(pt[0]), int(pt[1]), int(tw), int(th)])
        
        # Non-maximum suppression to handle overlapping boxes for the same template
        rects, _ = cv2.groupRectangles(rects, 1, 0.2)
        
        for (x, y, w, h) in rects:
            # Add semantic prefix for s3_asset_library resolution later
            semantic_label = f"badge/{stem}"
            results.append({
                "type": "image",
                "subtype": "badge",
                "semantic_label": semantic_label,
                "bbox": {"x": float(x), "y": float(y), "w": float(w), "h": float(h)},
                "id": f"matched_{stem}_{x}_{y}"
            })
            
    return results


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
- Section headers in cursive/handwriting/decorative script are category_header — do NOT skip them.
  Words like "Sharable", "Starters", "Entrées", "Sides", "Desserts", "Cocktails", "Wine",
  "Brunch", "Lunch", "Dinner" are category_header even when they are a single word.
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
    # NOTE: strict=True (Anthropic structured outputs) requires
    # additionalProperties:false on every nested object. Schema needs to be
    # restructured before enabling — Pydantic validation downstream protects
    # us for now.
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
                                "column": {"type": "integer", "enum": [0, 1, 2]}
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
                            "description": "A graphical logo region (the restaurant's primary wordmark/emblem).",
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
                        },
                        {
                            "type": "object",
                            "description": (
                                "An image element: a badge, ornament, or collage_box. "
                                "Badges are circular brand/award icons (Food Network, Diners' "
                                "Choice, YouTube, Yelp, TripAdvisor, etc.). Ornaments are "
                                "decorative flourishes/swashes. collage_box is the 'As seen on' "
                                "panel with multiple logos. Always set semantic_label to one of "
                                "the canonical S3 slugs when recognized."
                            ),
                            "properties": {
                                "type": {"type": "string", "enum": ["image"]},
                                "subtype": {
                                    "type": "string",
                                    "enum": ["badge", "ornament", "collage_box"]
                                },
                                "bbox": {
                                    "type": "object",
                                    "properties": {
                                        "x": {"type": "number"}, "y": {"type": "number"},
                                        "w": {"type": "number"}, "h": {"type": "number"}
                                    },
                                    "required": ["x","y","w","h"]
                                },
                                "semantic_label": {
                                    "type": ["string", "null"],
                                    "description": (
                                        "Canonical S3 slug. Badges: badge/food_network, "
                                        "badge/opentable_diners_choice, badge/youtube, badge/hulu, "
                                        "badge/tripadvisor, badge/yelp, badge/michelin, "
                                        "badge/zagat, badge/best_of. Ornaments: "
                                        "ornament/floral_swash_centered, ornament/floral_swash_left, "
                                        "ornament/calligraphic_rule, ornament/diamond_rule, "
                                        "ornament/vine_separator, ornament/scroll_divider. Use null "
                                        "only when the graphic is unique custom artwork that does "
                                        "not match any palette slug."
                                    )
                                }
                            },
                            "required": ["type","subtype","bbox","semantic_label"]
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
- Section headers in cursive/handwriting/decorative script must be captured as category_header — never skip them. Single-word cursive headers (Sharable, Starters, Entrées, Sides, etc.) are category_header even without other context.
- Capture every NON-text graphical element as type=image with subtype=badge|ornament|collage_box and a semantic_label from the S3 palette below:
   * Badges (circular brand/award icons): badge/food_network, badge/opentable_diners_choice, badge/youtube, badge/hulu, badge/tripadvisor, badge/yelp, badge/michelin, badge/zagat, badge/best_of
   * Ornaments (decorative flourishes/swashes): ornament/floral_swash_centered, ornament/floral_swash_left, ornament/calligraphic_rule, ornament/diamond_rule, ornament/vine_separator, ornament/scroll_divider
   * "As seen on" / "As featured in" multi-badge panels: subtype=collage_box
  ZERO TOLERANCE for null semantic_label on recognizable items — pick the closest palette match.
- Include ALL separator/divider/line/border/ornament elements
- Include ALL logo regions if present. Each distinct location's name/wordmark/emblem becomes a separate type=logo element in the elements array (primary restaurant name + any sub-logos like secondary locations, chain branches, or dual-branding). If unsure, include it.
- font_size: pixel_height * 0.75 (approximate pt)
- font_family: "decorative-script" for cursive/handwritten/calligraphy; "serif" for classic serif; "sans-serif" for modern clean; "display" for large decorative non-script headers
- text_align: center if element center is in middle 20% of canvas width, else left/right
- Price strings: strip $ prefix. Keep range format like 18/21. Use MP for market price. null if no price.
- column: 0 for leftmost column, 1 for middle column on 3-col menus or right column on 2-col menus, 2 for rightmost column on 3-col menus only. On 2-column menus only use 0 and 1.\
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

    # Use streaming so we can raise max_tokens above 16384 without hitting the
    # Anthropic SDK requirement error. Dense pages (As seen on collage_box) can
    # need ~24k tokens of tool_use args.
    try:
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=32768,
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
        ) as stream:
            response = stream.get_final_message()
    except anthropic.RateLimitError as e:
        print(f"[claude_tool] rate_limit: {e}")
        return None
    except anthropic.APIError as e:
        print(f"[claude_tool] api_error ({type(e).__name__}): {e}")
        return None

    if response.stop_reason == "max_tokens":
        print("[claude_tool] warning: truncated at max_tokens (consider raising further)")
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
    
    def _safe_int(v):
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    deduped = []
    for el in text_elements:
        bd = el.get("bbox") or {}
        content = str(el.get("content", ""))
        words = get_word_set(content)
        col = _safe_int(el.get("column", 0))
        # Normalise the source dict so downstream code sees int column too
        if not isinstance(el.get("column"), int):
            el["column"] = col

        duplicate_idx = None
        for i, ex_el in enumerate(deduped):
            ex_bd = ex_el.get("bbox") or {}
            ex_content = str(ex_el.get("content", ""))
            ex_words = get_word_set(ex_content)
            ex_col = _safe_int(ex_el.get("column", 0))
            if not isinstance(ex_el.get("column"), int):
                ex_el["column"] = ex_col
            
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
# Hints that suggest a primary restaurant logo (not a side-bar badge/award circle)
_PRIMARY_HINTS = _TOP_HINTS | {"bottom_center"}
# Hints that strongly suggest an award badge or side-bar graphic (not the main logo)
_BADGE_HINTS = {"middle_right", "middle_left", "center_right", "center_left"}


def _enforce_single_logo(elements: list) -> list:
    """
    R6-4: cluster logos by vertical region (top / middle / bottom) and merge
    fragments within the same region. This replaces the prior "single anchor +
    proximity threshold" approach which collapsed all logos within
    `anchor_size * 2.0` into one — losing distinct multi-region logos such as
    the AMI FFL menu's top-center brand, bottom-left "ON THE LAKE" Château,
    and bottom-center-right "ANNA MARIA" Château.

    Two logos belong to the same cluster if their vertical centers are within
    `min_separation = max(canvas_h_est * 0.20, 200 px)` of each other. For
    each cluster, the largest-area logo is the anchor and remaining fragments
    are merged into a union bbox (same as the legacy behaviour, scoped to
    that region). Up to 3 distinct logos per page survive — they don't get
    reclassified to image/badge.
    """
    logos = [(i, e) for i, e in enumerate(elements) if e.get("type") == "logo"]
    if len(logos) <= 1:
        return elements

    # Estimate canvas height from the spread of all elements (fallback 3400).
    all_y2 = [
        (e.get("bbox") or {}).get("y", 0) + (e.get("bbox") or {}).get("h", 0)
        for e in elements if e.get("bbox")
    ]
    canvas_h_est = max(all_y2, default=3400.0) or 3400.0
    # Same-band threshold: two logos belong to the same vertical band if their
    # y-centers are within `y_band` of each other.
    y_band = max(canvas_h_est * 0.15, 150.0)

    def _bd(item):
        return item[1].get("bbox") or {}
    def _cy(item):
        bd = _bd(item)
        return bd.get("y", 0) + bd.get("h", 0) / 2
    def _cx(item):
        bd = _bd(item)
        return bd.get("x", 0) + bd.get("w", 0) / 2
    def _size(item):
        bd = _bd(item)
        return max(bd.get("w", 100), bd.get("h", 100))

    # Sort logos by vertical center.
    logos_sorted = sorted(logos, key=_cy)

    # Greedy clustering: two logos cluster together if they share a y-band AND
    # are within `anchor_size * 1.2` of each other (so distinct multi-region
    # logos on the same horizontal line don't get collapsed into one bbox).
    clusters: list[list[tuple[int, dict]]] = []
    for item in logos_sorted:
        cy_i = _cy(item)
        cx_i = _cx(item)
        sz_i = _size(item)
        placed = False
        for cluster in clusters:
            same_band = any(abs(cy_i - _cy(it)) <= y_band for it in cluster)
            if not same_band:
                continue
            # Require horizontal/2D proximity to cluster members: merge only when
            # at least one member is within max(anchor_size, member_size) * 1.2.
            close_enough = any(
                ((cx_i - _cx(it)) ** 2 + (cy_i - _cy(it)) ** 2) ** 0.5
                <= max(sz_i, _size(it)) * 1.2
                for it in cluster
            )
            if close_enough:
                cluster.append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])

    # For each cluster, pick the largest-area logo as anchor and union the rest.
    merged_logos: list[dict] = []
    for cluster in clusters:
        if len(cluster) == 1:
            merged_logos.append(cluster[0][1])
            continue

        def _area(item):
            bd = item[1].get("bbox") or {}
            return bd.get("w", 0) * bd.get("h", 0)
        anchor_idx, anchor = max(cluster, key=_area)
        anchor_bd = anchor.get("bbox") or {}
        ux1 = anchor_bd.get("x", 0)
        uy1 = anchor_bd.get("y", 0)
        ux2 = ux1 + anchor_bd.get("w", 0)
        uy2 = uy1 + anchor_bd.get("h", 0)
        for _, frag in cluster:
            if frag is anchor:
                continue
            fb = frag.get("bbox") or {}
            ux1 = min(ux1, fb.get("x", 0))
            uy1 = min(uy1, fb.get("y", 0))
            ux2 = max(ux2, fb.get("x", 0) + fb.get("w", 0))
            uy2 = max(uy2, fb.get("y", 0) + fb.get("h", 0))

        # Topmost fragment provides the position_hint (most accurate for placement).
        topmost = min(cluster, key=_cy)[1]
        merged_logo = dict(anchor)
        merged_logo["bbox"] = {
            "x": ux1, "y": uy1, "w": ux2 - ux1, "h": uy2 - uy1,
        }
        merged_logo["position_hint"] = topmost.get("position_hint", anchor.get("position_hint", "top_center"))
        merged_logos.append(merged_logo)

    # Rebuild element list: drop ALL original logos, append merged_logos in original order.
    rebuilt = [e for e in elements if e.get("type") != "logo"]
    rebuilt.extend(merged_logos)
    return rebuilt


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
  and Graphical Candidate IDs [G1, G2, C1, C2].

═══ S3 ASSET PALETTE (Use for semantic_label) ═══
Use these canonical slugs to fetch clean PNGs from S3. Set semantic_label to one of these whenever you recognize the content:
- Badges: badge/youtube, badge/food_network, badge/opentable_diners_choice, badge/hulu, badge/tripadvisor, badge/yelp, badge/michelin, badge/zagat, badge/best_of
- Ornaments: ornament/floral_swash_centered, ornament/floral_swash_left, ornament/calligraphic_rule, ornament/diamond_rule, ornament/vine_separator, ornament/scroll_divider
- Separators: separator/wavy_line, separator/double_line, separator/diamond_rule, separator/dotted_ornament

═══ PROCESS ORDER (follow sequentially) ═══

STEP 1 — SKELETON SCAN (do before anything else):
Visually scan IMAGE 1 for ALL section/category headers and decorative dividers/separators.
Section headers are often set in a DIFFERENT typeface (cursive/script/calligraphy) than
the menu items below them. Examples: "Sharable", "Starters", "Entrées", "Broths & Greens",
"Sides", "Desserts", "Cocktails", "Wine List", "Beer", "Bar Menu", "Brunch", "Lunch",
"Dinner", "Kids Menu". These ARE category_header even when they are a single word.

STEP 2 — LABEL OCR BLOCKS:
For each numbered block [1, 2, 3] in IMAGE 2: assign subtype, column, font_family.
column: 0 for leftmost column, 1 for middle column on 3-col menus or right column on 2-col menus, 2 for rightmost column on 3-col menus only. On 2-column menus only use 0 and 1.
If a block is actually a fancy divider (like a dashed or wavy line), set subtype to "separator" and assign the correct semantic_label from the palette.

CATEGORY-HEADER RULES (apply aggressively — false negatives are worse than false positives):
  - Any short (1-3 word) block set in a noticeably different / cursive / script font → category_header
  - Any block followed below by a horizontal stack of items+prices → the block above is the category_header
  - Decorative-script words like "Sharable", "Starters", "Entrées", "Sides" → category_header even if no font hint
  - Single word in significantly larger font than surrounding text → category_header
  - The restaurant name (logo wordmark or biggest top-of-page text) is restaurant_name, NOT category_header

STEP 3 — LABEL GRAPHICAL CANDIDATES:
For each numbered box [G1, G2, C1, C2] in IMAGE 2: identify its type in graphic_labels.
  - Magenta boxes (G1, G2...) are generic candidates.
  - Cyan boxes (C1, C2...) are high-confidence template matches.
Assign subtype (badge, ornament, collage_box) and semantic_label from the S3 palette.
"As seen on" / "As featured in" panels MUST be labeled as collage_box.
Circular award/brand badges (Food Network, Diners' Choice, TripAdvisor, etc.) MUST be labeled as badge with the correct semantic_label.
YouTube/Yelp/Hulu icons MUST be labeled as badge with the correct semantic_label.

CRITICAL — NEVER LEAVE semantic_label NULL when:
  - the shape is clearly circular and contains brand text/icon → match to a badge/* slug
  - the shape is a flourish/swash under a header → match to ornament/floral_swash_centered or ornament/floral_swash_left
  - the shape is a calligraphic/scrolled rule → match to ornament/calligraphic_rule or ornament/scroll_divider
  - the shape is a wavy/double/dotted line → match to separator/wavy_line / separator/double_line / separator/dotted_ornament
Only return null when the element is a unique custom artwork (e.g. the restaurant's
own monogram/wordmark) that doesn't resemble any palette item. Default behaviour:
pick the closest palette match rather than null.

STEP 4 — DECORATIVE ELEMENTS (Missed by SoM):
For each section header or fancy divider from Step 1 that has NO numbered box in IMAGE 2:
Draw a bbox and assign subtype (category_header or separator) and semantic_label.

STEP 5 — LOGO BBOXES:
Identify ALL distinct restaurant logo/branding regions on the page. Each bbox covers ONE location's name, wordmark, or emblem:
  - PRIMARY: the main restaurant name (usually top-center, largest)
  - SUB-LOGOS: secondary location names, chain branches, dual-branding (e.g. "ON THE LAKE", "ANNA MARIA")
  - EXCLUDE: award badges, social icons, "As seen on" panels — those are graphic_elements
Return up to 3 logo_bboxes per page.

STEP 6 — GRAPHIC ELEMENTS (Non-text graphical regions):
Scan IMAGE 1 for any OTHER graphical elements (badges, circles, ornaments) NOT already labeled in graphic_labels.
ZERO TOLERANCE for missing social icons (YouTube, Yelp) or circular badges (Food Network, Diners' Choice, OpenTable).
Use semantic_label from the S3 palette for any recognized badge.
"""

_HYBRID_TOOL_SCHEMA = {
    "name": "label_menu_layout",
    # NOTE: strict=True requires additionalProperties:false + every property
    # in `required` on every nested object. This schema has many optional fields
    # so we rely on Pydantic validation downstream instead. See _TOOL_SCHEMA for
    # the strict-mode example.
    "description": "Extract menu layout with semantic labels and S3 asset matching.",
    "input_schema": {
        "type": "object",
        "properties": {
            "background_color": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"},
            "ocr_labels": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "subtype": {
                            "type": "string",
                            "enum": [
                                "restaurant_name", "category_header", "item_name",
                                "item_description", "item_price", "tagline",
                                "address", "phone", "other_text", "separator"
                            ],
                        },
                        "font_family": {
                            "type": "string",
                            "enum": ["sans-serif", "serif", "decorative-script", "display", "monospace"],
                        },
                        "corrected_text": {"type": ["string", "null"]},
                        "column": {"type": "integer", "enum": [0, 1, 2]},
                        "semantic_label": {"type": ["string", "null"]},
                    },
                    "required": ["id", "subtype"],
                },
            },
            "decorative_elements": {
                "type": "array",
                "description": "Missed headers or separators.",
                "items": {
                    "type": "object",
                    "properties": {
                        "subtype": {"type": "string", "enum": ["category_header", "separator"]},
                        "content": {"type": "string"},
                        "semantic_label": {"type": ["string", "null"]},
                        "column": {"type": "integer", "enum": [0, 1, 2]},
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
            "logo_bboxes": {
                "type": ["array", "null"],
                "description": (
                    "All distinct logo regions on the page (primary + sub-logos). "
                    "Each bbox covers ONE branding element — the main restaurant name, "
                    "secondary locations, chain branches, or dual-brand wordmarks. Do NOT "
                    "include award badges, brand circles (Food Network, OpenTable), or "
                    "'As seen on' panels here — those go in graphic_labels or graphic_elements. "
                    "Return up to 3 entries per page (max 5)."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "number"}, "y": {"type": "number"},
                        "w": {"type": "number"}, "h": {"type": "number"},
                    },
                    "required": ["x", "y", "w", "h"],
                },
            },
            "graphic_labels": {
                "type": "array",
                "description": "Label every G# and C# candidate box. Use the EXACT id string shown in IMAGE 2 (e.g. 'G1', 'C3'). Set semantic_label from the S3 palette for any recognized badge/brand icon.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Exact label shown on box in IMAGE 2, e.g. 'G1', 'C2'"},
                        "subtype": {"type": "string", "enum": ["ornament", "badge", "collage_box"]},
                        "semantic_label": {
                            "type": ["string", "null"],
                            "description": "S3 slug if recognized (e.g. 'badge/food_network'), else null",
                        },
                    },
                    "required": ["id", "subtype", "semantic_label"],
                },
            },
            "graphic_elements": {
                "type": "array",
                "description": "Additional graphical regions visible in IMAGE 1 that have NO G#/C# box. Use for badges, circles, ornament panels not already in graphic_labels.",
                "items": {
                    "type": "object",
                    "properties": {
                        "subtype": {"type": "string", "enum": ["ornament", "badge", "collage_box"]},
                        "semantic_label": {
                            "type": ["string", "null"],
                            "description": "S3 slug if recognized (e.g. 'badge/food_network'), else null",
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
                    "required": ["subtype", "semantic_label", "bbox"],
                },
            },
            "menu_data": {
                "type": "object",
                "properties": {
                    "restaurant_name": {"type": "string"},
                    "categories": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "column": {"type": "integer"},
                                "items": {"type": "array", "items": {"type": "object"}},
                            },
                        },
                    },
                },
                "required": ["restaurant_name", "categories"],
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
    # Batch sizes — keep small on CPU recognition (large batches don't help on CPU
    # and dramatically increase memory pressure).
    os.environ.setdefault("RECOGNITION_BATCH_SIZE", "8")
    os.environ.setdefault("DETECTOR_BATCH_SIZE", "1")

    try:
        # Surya device strategy:
        # - Detection on MPS = fast and stable.
        # - Recognition on MPS = pathological slowdowns (3000s/iter) on dense
        #   pages because of a torch-MPS memory-thrashing bug. Force recognition
        #   to CPU. Slower per page but deterministic, and never hangs.
        # Override with MENU_SURYA_REC_DEVICE=mps if a future torch fixes it.
        try:
            import torch as _torch
            if _torch.backends.mps.is_available():
                os.environ.setdefault("TORCH_DEVICE", "mps")
                os.environ.setdefault("DETECTOR_DEVICE", "mps")
                os.environ.setdefault("RECOGNITION_DEVICE",
                                      os.environ.get("MENU_SURYA_REC_DEVICE", "cpu"))
                print(f"[surya] device: detection=mps recognition={os.environ['RECOGNITION_DEVICE']}")
        except Exception:
            pass

        det_device = os.environ.get("DETECTOR_DEVICE", "cpu")
        rec_device = os.environ.get("RECOGNITION_DEVICE", "cpu")

        # Try new API first (surya >= 0.17)
        try:
            from surya.foundation import FoundationPredictor
            from surya.recognition import RecognitionPredictor
            from surya.detection import DetectionPredictor

            print(f"[surya] loading new models — det={det_device}, rec={rec_device}...")
            # FoundationPredictor drives the recognition model — pin it to rec_device.
            _surya_foundation_predictor = FoundationPredictor(device=rec_device)
            _surya_rec_predictor = RecognitionPredictor(_surya_foundation_predictor)
            _surya_det_predictor = DetectionPredictor(device=det_device)
            _surya_api_version = "new"
            print(f"[surya] models ready (API v0.17+)")
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

        print(f"[surya] loading old models — det={det_device}, rec={rec_device}...")
        _surya_det_model = _det_model()
        if det_device == "mps":
            _surya_det_model = _surya_det_model.to("mps")
        _surya_det_processor = _det_proc()

        _surya_rec_model = _rec_model()
        if rec_device == "mps":
            _surya_rec_model = _surya_rec_model.to("mps")
        # Note: rec_device == "cpu" leaves the model on CPU (default) —
        # avoids the MPS memory-thrashing hang.

        _surya_rec_processor = _rec_proc()
        _surya_api_version = "old"
        print(f"[surya] models ready (API v0.4.x)")
        return True
    except Exception as exc:
        print(f"[surya] model load failed: {exc}")
        return False


# Hard wall-clock cap on a single Surya OCR call. Prevents the 16-hour MPS hang
# we saw on page 2 of AMI FFL DINNER from ever repeating. Override per-env if you
# have a really dense page you want to give Surya more budget on.
_SURYA_TIMEOUT_SEC = int(os.environ.get("MENU_SURYA_TIMEOUT_SEC", "360"))


def _run_with_timeout(fn, timeout_sec: int, *args, **kwargs):
    """Run fn in a worker thread; raise TimeoutError if it exceeds timeout."""
    import threading
    box: dict = {}

    def _target():
        try:
            box["result"] = fn(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout_sec)
    if t.is_alive():
        # We can't kill a python thread cleanly. Daemon=True ensures it dies on
        # process exit. The caller's fallback path runs immediately on raise.
        raise TimeoutError(f"surya inference exceeded {timeout_sec}s")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def extract_blocks_surya(img: Image.Image) -> list:
    """
    Run Surya OCR on img, return line-level blocks as:
      [{"text": str, "bbox": [x1, y1, x2, y2], "confidence": float}, ...]
    Returns empty list if Surya is not installed or inference fails.
    """
    if not _load_surya_models():
        return []

    def _infer():
        if _surya_api_version == "new":
            return _surya_rec_predictor([img], det_predictor=_surya_det_predictor)
        from surya.ocr import run_ocr
        return run_ocr(
            [img], [["en"]],
            _surya_det_model, _surya_det_processor,
            _surya_rec_model, _surya_rec_processor,
        )

    try:
        results = _run_with_timeout(_infer, _SURYA_TIMEOUT_SEC)
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
    except TimeoutError as exc:
        # Caller falls back to Claude Vision; we still keep Surya for the next page.
        print(f"[surya] {exc} — falling back for this page")
        return []
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


def _detect_templates(gray_img: np.ndarray) -> list[dict]:
    """
    Scans the grayscale image for loaded logo templates.
    Uses multi-scale template matching to handle varying logo sizes.
    """
    if not _CV2_AVAILABLE or not _LOGO_TEMPLATES:
        return []

    candidates = []
    # Multi-scale matching: broader range to catch tiny icons and large badges.
    # From 0.2x to 2.0x of template size.
    scales = [0.2, 0.3, 0.4, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
    
    for name, tpl in _LOGO_TEMPLATES.items():
        th, tw = tpl.shape
        for scale in scales:
            rw, rh = int(tw * scale), int(th * scale)
            if rw < 15 or rh < 15: continue
            if rw > gray_img.shape[1] or rh > gray_img.shape[0]: continue
            
            resized_tpl = cv2.resize(tpl, (rw, rh), interpolation=cv2.INTER_AREA)
            res = cv2.matchTemplate(gray_img, resized_tpl, cv2.TM_CCOEFF_NORMED)
            threshold = 0.72 # Relaxed for robustness against JPEG noise
            loc = np.where(res >= threshold)
            
            for pt in zip(*loc[::-1]): # x, y
                score = res[pt[1], pt[0]]
                candidates.append({
                    "bbox": (int(pt[0]), int(pt[1]), int(pt[0] + rw), int(pt[1] + rh)),
                    "score": float(score),
                    "type": "candidate_template",
                    "label": f"badge/{name}"
                })
    
    if not candidates:
        return []
    
    # NMS for templates
    candidates.sort(key=lambda x: x["score"], reverse=True)
    final_candidates = []
    for cand in candidates:
        overlap = False
        for f_cand in final_candidates:
            if cand["label"] != f_cand["label"]: continue
            # IoU check
            a, b = cand["bbox"], f_cand["bbox"]
            ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
            ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
            iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
            inter = iw * ih
            area_a = (a[2]-a[0]) * (a[3]-a[1])
            area_b = (b[2]-b[0]) * (b[3]-b[1])
            iou = inter / float(area_a + area_b - inter)
            if iou > 0.3:
                overlap = True
                break
        if not overlap:
            final_candidates.append(cand)
            
    return final_candidates


def detect_graphical_candidates(img: Image.Image) -> list[dict]:
    """
    Advanced OpenCV pre-pass for circular badges and bordered boxes.
    Uses multi-scale template matching + HoughCircles + Contours.
    """
    if not _CV2_AVAILABLE:
        return []

    # 1. Template Match (Cyan)
    candidates = match_badges(img)
    for c in candidates:
        c["type"] = "candidate_template"
        c["label"] = c.get("semantic_label", "template")
        # SoM expects list [x1, y1, x2, y2]
        b = c["bbox"]
        c["bbox"] = [b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]]

    # 2. Graphic Blobs (Magenta)
    from separator import detect_graphic_blobs
    blobs = detect_graphic_blobs(img)
    for b in blobs:
        bbox = b["bbox"]
        candidates.append({
            "bbox": [bbox["x"], bbox["y"], bbox["x"] + bbox["w"], bbox["y"] + bbox["h"]],
            "type": "candidate_box" if bbox["w"] > 100 and bbox["h"] > 100 else "candidate_badge"
        })

    # --- Non-Max Suppression (Cleanup Overlaps) ---
    if not candidates:
        return []
        
    # Sort by area descending
    candidates.sort(key=lambda x: (x["bbox"][2]-x["bbox"][0]) * (x["bbox"][3]-x["bbox"][1]), reverse=True)
    final = []
    for cand in candidates:
        # Safety: validate bbox values are valid before IoU
        c_x1, c_y1, c_x2, c_y2 = cand["bbox"]
        if c_x2 <= c_x1 or c_y2 <= c_y1: continue

        is_covered = False
        cb = {"x": c_x1, "y": c_y1, "w": c_x2 - c_x1, "h": c_y2 - c_y1}
        for f in final:
            f_x1, f_y1, f_x2, f_y2 = f["bbox"]
            fb = {"x": f_x1, "y": f_y1, "w": f_x2 - f_x1, "h": f_y2 - f_y1}
            
            iou = _bbox_iou(cb, fb)
            if iou > 0.5:
                # Always prefer template matches
                if f.get("type") == "candidate_template":
                    is_covered = True
                    break
                if cand.get("type") == "candidate_template":
                    continue # Will likely replace or just let through if different
                else:
                    is_covered = True
                    break
        if not is_covered:
            final.append(cand)
            
    # Hard limit: keep top 25 most confident/largest candidates to avoid overloading Claude
    return final[:25]


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
        
    # 2. Draw Graphic Candidates (Magenta for unknown, Cyan for template matches)
    if graphic_candidates:
        magenta = (255, 0, 255) # Magenta for generic candidates
        cyan = (0, 255, 255)    # Cyan for template-matched graphics
        
        for i, cand in enumerate(graphic_candidates):
            x1, y1, x2, y2 = cand["bbox"]
            is_template = cand.get("type") == "candidate_template"
            color = cyan if is_template else magenta
            
            # Outline only for graphics, no fill to avoid obscuring details
            draw.rectangle([x1, y1, x2, y2], outline=(*color, 255), width=4)
            
            lbl = f"C{i + 1}" if is_template else f"G{i + 1}"
            lbl_w, lbl_h = len(lbl) * 12 + 8, 22
            lbl_x = x1
            lbl_y = max(0, y1 - lbl_h - 2)
            draw.rectangle([lbl_x, lbl_y, lbl_x + lbl_w, lbl_y + lbl_h], fill=(*color, 255))
            draw.text((lbl_x + 4, lbl_y + 3), lbl, fill=(255, 255, 255, 255))

    result = Image.alpha_composite(base, overlay)
    return result.convert("RGB")


def _generate_tiles(w: int, h: int, tile_size: int = 1500, overlap: int = 250) -> list[tuple[int, int, int, int]]:
    """Generates bounding boxes for overlapping high-resolution tiles."""
    tiles = []
    y = 0
    while y < h:
        x = 0
        y_end = min(y + tile_size, h)
        y_start = max(0, y_end - tile_size)
        while x < w:
            x_end = min(x + tile_size, w)
            x_start = max(0, x_end - tile_size)
            
            tiles.append((x_start, y_start, x_end, y_end))
            if x_end == w: break
            x += (tile_size - overlap)
        if y_end == h: break
        y += (tile_size - overlap)
    return tiles


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

    # --- Dynamic Slicing (SAHI-lite) ---
    # Divide high-resolution image into overlapping tiles to ensure small logos are visible.
    quadrants = []
    rects = _generate_tiles(orig_w, orig_h)
    
    # Safety limit: never send more than 18 tiles (Claude limit is 20)
    if len(rects) > 18:
        # If too many tiles, fallback to larger tiles to reduce count
        rects = _generate_tiles(orig_w, orig_h, tile_size=orig_w//2 + 200, overlap=200)

    for i, (x1, y1, x2, y2) in enumerate(rects):
        q_img = img.crop((x1, y1, x2, y2))
        q_buf = io.BytesIO()
        q_img.convert("RGB").save(q_buf, format="JPEG", quality=88)
        quadrants.append({
            "id": f"Tile {i+1}",
            "b64": base64.standard_b64encode(q_buf.getvalue()).decode(),
            "bbox": (x1, y1, x2, y2)
        })

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
    
    # Add graphical candidate list with shape hints so Claude knows what type of element to expect
    _SHAPE_HINT = {
        "candidate_badge":    "CIRCLE/OVAL shape → must be badge or null, NEVER ornament",
        "candidate_box":      "RECTANGLE/BOX shape → collage_box or null",
        "candidate_template": "TEMPLATE MATCH",
    }
    g_lines = []
    for i, g in enumerate(graphic_candidates):
        x1, y1, x2, y2 = g["bbox"]
        cx1, cy1 = x1 / scale_x, y1 / scale_y
        cw, ch = (x2 - x1) / scale_x, (y2 - y1) / scale_y
        g_type = g["type"]
        if g_type == "candidate_template":
             g_lines.append(f"[C{i + 1}] (Cyan) TEMPLATE MATCH: {g['label']} — x={cx1:.0f} y={cy1:.0f} w={cw:.0f} h={ch:.0f}")
        else:
             shape_hint = _SHAPE_HINT.get(g_type, g_type)
             g_lines.append(f"[G{i + 1}] (Magenta) {shape_hint} — x={cx1:.0f} y={cy1:.0f} w={cw:.0f} h={ch:.0f}")
    graphic_list = "\n".join(g_lines)

    sw, sh = send_img.size
    user_msg = (
        f"Both images are {sw}×{sh}px. All bbox values must be in this pixel space.\n\n"
        f"I am providing {len(quadrants)} high-resolution overlapping tiles to ensure small logos are visible.\n\n"
        "Follow the 6-step process from the system prompt exactly.\n\n"
        "═══ STEP 1 — SKELETON SCAN ═══\n"
        "Scan the High-Resolution Tiles now. Identify every section/category header.\n\n"
        "═══ STEP 2 — OCR BLOCKS ═══\n"
        f"Surya OCR extracted {len(surya_blocks)} blocks:\n"
        f"{block_list}\n\n"
        "═══ STEP 3 — GRAPHICAL CANDIDATES ═══\n"
        "Pre-pass detected potential graphical regions (Magenta G# and Cyan C# boxes):\n"
        "RULES:\n"
        "- CIRCLE/OVAL shapes → subtype MUST be 'badge', NEVER 'ornament'. Set semantic_label from palette if you recognize the brand, else null.\n"
        "- RECTANGLE/BOX shapes → subtype 'collage_box' ONLY if it literally contains multiple logos. Otherwise null.\n"
        "- TEMPLATE MATCH (Cyan) → use the matched label as semantic_label.\n"
        "- If you do NOT recognize what is shown at a G# location: set semantic_label to null.\n"
        "- Do NOT assign ornament/floral_swash to shapes that are circles or large boxes.\n"
        f"{graphic_list or '(none)'}\n"
        "Identify these in the graphic_labels tool field.\n\n"
        "═══ STEP 4 — DECORATIVE ELEMENTS ═══\n"
        "SECTION HEADERS: For every cursive/script header from Step 1 with no numbered OCR block.\n\n"
        "═══ STEP 5 — LOGO BBOX ═══\n"
        "Draw ONE bbox tightly around the PRIMARY restaurant branding in IMAGE 1.\n\n"
        "═══ STEP 6 — GRAPHIC ELEMENTS ═══\n"
        "Scan the Tiles for ANY OTHER non-text graphical regions not already labeled.\n"
        "CRITICAL — use exact semantic_label slugs:\n"
        "  Food Network → badge/food_network\n"
        "  OpenTable / Diners' Choice → badge/opentable_diners_choice\n"
        "  YouTube → badge/youtube   Hulu → badge/hulu\n"
        "  TripAdvisor → badge/tripadvisor   Yelp → badge/yelp\n"
    )

    # Encode annotated image (IMAGE 2 — for OCR block spatial reference)
    buf = io.BytesIO()
    send_img.convert("RGB").save(buf, format="JPEG", quality=85)
    annotated_b64 = base64.standard_b64encode(buf.getvalue()).decode()

    # Encode clean image (IMAGE 1 — for accurate text reading and decorative element location)
    clean_buf = io.BytesIO()
    clean_send.convert("RGB").save(clean_buf, format="JPEG", quality=85)
    clean_b64 = base64.standard_b64encode(clean_buf.getvalue()).decode()

    # Build the multi-image message for Claude
    content_blocks = [
        {"type": "text", "text": f"I am providing {len(quadrants)} high-resolution overlapping tiles of the menu to ensure small logos are visible."},
    ]
    
    for q in quadrants:
        content_blocks.append({"type": "text", "text": f"Tile {q['id']}:"})
        content_blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": q["b64"]}})

    content_blocks.extend([
        {"type": "text", "text": "IMAGE 1 — Clean original (full view):"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": clean_b64}},
        {"type": "text", "text": "IMAGE 2 — Annotated (spatial reference):"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": annotated_b64}},
        {"type": "text", "text": user_msg}
    ])

    try:
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=32768,
            system=_HYBRID_SYSTEM_PROMPT,
            tools=[_HYBRID_TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": "label_menu_layout"},
            messages=[{
                "role": "user",
                "content": content_blocks,
            }],
        ) as stream:
            response = stream.get_final_message()

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
        subtype = lbl.get("subtype", "other_text")
        el_type = "separator" if subtype == "separator" else "text"
        
        el_dict = {
            "type": el_type,
            "subtype": subtype,
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
        }
        if lbl.get("semantic_label"):
            el_dict["semantic_label"] = lbl.get("semantic_label")
        elements.append(el_dict)

    # Decorative elements use Claude's approximate bboxes — fine for section headers
    for dec in data.get("decorative_elements", []):
        bd = dec.get("bbox") or {}
        if scale_x != 1.0 or scale_y != 1.0:
            bd = {
                "x": bd.get("x", 0) * scale_x, "y": bd.get("y", 0) * scale_y,
                "w": bd.get("w", 0) * scale_x,  "h": bd.get("h", 0) * scale_y,
            }
        elem_h = max(1.0, float(bd.get("h", 30)))
        subtype = dec.get("subtype", "category_header")
        el_type = "separator" if subtype == "separator" else "text"
        
        el_dict = {
            "type": el_type,
            "subtype": subtype,
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
        }
        if dec.get("semantic_label"):
            el_dict["semantic_label"] = dec.get("semantic_label")
        elements.append(el_dict)

    # R7-A: Multi-logo support — iterate every entry in logo_bboxes (plural).
    # Backward-compat: if Claude returns the old singular `logo_bbox`, wrap it
    # as a 1-item list before parsing.
    logo_bboxes = data.get("logo_bboxes") or []
    if not logo_bboxes:
        single = data.get("logo_bbox")
        if isinstance(single, dict) and single.get("w", 0) > 0 and single.get("h", 0) > 0:
            logo_bboxes = [single]
    for idx, lb in enumerate(logo_bboxes[:5]):
        if not isinstance(lb, dict) or lb.get("w", 0) <= 0 or lb.get("h", 0) <= 0:
            continue
        if scale_x != 1.0 or scale_y != 1.0:
            lb = {
                "x": lb.get("x", 0) * scale_x, "y": lb.get("y", 0) * scale_y,
                "w": lb.get("w", 0) * scale_x, "h": lb.get("h", 0) * scale_y,
            }
        _lb_cx = lb.get("x", 0) + lb.get("w", 0) / 2
        _lb_y = lb.get("y", 0)
        _py = "top" if _lb_y < orig_h * 0.4 else ("middle" if _lb_y < orig_h * 0.7 else "bottom")
        _px = "left" if _lb_cx < orig_w * 0.35 else ("right" if _lb_cx > orig_w * 0.65 else "center")
        elements.append({
            "type": "logo",
            "bbox": lb,
            "position_hint": f"{_py}_{_px}",
            "logo_index": idx,
        })

    # Process graphic_labels (G# and C# mappings)
    graphic_label_map = {lbl.get("id"): lbl for lbl in data.get("graphic_labels", []) if lbl.get("id")}
    for i, g in enumerate(graphic_candidates):
        is_template = g.get("type") == "candidate_template"
        is_circle = g.get("type") == "candidate_badge"  # HoughCircles detection = always round
        prefix = "C" if is_template else "G"
        lbl = graphic_label_map.get(f"{prefix}{i+1}")
        if lbl:
            sl = lbl.get("semantic_label") or (g.get("label") if is_template else None)
            # Circle shapes are never ornamental swashes — strip that label to force pixel crop.
            # Ornaments are calligraphic curves; circles are always badge/award icons.
            if is_circle and sl and sl.startswith("ornament/"):
                sl = None
            # Don't label circles as collage_box either — only rectangular bordered panels are.
            subtype = lbl.get("subtype", "badge" if is_circle else "ornament")
            if is_circle and subtype == "collage_box":
                subtype = "badge"
            x1, y1, x2, y2 = g["bbox"]
            elements.append({
                "type": "image",
                "subtype": subtype,
                "semantic_label": sl,
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
          f"labels={len(data.get('graphic_labels', []))}, graphics={len(data.get('graphic_elements', []))})")
    
    # --- Post-processing (The 'Precision Engine' cleanup) ---
    # 1. Deduplicate text (Word-match merge Surya vs Claude's hallucinated decorative copies)
    elements = _dedup_text_elements(elements)

    # 2. Snap decorative headers to content below and center in column
    elements = _snap_decorative_headers(elements, orig_w=orig_w)

    # 2b. (Fix 1) Snap ornament/separator graphic decorators into content gaps,
    #     or drop them if no plausible gap exists. Mirrors the y-snapping that
    #     `_snap_decorative_headers` does for type=="text", but for graphics.
    elements = _snap_graphic_decorators(elements, orig_w=orig_w)

    # 3. Enforce single primary logo — reclassify distant graphic logos as image/badge so they
    #    get individually pixel-cropped instead of all sharing the restaurant logo image_data.
    elements = _enforce_single_logo(elements)

    # 4. Mask any text/separators inside the (now single) logo area
    elements = _mask_logo_elements(elements)

    # 5. Verification pass — second Claude call with overlay image to catch missed headers/badges
    elements = _verification_pass(img, elements, orig_w, orig_h)

    # 6. Re-snap after any newly added elements from verification pass
    elements = _snap_decorative_headers(elements, orig_w=orig_w)

    # 7. Re-enforce single logo after verification (verification may add graphic elements)
    elements = _enforce_single_logo(elements)

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

    # Determine effective canvas area from the spread of element bboxes — used to
    # reject logos that have grown to cover most of the page (e.g., after a
    # pixel-refinement runaway). A logo bbox > 35% of canvas area is almost
    # certainly a false positive and we should NOT mask anything against it.
    all_x2 = [
        (e.get("bbox") or {}).get("x", 0) + (e.get("bbox") or {}).get("w", 0)
        for e in elements if e.get("bbox")
    ]
    all_y2 = [
        (e.get("bbox") or {}).get("y", 0) + (e.get("bbox") or {}).get("h", 0)
        for e in elements if e.get("bbox")
    ]
    canvas_area_est = max(max(all_x2, default=1.0), 1.0) * max(max(all_y2, default=1.0), 1.0)
    safe_logos = []
    for logo in logos:
        lbd = logo.get("bbox") or {}
        area = float(lbd.get("w", 0)) * float(lbd.get("h", 0))
        if area > canvas_area_est * 0.35:
            print(f"[mask_logo] skip oversized logo bbox: {lbd.get('w', 0):.0f}×{lbd.get('h', 0):.0f} "
                  f"({100 * area / canvas_area_est:.1f}% of canvas)")
            continue
        safe_logos.append(logo)
    logos = safe_logos
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

    # Sanity: the refined bbox must not have grown beyond a sane multiple of the
    # rough bbox. If it did (rough was a small logo and we found ink across the
    # whole zone — usually a sign the threshold caught body text), reject.
    if abs_w > rw * 3.0 or abs_h > rh * 3.0:
        print(f"[logo_pixel] reject runaway expansion: rough {rw:.0f}×{rh:.0f} "
              f"→ refined {abs_w:.0f}×{abs_h:.0f}")
        return None
    # Sanity: refined must not cover more than 25% of the canvas (logos rarely do)
    if (abs_w * abs_h) > (canvas_w * canvas_h) * 0.25:
        print(f"[logo_pixel] reject oversized logo: {abs_w:.0f}×{abs_h:.0f} "
              f"({100 * abs_w * abs_h / (canvas_w * canvas_h):.1f}% of canvas)")
        return None

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
    Second Claude call: identifies elements VISIBLE in IMAGE 1 but NOT covered by boxes in IMAGE 2.
    Supports text, images (badges/logos), and separators (dividers).
    """
    client = _get_client()
    if client is None:
        return elements

    overlay_img = _render_extraction_overlay(img, elements)
    max_dim = 1400
    scale = min(1.0, max_dim / max(img.width, img.height))
    send_w, send_h = max(1, int(img.width * scale)), max(1, int(img.height * scale))
    clean_resized = img.resize((send_w, send_h), Image.LANCZOS)
    overlay_resized = overlay_img.resize((send_w, send_h), Image.LANCZOS)

    def _enc(pil_img: Image.Image) -> str:
        buf = io.BytesIO()
        pil_img.convert("RGB").save(buf, format="JPEG", quality=88)
        return base64.standard_b64encode(buf.getvalue()).decode()

    clean_b64 = _enc(clean_resized)
    overlay_b64 = _enc(overlay_resized)

    verify_prompt = (
        f"These images are {send_w}×{send_h}px.\n"
        "IMAGE 1: Clean original.\n"
        "IMAGE 2: Already-extracted elements (green=header, blue=text, red=logo, orange=separator).\n\n"
        "═══ S3 ASSET PALETTE ═══\n"
        "Badges: badge/youtube, badge/food_network, badge/opentable_diners_choice, badge/hulu, badge/tripadvisor, badge/yelp, badge/michelin, badge/zagat, badge/best_of\n"
        "Ornaments/Separators: ornament/floral_swash_centered, separator/wavy_line, separator/double_line, separator/diamond_rule\n\n"
        "TASK: Identify elements in IMAGE 1 with NO box in IMAGE 2.\n"
        "1. Missing Text: subtype='category_header' or 'item_name' etc.\n"
        "2. Missing Images: subtype='badge' or 'collage_box'. Use semantic_label from palette if recognized (e.g. badge/youtube).\n"
        "3. Missing Separators: subtype='separator'. Use semantic_label from palette (e.g. separator/wavy_line).\n\n"
        "Return ONLY a JSON array of objects:\n"
        "[{\"type\": \"text|image|separator\", \"subtype\": \"...\", \"content\": \"...\", \"semantic_label\": \"...|null\", \"bbox\": {\"x\":#, \"y\":#, \"w\":#, \"h\":#}}]\n"
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "IMAGE 1 — Clean:"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": clean_b64}},
                    {"type": "text", "text": "IMAGE 2 — Overlay:"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": overlay_b64}},
                    {"type": "text", "text": verify_prompt},
                ],
            }],
        )
        raw = response.content[0].text or "[]"
        import re as _re
        match = _re.search(r"\[.*\]", raw, _re.DOTALL)
        if not match: return elements
        missing = json.loads(match.group())
        
        inv_scale = 1.0 / scale
        for m in missing:
            bd = m.get("bbox") or {}
            abs_bd = {k: v * inv_scale for k, v in bd.items()}
            m_type = m.get("type", "text")
            
            if m_type == "text":
                elem_h = max(1.0, abs_bd.get("h", 30))
                elements.append({
                    "type": "text",
                    "subtype": m.get("subtype", "category_header"),
                    "content": m.get("content", ""),
                    "bbox": abs_bd,
                    "style": {
                        "font_size": round(elem_h * 0.75, 1),
                        "font_weight": "normal", "font_style": "italic",
                        "font_family": "decorative-script", "color": "#1a1a1a", "text_align": "center"
                    },
                    "column": int(m.get("column", 0)),
                })
            else: # image, logo, or separator — treat "logo" as "image" (badge)
                elements.append({
                    "type": "image" if m_type in ("image", "logo") else "separator",
                    "subtype": m.get("subtype", "badge" if m_type == "logo" else "ornament"),
                    "semantic_label": m.get("semantic_label"),
                    "bbox": abs_bd,
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


def _snap_graphic_decorators(elements: list, orig_w: float = 1200) -> list:
    """
    Fix 1 — snap (or drop) ornament/separator GRAPHIC elements so they land in
    the largest vertical gap between content blocks near their detected y.

    `_snap_decorative_headers` only fixes ``type=="text"`` cursive headers; this
    function does the equivalent for ``type=="image"`` ornaments and
    ``type=="separator"`` decorative dividers whose semantic_label is in the
    ornament/* or separator/* palette (or whose subtype is ornament/
    decorative_divider).

    Behaviour:
      - If the element already sits inside the LARGEST nearby content gap, leave
        it alone.
      - Otherwise, move it so its vertical center sits in the middle of that gap.
      - If no nearby gap is large enough to hold it (gap < element_h + 12 px),
        DROP the element — but only when it has no semantic_label, or its
        semantic_label is in the ornament/* palette (i.e. it came from Claude's
        approximate scan). Never drop a `separator` element with subtype
        horizontal_line / vertical_line and no semantic_label — those are real
        PyMuPDF vector lines.
    """
    if not elements:
        return elements

    # Sorted, deduped list of non-decorative-text content block centers.
    content_blocks = sorted(
        (
            e for e in elements
            if e.get("type") == "text"
            and e.get("style", {}).get("font_family") not in ("decorative-script", "display")
            and e.get("subtype") != "category_header"
        ),
        key=lambda b: float(b.get("bbox", {}).get("y", 0)),
    )

    def _is_target(el: dict) -> bool:
        t = el.get("type")
        if t not in ("image", "separator"):
            return False
        sl = el.get("semantic_label") or ""
        sub = el.get("subtype") or ""
        if sl.startswith(("ornament/", "separator/")):
            return True
        if sub in ("ornament", "decorative_divider"):
            return True
        return False

    def _droppable(el: dict) -> bool:
        # Only drop graphics that came from Claude's approximate decorator scan.
        # PyMuPDF vector separators (subtype=horizontal_line/vertical_line, no
        # semantic_label) are spatially correct — never drop them.
        sl = el.get("semantic_label") or ""
        sub = el.get("subtype") or ""
        if el.get("type") == "separator" and sub in ("horizontal_line", "vertical_line") and not sl:
            return False
        if sl.startswith("ornament/"):
            return True
        if not sl:
            return True  # unlabeled graphic — must justify its position
        return False

    result: list = []
    for el in elements:
        if not _is_target(el):
            result.append(el)
            continue

        bd = el.get("bbox") or {}
        el_y = float(bd.get("y", 0))
        el_h = float(bd.get("h", 0)) or 1.0
        el_cy = el_y + el_h / 2

        # y-band around the element to consider for gap-detection.
        band = max(150.0, el_h * 2.5)
        band = min(band, 200.0)  # spec says ±200 ceiling

        # Collect content centers within the band (sorted by y).
        nearby = []
        for b in content_blocks:
            bb = b.get("bbox") or {}
            by = float(bb.get("y", 0))
            bh = float(bb.get("h", 0))
            b_top = by
            b_bot = by + bh
            # consider any block that overlaps the band
            if b_bot >= el_cy - band and b_top <= el_cy + band:
                nearby.append((b_top, b_bot))

        nearby.sort()

        # Build gaps between consecutive nearby content blocks (real inter-content gaps).
        real_gaps = []  # list of (gap_top, gap_bot, gap_h)
        for i in range(len(nearby) - 1):
            gap_top = nearby[i][1]
            gap_bot = nearby[i + 1][0]
            if gap_bot > gap_top:
                real_gaps.append((gap_top, gap_bot, gap_bot - gap_top))
        # Open space above/below the cluster — fallback only (Defect B fix).
        fallback_gaps = []
        if nearby:
            top_block_y = nearby[0][0]
            bot_block_y = nearby[-1][1]
            open_top = (max(0.0, el_cy - band), top_block_y, top_block_y - max(0.0, el_cy - band))
            open_bot = (bot_block_y, el_cy + band, el_cy + band - bot_block_y)
            if open_top[2] > 0:
                fallback_gaps.append(open_top)
            if open_bot[2] > 0:
                fallback_gaps.append(open_bot)

        need = el_h + 12.0
        # Real gaps get first dibs; fall back to open margins only if no real gap fits.
        real_usable = [g for g in real_gaps if g[2] >= need]
        gaps = real_gaps if real_usable else (real_gaps + fallback_gaps)

        if not gaps:
            # No content blocks within the band — leave alone (likely a page margin).
            result.append(el)
            continue

        # Largest nearby gap.
        largest = max(gaps, key=lambda g: g[2])
        gap_top, gap_bot, gap_h = largest

        if gap_h < need:
            # No suitable home nearby — drop if it's a Claude-injected guess,
            # otherwise leave the element where it is.
            if _droppable(el):
                print(f"[snap_graphic] drop {el.get('semantic_label') or el.get('subtype','?')} "
                      f"@ y={el_cy:.0f} (no gap, max nearby={gap_h:.0f}px, need {need:.0f}px)")
                continue
            result.append(el)
            continue

        # Defect A fix — already inside ANY usable gap? leave it.
        already_in_a_usable_gap = any(
            g_top <= el_cy <= g_bot and g_h >= need
            for (g_top, g_bot, g_h) in gaps
        )
        if already_in_a_usable_gap:
            result.append(el)
            continue

        # Move so element center sits at the gap midpoint.
        new_cy = (gap_top + gap_bot) / 2.0
        new_y = new_cy - el_h / 2.0
        moved = dict(el)
        moved["bbox"] = dict(bd)
        moved["bbox"]["y"] = float(new_y)
        print(f"[snap_graphic] move {el.get('semantic_label') or el.get('subtype','?')} "
              f"y={el_cy:.0f}→{new_cy:.0f} (gap {gap_top:.0f}–{gap_bot:.0f}, h={gap_h:.0f}px)")
        result.append(moved)

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

    # Fix 1: same gap-snapping for ornament/separator graphic decorators.
    merged = _snap_graphic_decorators(merged, orig_w=orig_w)

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
            raw_price = item_d.get("price")
            cat.items.append(MenuItem(
                name=str(item_d.get("name") or ""),
                description=item_d.get("description"),
                price=str(raw_price) if raw_price is not None else None,
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
