import re
import fitz
fitz.TOOLS.mupdf_display_errors(False)  # suppress non-fatal MuPDF stderr warnings (e.g. broken structure trees)
import cv2
import pytesseract
import numpy as np
from PIL import Image
from pathlib import Path
from typing import List, Tuple

from models import RawBlock

SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
SUPPORTED_PDF_EXTS = {".pdf"}
PDF_RENDER_DPI = 200


def load_pages(file_path: str) -> List[Tuple[Image.Image, int]]:
    """Return list of (PIL Image, page_index) for any supported input."""
    p = Path(file_path)
    ext = p.suffix.lower()

    if ext in SUPPORTED_PDF_EXTS:
        return _pdf_to_images(file_path)
    elif ext in SUPPORTED_IMAGE_EXTS:
        img = Image.open(file_path).convert("RGB")
        return [(img, 0)]
    else:
        raise ValueError(
            f"Unsupported file type '{ext}'. "
            f"Supported: PDF, JPG, PNG. "
            f"PSD files must be exported to JPG/PNG first."
        )


def _pdf_to_images(path: str) -> List[Tuple[Image.Image, int]]:
    doc = fitz.open(path)
    pages = []
    mat = fitz.Matrix(PDF_RENDER_DPI / 72, PDF_RENDER_DPI / 72)
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=mat)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        pages.append((img, i))
    return pages


def extract_blocks_pdf(file_path: str) -> List[List[RawBlock]]:
    """Extract text blocks from a PDF with exact positions via pymupdf.
    Returns a list per page."""
    doc = fitz.open(file_path)
    all_pages = []

    for page_idx, page in enumerate(doc):
        mat = fitz.Matrix(PDF_RENDER_DPI / 72, PDF_RENDER_DPI / 72)
        scale = PDF_RENDER_DPI / 72
        blocks = []

        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:  # skip image blocks
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    text = _normalize_spaced(span["text"].strip())
                    if not text:
                        continue
                    r = span["bbox"]
                    blocks.append(RawBlock(
                        text=text,
                        x=r[0] * scale,
                        y=r[1] * scale,
                        w=(r[2] - r[0]) * scale,
                        h=(r[3] - r[1]) * scale,
                        font_size=span["size"] * scale,
                        is_bold="Bold" in span["font"] or "bold" in span["font"],
                        is_italic="Italic" in span["font"] or "italic" in span["font"],
                        page=page_idx,
                        source="pdf",
                    ))
        all_pages.append(blocks)
    return all_pages


