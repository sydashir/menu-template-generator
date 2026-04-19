import io
import os
import json
import base64
from typing import Optional

from google import genai
from google.genai import types
from PIL import Image
from dotenv import load_dotenv

from models import MenuData, MenuCategory, MenuItem
from claude_extractor import (
    _MAX_IMG_DIM, _bbox_iou, _dedup_text_elements, 
    _snap_decorative_headers, _mask_logo_elements, 
    _enforce_single_logo, _draw_som_annotations, 
    extract_blocks_surya
)

load_dotenv()

_client: Optional[genai.Client] = None

def _get_client() -> Optional[genai.Client]:
    global _client
    if _client is not None:
        return _client
    # Try multiple key names to match user environment
    key = (os.environ.get("GOOGLE_API_KEY") or 
           os.environ.get("GEMINI_API_KEY") or 
           os.environ.get("GOOGLE_GENAI_API_KEY"))
    if not key:
        return None
    _client = genai.Client(api_key=key)
    return _client

_GEMINI_SYSTEM_PROMPT = """\
You are a precise restaurant menu layout analyst. Your goal is to achieve 95%+ accuracy.
You will receive TWO images of the same menu page:
1. IMAGE 1: Clean original — use this to READ text accurately and locate the logo/branding.
2. IMAGE 2: Annotated with numbered Set-of-Marks boxes — use this ONLY to identify the IDs of the OCR blocks provided in the list.

Task:
1. Label every OCR block by its ID. Assign 'subtype', 'column' (0 or 1), 'font_family', and 'corrected_text' if OCR misread it.
2. CRITICAL — Capture decorative elements (cursive/script section headers) that OCR missed. Provide 'bbox' relative to nearby OCR blocks.
3. logo_bbox: Find the FULL restaurant branding block (emblem + text graphic) as one box.
4. background_color: Dominant hex color.

Output MUST be valid JSON matching the schema.
"""

def extract_layout_surya_som_gemini(img: Image.Image) -> dict | None:
    """
    Mirror of extract_layout_surya_som using Gemini 2.0 Flash.
    Bypasses Anthropic 403 errors on restricted IPs.
    """
    client = _get_client()
    if client is None:
        print("[gemini_som] no API key found")
        return None

    surya_blocks = extract_blocks_surya(img)
    if not surya_blocks:
        return None

    # Apply Set-of-Marks (SoM) annotations
    annotated_img = _draw_som_annotations(img, surya_blocks)
    orig_w, orig_h = img.size
    
    # Scale for API
    send_img = annotated_img
    clean_send = img
    scale_x = scale_y = 1.0
    if max(orig_w, orig_h) > _MAX_IMG_DIM:
        ratio = _MAX_IMG_DIM / max(orig_w, orig_h)
        new_w, new_h = max(1, int(orig_w * ratio)), max(1, int(orig_h * ratio))
        send_img = annotated_img.resize((new_w, new_h), Image.LANCZOS)
        clean_send = img.resize((new_w, new_h), Image.LANCZOS)
        scale_x = orig_w / new_w
        scale_y = orig_h / new_h

    sw, sh = send_img.size

    # Prepare block list for context
    lines = []
    for i, b in enumerate(surya_blocks):
        x1, y1, x2, y2 = b["bbox"]
        cx1, cy1 = x1 / scale_x, y1 / scale_y
        cw, ch = (x2 - x1) / scale_x, (y2 - y1) / scale_y
        text = b["text"][:77] + "..." if len(b["text"]) > 80 else b["text"]
        lines.append(f"[{i + 1}] \"{text}\" — x={cx1:.0f} y={cy1:.0f} w={cw:.0f} h={ch:.0f}")
    block_list = "\n".join(lines)

    user_msg = f"Images are {sw}x{sh}px.\nOCR blocks:\n{block_list}\n\nExtract the layout into JSON."

    try:
        # Convert PIL to bytes for SDK
        clean_bytes = io.BytesIO()
        clean_send.save(clean_bytes, format="JPEG", quality=85)
        annotated_bytes = io.BytesIO()
        send_img.save(annotated_bytes, format="JPEG", quality=85)

        response = client.models.generate_content(
            model="gemini-2.0-flash-001",
            contents=[
                "IMAGE 1 (Clean):",
                types.Part.from_bytes(data=clean_bytes.getvalue(), mime_type="image/jpeg"),
                "IMAGE 2 (Annotated):",
                types.Part.from_bytes(data=annotated_bytes.getvalue(), mime_type="image/jpeg"),
                user_msg
            ],
            config=types.GenerateContentConfig(
                system_instruction=_GEMINI_SYSTEM_PROMPT,
                response_mime_type="application/json",
            )
        )
        
        # Strip markdown if present
        text = response.text
        if text.startswith("```"):
            text = text.split("```")[1].lstrip("json").strip()
            
        data = json.loads(text)
    except Exception as e:
        print(f"[gemini_som] error: {e}")
        return None

    # Build elements (Mirroring Claude logic)
    elements: list[dict] = []
    label_map = {lbl.get("id"): lbl for lbl in data.get("ocr_labels", []) if lbl.get("id")}

    for i, b in enumerate(surya_blocks):
        lbl = label_map.get(i + 1, {})
        x1, y1, x2, y2 = b["bbox"]
        elem_h = max(1.0, y2 - y1)
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

    lb = data.get("logo_bbox")
    if isinstance(lb, dict) and lb.get("w", 0) > 0 and lb.get("h", 0) > 0:
        if scale_x != 1.0 or scale_y != 1.0:
            lb = {"x": lb.get("x", 0) * scale_x, "y": lb.get("y", 0) * scale_y,
                  "w": lb.get("w", 0) * scale_x,  "h": lb.get("h", 0) * scale_y}
        elements.append({"type": "logo", "bbox": lb, "position_hint": "top_center"})

    # Apply the same high-accuracy cleanup engine
    elements = _dedup_text_elements(elements)
    elements = _snap_decorative_headers(elements, orig_w=orig_w)
    elements = _mask_logo_elements(elements)

    print(f"[gemini_som] built {len(elements)} elements")
    return {
        "elements": elements,
        "menu_data": data.get("menu_data", {}),
        "background_color": data.get("background_color", "#ffffff"),
    }

def extract_full_layout_via_gemini(img: Image.Image) -> dict | None:
    """Holistic pass using Gemini when Surya is unavailable."""
    client = _get_client()
    if client is None: return None
    
    orig_w, orig_h = img.size
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    
    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash-001",
            contents=[
                types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"),
                f"Extract complete menu layout for {orig_w}x{orig_h} image into JSON."
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            )
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"[gemini_holistic] error: {e}")
        return None
