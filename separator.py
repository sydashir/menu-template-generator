import cv2
import numpy as np
from PIL import Image
from typing import List, Tuple

from models import RawLine


def detect_separators(img: Image.Image) -> List[RawLine]:
    """
    Detect structural separators in a menu image.

    Three-pass strategy:
    1. Horizontal morphological lines (structural dividers)
    2. Vertical morphological lines (column dividers)
    3. Rectangle detection — groups of 2H + 2V that form a closed box are
       merged into a single RawLine(subtype='border'), preventing the 4 edges
       from appearing as 4 separate orange stripes in the rendered output.
    """
    arr = np.array(img.convert("L"))
    h, w = arr.shape

    binary = cv2.adaptiveThreshold(
        arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 15, 3
    )

    h_lines = _detect_direction(binary, w, h, "horizontal")
    v_lines = _detect_direction(binary, w, h, "vertical")

    # Merge matched H+V quads into box border elements
    boxes, remaining_h, remaining_v = _merge_into_boxes(h_lines, v_lines, w, h)

    return _dedup_lines(boxes + remaining_h + remaining_v)


# ---------------------------------------------------------------------------
# Direction detection
# ---------------------------------------------------------------------------

def _detect_direction(binary: np.ndarray, w: int, h: int, direction: str) -> List[RawLine]:
    if direction == "horizontal":
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(w // 35, 25), 1))
    else:
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(h // 35, 25)))

    mask = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    results: List[RawLine] = []
    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)

        if direction == "horizontal":
            # Require at least 10% of image width — filters illustration noise.
            if cw < w * 0.10:
                continue
            # Filter ornamental wavy/curly rules: their ragged edges make the
            # morphological contour appear 4-8px tall. Real menu separators are
            # 1-3px tall (thin rule) or ≥8px (thick decorative bar, handled as box).
            # Skip the 4-7px band which is almost exclusively decorative ornaments.
            if 4 <= ch <= 7:
                continue
            results.append(RawLine(x1=float(x), y1=float(y),
                                   x2=float(x + cw), y2=float(y + ch),
                                   orientation="horizontal"))
        else:
            if ch < h * 0.08:
                continue
            # Reject full-page-height lines at page edges (decorative page borders)
            is_edge = x < w * 0.07 or (x + cw) > w * 0.93
            if is_edge and ch > h * 0.82:
                continue
            results.append(RawLine(x1=float(x), y1=float(y),
                                   x2=float(x + cw), y2=float(y + ch),
                                   orientation="vertical"))

    return results


# ---------------------------------------------------------------------------
# Box detection: match 2H + 2V lines into a closed rectangle
# ---------------------------------------------------------------------------

def _merge_into_boxes(
    h_lines: List[RawLine],
    v_lines: List[RawLine],
    w: int,
    h: int,
    tol_frac: float = 0.03,
) -> Tuple[List[RawLine], List[RawLine], List[RawLine]]:
    """
    Find groups of (left_vertical, right_vertical, top_horizontal, bottom_horizontal)
    that form a closed rectangle, and output each group as a single
    RawLine(subtype='border') covering the full bounding box.

    Returns (boxes, remaining_h_lines, remaining_v_lines).
    """
    tol = max(h, w) * tol_frac

    used_h: set = set()
    used_v: set = set()
    boxes: List[RawLine] = []

    # Sort vertical lines left-to-right by center x
    v_indexed = sorted(enumerate(v_lines), key=lambda iv: (iv[1].x1 + iv[1].x2) / 2)

    for a, (vi, v_left) in enumerate(v_indexed):
        if vi in used_v:
            continue

        vl_cx = (v_left.x1 + v_left.x2) / 2
        vl_y1 = min(v_left.y1, v_left.y2)
        vl_y2 = max(v_left.y1, v_left.y2)

        for _b, (vj, v_right) in enumerate(v_indexed):
            if vj <= vi or vj in used_v:
                continue

            vr_cx = (v_right.x1 + v_right.x2) / 2
            vr_y1 = min(v_right.y1, v_right.y2)
            vr_y2 = max(v_right.y1, v_right.y2)

            box_w = vr_cx - vl_cx
            if box_w < w * 0.05 or box_w > w * 0.92:
                continue  # too narrow or full-page-width (not a section box)

            # Minimum box height: ornament swashes form tiny false boxes (~20-60px).
            # Real structural borders (section frames, "As seen on" box) are >=80px tall.
            box_h_approx = max(vl_y2 - vl_y1, vr_y2 - vr_y1)
            if box_h_approx < 80:
                continue

            # Both verticals must span similar y-range
            y_overlap = max(0.0, min(vl_y2, vr_y2) - max(vl_y1, vr_y1))
            min_span = min(vl_y2 - vl_y1, vr_y2 - vr_y1)
            if min_span < 1 or y_overlap / min_span < 0.6:
                continue

            box_y1 = min(vl_y1, vr_y1)
            box_y2 = max(vl_y2, vr_y2)

            # Find horizontal lines that cap the top and bottom of this box
            top_match: RawLine | None = None
            top_hi = -1
            bot_match: RawLine | None = None
            bot_hi = -1

            for k, hln in enumerate(h_lines):
                if k in used_h:
                    continue
                hx1 = min(hln.x1, hln.x2)
                hx2 = max(hln.x1, hln.x2)
                hy = (hln.y1 + hln.y2) / 2

                # H line must span at least 50% of the box's x range
                overlap_x = max(0.0, min(hx2, vr_cx + tol) - max(hx1, vl_cx - tol))
                if overlap_x / max(box_w, 1) < 0.5:
                    continue

                if abs(hy - box_y1) <= tol * 2:
                    if top_match is None or abs(hy - box_y1) < abs(
                        (top_match.y1 + top_match.y2) / 2 - box_y1
                    ):
                        top_match, top_hi = hln, k
                elif abs(hy - box_y2) <= tol * 2:
                    if bot_match is None or abs(hy - box_y2) < abs(
                        (bot_match.y1 + bot_match.y2) / 2 - box_y2
                    ):
                        bot_match, bot_hi = hln, k

            if top_match is not None and bot_match is not None:
                # Complete box found — emit as a single border element
                final_y1 = (top_match.y1 + top_match.y2) / 2
                final_y2 = (bot_match.y1 + bot_match.y2) / 2
                boxes.append(RawLine(
                    x1=float(vl_cx), y1=float(final_y1),
                    x2=float(vr_cx), y2=float(final_y2),
                    orientation="horizontal",
                    subtype="border",
                ))
                used_v.add(vi)
                used_v.add(vj)
                used_h.add(top_hi)
                used_h.add(bot_hi)
                break  # v_left has been matched; move to next v_left

    remaining_h = [ln for k, ln in enumerate(h_lines) if k not in used_h]
    remaining_v = [ln for k, ln in enumerate(v_lines) if k not in used_v]
    return boxes, remaining_h, remaining_v


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _dedup_lines(lines: List[RawLine], tol: float = 15.0) -> List[RawLine]:
    """Remove near-duplicate separator lines (within tol pixels on all coords)."""
    dedup: List[RawLine] = []
    for ln in lines:
        found = False
        for ex in dedup:
            if (
                ln.orientation == ex.orientation
                and abs(ln.x1 - ex.x1) < tol
                and abs(ln.y1 - ex.y1) < tol
                and abs(ln.x2 - ex.x2) < tol
                and abs(ln.y2 - ex.y2) < tol
            ):
                found = True
                break
        if not found:
            dedup.append(ln)
    return dedup