def extract_separators_pdf(
    file_path: str,
    page_idx: int,
    side_label: str = "full",
    side_canvas_w: float | None = None,
) -> List["RawLine"]:
    """Extract separator-like lines directly from PDF vector drawings.

    This yields more faithful divider positions than image morphology for PDFs.
    """
    from models import RawLine

    doc = fitz.open(file_path)
    page = doc[page_idx]
    scale = PDF_RENDER_DPI / 72
    lines: List[RawLine] = []

    def _project_x(x: float) -> float | None:
        if side_label == "full":
            return x
        if side_canvas_w is None:
            return x
        if side_label == "front":
            if x < side_canvas_w:
                return x
            return None
        if side_label == "back":
            if x >= side_canvas_w:
                return x - side_canvas_w
            return None
        return x

    _MIN_LINE_PX = 30  # minimum length to qualify as a real separator (filters decorative strokes)
    canvas_w_px = page.rect.width * scale

    def _is_dark(color) -> bool:
        """Return True if the color is dark enough to be a meaningful separator element."""
        if color is None:
            return False
        r, g, b = float(color[0]), float(color[1]), float(color[2])
        return 0.299 * r + 0.587 * g + 0.114 * b < 0.35

    drawings = page.get_drawings()
    for d in drawings:
        d_bbox = d.get("rect")
        items = d.get("items", [])
        d_fill = d.get("fill")
        d_color = d.get("color")

        for item in items:
            kind = item[0]

            # Explicit line segment — skip short decorative strokes
            if kind == "l" and len(item) >= 3:
                p1, p2 = item[1], item[2]
                x1 = _project_x(float(p1.x) * scale)
                x2 = _project_x(float(p2.x) * scale)
                if x1 is None or x2 is None:
                    continue
                y1 = float(p1.y) * scale
                y2 = float(p2.y) * scale
                dx = abs(x2 - x1)
                dy = abs(y2 - y1)
                if max(dx, dy) < _MIN_LINE_PX:
                    continue  # skip short decorative strokes
                orientation = "horizontal" if dx >= dy else "vertical"
                lines.append(RawLine(x1=x1, y1=y1, x2=x2, y2=y2, orientation=orientation))

            # Rectangles used as dividers
            elif kind == "re" and len(item) >= 2:
                r = item[1]
                rx0 = _project_x(float(r.x0) * scale)
                rx1 = _project_x(float(r.x1) * scale)
                if rx0 is None or rx1 is None:
                    continue
                ry0 = float(r.y0) * scale
                ry1 = float(r.y1) * scale
                rw = abs(rx1 - rx0)
                rh = abs(ry1 - ry0)
                # Thin horizontal lines: always valid (no color filter)
                if rw >= 20 and rh <= 6:
                    lines.append(RawLine(
                        x1=min(rx0, rx1), y1=ry0,
                        x2=max(rx0, rx1), y2=ry1,
                        orientation="horizontal",
                    ))
                # Thick horizontal bands: must be wider than tall (aspect ratio >= 2:1) and dark fill
                elif rw >= 20 and 6 < rh <= min(rw * 0.5, 80) and _is_dark(d_fill):
                    lines.append(RawLine(
                        x1=min(rx0, rx1), y1=ry0,
                        x2=max(rx0, rx1), y2=ry1,
                        orientation="horizontal",
                    ))
                # Thin vertical lines: always valid
                elif rh >= 20 and rw <= 6:
                    lines.append(RawLine(
                        x1=rx0, y1=min(ry0, ry1),
                        x2=rx0, y2=max(ry0, ry1),
                        orientation="vertical",
                    ))
                # Thick vertical bands: must be taller than wide (aspect ratio >= 2:1) and dark fill
                elif rh >= 20 and 6 < rw <= min(rh * 0.5, 80) and _is_dark(d_fill):
                    lines.append(RawLine(
                        x1=rx0, y1=min(ry0, ry1),
                        x2=rx0, y2=max(ry0, ry1),
                        orientation="vertical",
                    ))

        # Compound bezier-path drawings (ornamental dividers) — use the drawing's bounding box
        # Only for dark-filled ornaments (not light backgrounds or decorative colored curves)
        if d_bbox and sum(1 for it in items if it[0] == "c") >= 3 and _is_dark(d_fill or d_color):
            drw = abs(d_bbox.x1 - d_bbox.x0) * scale
            drh = abs(d_bbox.y1 - d_bbox.y0) * scale
            # Wide ornament: at least 150px wide, not taller than it is wide, not full-canvas background
            if drw >= 150 and drh <= drw and drw <= canvas_w_px * 0.95:
                bx0 = _project_x(float(d_bbox.x0) * scale)
                bx1 = _project_x(float(d_bbox.x1) * scale)
                if bx0 is not None and bx1 is not None:
                    by0 = float(d_bbox.y0) * scale
                    by1 = float(d_bbox.y1) * scale
                    lines.append(RawLine(
                        x1=bx0, y1=by0,
                        x2=bx1, y2=by1,
                        orientation="horizontal",
                    ))

    # De-duplicate near-identical lines.
    # Use normalized spans (left<right for H, top<bottom for V) so direction of drawing doesn't matter.
    # For overlapping thick-band + thin-line pairs at the same visual position, keep the thicker one.
    _TOL = 8  # px tolerance for matching

    def _span(ln: "RawLine"):
        if ln.orientation == "horizontal":
            return (min(ln.x1, ln.x2), max(ln.x1, ln.x2), (ln.y1 + ln.y2) / 2)
        else:
            return ((ln.x1 + ln.x2) / 2, min(ln.y1, ln.y2), max(ln.y1, ln.y2))

    def _thickness(ln: "RawLine") -> float:
        if ln.orientation == "horizontal":
            return abs(ln.y2 - ln.y1)
        return abs(ln.x2 - ln.x1)

    def _close(ln: "RawLine", ex: "RawLine") -> bool:
        if ln.orientation != ex.orientation:
            return False
        if ln.orientation == "horizontal":
            la, lb, lc = min(ln.x1, ln.x2), max(ln.x1, ln.x2), (ln.y1 + ln.y2) / 2
            ea, eb, ec = min(ex.x1, ex.x2), max(ex.x1, ex.x2), (ex.y1 + ex.y2) / 2
        else:
            la, lb, lc = (ln.x1 + ln.x2) / 2, min(ln.y1, ln.y2), max(ln.y1, ln.y2)
            ea, eb, ec = (ex.x1 + ex.x2) / 2, min(ex.y1, ex.y2), max(ex.y1, ex.y2)
        return abs(la - ea) < _TOL and abs(lb - eb) < _TOL and abs(lc - ec) < _TOL

    dedup: List[RawLine] = []
    for ln in lines:
        matched_idx = None
        for i, ex in enumerate(dedup):
            if _close(ln, ex):
                matched_idx = i
                break
        if matched_idx is None:
            dedup.append(ln)
        else:
            # Keep the thicker one (more visually accurate)
            if _thickness(ln) > _thickness(dedup[matched_idx]):
                dedup[matched_idx] = ln

    return dedup


