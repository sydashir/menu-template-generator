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

    lines = []
    lines += _detect_direction(binary, w, h, "horizontal")
    lines += _detect_direction(binary, w, h, "vertical")
    return lines


def _detect_direction(binary: np.ndarray, w: int, h: int, direction: str) -> List[RawLine]:
    if direction == "horizontal":
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(w // 10, 40), 1))
    else:
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(h // 10, 40)))

    mask = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    results: List[RawLine] = []
    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)

        if direction == "horizontal":
            if cw < w * 0.2:  # skip short fragments
                continue
            results.append(RawLine(
                x1=float(x), y1=float(y + ch // 2),
                x2=float(x + cw), y2=float(y + ch // 2),
                orientation="horizontal",
            ))
        else:
            if ch < h * 0.1:
                continue
            results.append(RawLine(
                x1=float(x + cw // 2), y1=float(y),
                x2=float(x + cw // 2), y2=float(y + ch),
                orientation="vertical",
            ))

    return results
