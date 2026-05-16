"""
Hybrid Engine — combines physical OpenCV shapes with semantic text context.
Provides high-accuracy validation and labeling for graphics (logos, separators, badges).
"""

from typing import List, Dict, Any, Optional
from models import TextElement, BBox, SeparatorElement, LogoElement, ImageElement


def validate_graphic_elements(
    text_elements: List[Dict[str, Any]],
    raw_cv_lines: List[Dict[str, Any]],
    raw_cv_contours: List[Dict[str, Any]],
    matched_assets: List[Dict[str, Any]],
    canvas_w: int,
    canvas_h: int
) -> List[Dict[str, Any]]:
    """
    Filter and label raw OpenCV detections using the semantic context of text blocks.
    
    1. Filter out contours that overlap heavily with text (removes text-related noise).
    2. Identify the 'Logo' as the topmost significant non-text contour.
    3. Validate 'Separators' by checking if they sit in gaps between semantic text blocks.
    4. Incorporate high-confidence 'Matched Assets' (badges) directly.
    """
    final_elements = []
    
    # Pre-process text elements into a spatial index or sorted list for fast lookup.
    # Defensive: bbox values can leak through as strings from Claude vision output;
    # coerce to float so the sort never crashes on str/int comparison.
    def _safe_y(el):
        try:
            return float((el.get("bbox") or {}).get("y", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    sorted_text = sorted(text_elements, key=_safe_y)
    
    # 1. Handle Matched Assets (Badges) — High Confidence
    # matched_assets should already have semantic_label (e.g. 'badge/food_network')
    for asset in matched_assets:
        final_elements.append(asset)
        
    # 2. Filter Contours (Potential Logos/Ornaments)
    # Remove any contour that intersects significantly with any text element
    valid_contours = []
    for cnt in raw_cv_contours:
        bbox = cnt["bbox"]
        if _intersects_any_text(bbox, text_elements, threshold=0.15):
            continue
        valid_contours.append(cnt)
        
    # Identify Logo: Topmost valid contour that isn't a thin line
    if valid_contours:
        # Sort by Y position
        valid_contours.sort(key=lambda c: c["bbox"]["y"])
        logo_candidate = valid_contours[0]
        # If it's in the top 30% of the page, it's very likely the logo
        if logo_candidate["bbox"]["y"] < canvas_h * 0.35:
            final_elements.append({
                "type": "logo",
                "bbox": logo_candidate["bbox"],
                "id": f"logo_hybrid_{int(logo_candidate['bbox']['y'])}"
            })
            # Remove from ornaments list
            valid_contours = valid_contours[1:]
            
    # Remaining valid contours are treated as 'ornaments' or 'images'
    for cnt in valid_contours:
        final_elements.append({
            "type": "image",
            "subtype": "ornament",
            "bbox": cnt["bbox"],
            "id": f"ornament_hybrid_{int(cnt['bbox']['y'])}"
        })
        
    # 3. Validate Separators
    for line in raw_cv_lines:
        bbox = line["bbox"]
        # A valid structural separator should sit in a gap between text blocks
        # or be a very long line that doesn't collide with text.
        if _intersects_any_text(bbox, text_elements, threshold=0.05):
            continue
            
        # Optional: Further validate horizontal separators by checking if they
        # reside between category headers.
        
        final_elements.append({
            "type": "separator",
            "subtype": line.get("subtype", "horizontal_line"),
            "orientation": line.get("orientation", "horizontal"),
            "bbox": bbox,
            "id": f"sep_hybrid_{int(bbox['y'])}_{int(bbox['x'])}"
        })
        
    return final_elements


def _intersects_any_text(bbox: Dict[str, float], text_elements: List[Dict[str, Any]], threshold: float) -> bool:
    """Check if the given bbox intersects with any text element above the threshold IoU/Overlap."""
    for te in text_elements:
        t_bbox = te["bbox"]
        if _get_intersection_ratio(bbox, t_bbox) > threshold:
            return True
    return False


def _get_intersection_ratio(a: Dict[str, float], b: Dict[str, float]) -> float:
    """Calculate how much of box 'a' is covered by box 'b' (Area of Intersection / Area of A)."""
    ax1, ay1 = a["x"], a["y"]
    ax2, ay2 = ax1 + a["w"], ay1 + a["h"]
    bx1, by1 = b["x"], b["y"]
    bx2, by2 = bx1 + b["w"], by1 + b["h"]
    
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
        
    intersection_area = (ix2 - ix1) * (iy2 - iy1)
    area_a = a["w"] * a["h"]
    
    return intersection_area / area_a if area_a > 0 else 0.0