def preprocess_for_ocr(img: Image.Image) -> Image.Image:
    """
    Improve OCR accuracy via:
    1. Upscale small images (tesseract works best at ~300 DPI equivalent)
    2. CLAHE contrast enhancement (helps colored / low-contrast backgrounds)
    3. Adaptive binarization (converts colored background to clean black/white)
    """
    arr = np.array(img.convert("RGB"))
    h, w = arr.shape[:2]

    # Upscale if narrower than 1800px
    if w < 1800:
        scale = 1800 / w
        arr = cv2.resize(arr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)

    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

    # CLAHE evens out contrast across the image (handles dark corners, light centers)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)

    # Adaptive threshold separates text from any background color/texture
    binary = cv2.adaptiveThreshold(
        enhanced, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31, 10,
    )

    return Image.fromarray(binary)


def extract_blocks_image(img: Image.Image, page_idx: int = 0) -> List[RawBlock]:
    """Extract text blocks from a PIL image using tesseract."""
    processed = preprocess_for_ocr(img)
    # Scale factor to map processed coords back to original image space
    orig_w, _ = img.size
    proc_w, _ = processed.size
    coord_scale = orig_w / proc_w

    data = pytesseract.image_to_data(
        processed, output_type=pytesseract.Output.DICT,
        config="--psm 3 --oem 1",
    )
    blocks: List[RawBlock] = []
    n = len(data["text"])

    for i in range(n):
        text = data["text"][i].strip()
        if not text or int(data["conf"][i]) < 30:
            continue
        x = float(data["left"][i]) * coord_scale
        y = float(data["top"][i]) * coord_scale
        w = float(data["width"][i]) * coord_scale
        h = float(data["height"][i]) * coord_scale
        if w < 2 or h < 2:
            continue
        blocks.append(RawBlock(
            text=text,
            x=x, y=y, w=w, h=h,
            font_size=h * 0.75,  # approximate pt from pixel height
            page=page_idx,
            source="ocr",
        ))
    return _merge_ocr_words(blocks)


def _merge_ocr_words(blocks: List[RawBlock]) -> List[RawBlock]:
    """Merge individual words that are on the same line into line-level blocks."""
    if not blocks:
        return []

    blocks = sorted(blocks, key=lambda b: (round(b.y / 5) * 5, b.x))
    merged: List[RawBlock] = []
    current = blocks[0]

    for b in blocks[1:]:
        same_line = abs(b.y - current.y) < current.h * 0.6
        close_enough = b.x <= current.x + current.w + current.h * 1.5

        if same_line and close_enough:
            new_w = (b.x + b.w) - current.x
            current = RawBlock(
                text=current.text + " " + b.text,
                x=current.x,
                y=min(current.y, b.y),
                w=new_w,
                h=max(current.h, b.h),
                font_size=max(current.font_size, b.font_size),
                is_bold=current.is_bold or b.is_bold,
                is_italic=current.is_italic or b.is_italic,
                page=current.page,
                source="ocr",
            )
        else:
            merged.append(current)
            current = b

    merged.append(current)
    return merged


def detect_logo_pdf(file_path: str, page_idx: int = 0) -> dict | None:
    """Extract the first embedded image from a PDF page as a logo candidate.

    Note: we rasterize the image rectangle from the rendered PDF page instead of
    returning raw embedded image bytes. Some PDFs store logos with masks/CMYK
    data that can appear as black boxes in browsers when decoded directly.
    """
    doc = fitz.open(file_path)
    page = doc[page_idx]
    scale = PDF_RENDER_DPI / 72
    mat = fitz.Matrix(scale, scale)
    image_list = page.get_images(full=True)

    if not image_list:
        return None

    for img_info in image_list:
        xref = img_info[0]
        rects = page.get_image_rects(xref)
        if not rects:
            continue
        r = rects[0]

        # Render clipped region to RGB PNG bytes for robust browser display.
        clip_pix = page.get_pixmap(matrix=mat, clip=r, alpha=False)
        logo_bytes = clip_pix.tobytes("png")

        return {
            "x": r.x0 * scale,
            "y": r.y0 * scale,
            "w": (r.x1 - r.x0) * scale,
            "h": (r.y1 - r.y0) * scale,
            "image_bytes": logo_bytes,
            "ext": "png",
        }
    return None


_CHAR_SPACED = re.compile(r"^([A-Za-z0-9](?:\s[A-Za-z0-9]){2,})$")


def _normalize_spaced(text: str) -> str:
    """Collapse 'D A I L Y' → 'DAILY' for character-spaced PDF text."""
    stripped = text.strip()
    if _CHAR_SPACED.match(stripped):
        return stripped.replace(" ", "")
    return stripped


def is_double_sided(img: Image.Image) -> bool:
    """Return True if the image looks like a front+back print layout (landscape spread).
    Requires aspect ratio >= 1.8 — true spreads are always close to 2:1.
    Landscape single-page menus (~1.4-1.6:1) are intentionally excluded."""
    w, h = img.size
    return w > h * 1.8


def split_double_sided(img: Image.Image) -> Tuple[Image.Image, Image.Image]:
    """Split a landscape spread into left (front) and right (back) halves."""
    w, h = img.size
    mid = w // 2
    front = img.crop((0, 0, mid, h))
    back = img.crop((mid, 0, w, h))
    return front, back
