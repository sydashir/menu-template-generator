import cv2
import numpy as np
from PIL import Image
from typing import List

from models import RawLine


def detect_separators(img: Image.Image) -> List[RawLine]:
    """Detect horizontal and vertical lines in a menu image."""
    arr = np.array(img.convert("L"))
    h, w = arr.shape

    # Adaptive threshold works better than Canny on menus with varied backgrounds
    binary = cv2.adaptiveThreshold(
        arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 15, 3
    )

    lines: List[RawLine] = []
    lines += _detect_direction(binary, w, h, "horizontal")
    lines += _detect_direction(binary, w, h, "vertical")
    return _dedup_lines(lines)


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
            if cw < w * 0.04:  # calibrated threshold (down from 6%, above the 3% noise floor)
                continue
            results.append(RawLine(
                x1=float(x), y1=float(y),
                x2=float(x + cw), y2=float(y + ch),
                orientation="horizontal",
            ))
        else:
            if ch < h * 0.04:  # calibrated vertical threshold
                continue
            results.append(RawLine(
                x1=float(x), y1=float(y),
                x2=float(x + cw), y2=float(y + ch),
                orientation="vertical",
            ))

    return results


def _dedup_lines(lines: List[RawLine], tol: float = 15.0) -> List[RawLine]:
    """Remove near-duplicate separator lines (within tol pixels in all coordinates)."""
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
