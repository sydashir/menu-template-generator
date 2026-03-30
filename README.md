# Menu Template Generator

Converts restaurant menu PDFs and images into structured canvas templates.

## Setup

```bash
pip install -r requirements.txt

# Tesseract must also be installed (for image OCR)
# macOS:  brew install tesseract
# Ubuntu: sudo apt install tesseract-ocr
```

## Run (API)

```bash
uvicorn main:app --reload
```

Then open `http://localhost:8000/docs` to use the interactive upload UI.

### Endpoint

`POST /process` — upload a menu file, get back paths to the generated JSON files.

```bash
curl -X POST http://localhost:8000/process \
  -F "file=@/path/to/menu.pdf"
```

## Run (CLI / direct)

```python
from pipeline import process

results = process("menu.pdf", output_dir="outputs/my_menu")
# results is a list of dicts (one per page/side) with keys:
# side, page, menu_data, template, num_elements, num_categories
```

## Outputs

For each processed page/side two files are written to `output_dir`:

| File | Contents |
|---|---|
| `*_menu_data.json` | Semantic structure — restaurant name, categories, items, prices |
| `*_template.json` | Canvas layout — every element with x, y, width, height, style |

### template.json structure

```json
{
  "version": "1.0.0",
  "canvas": { "width": 1700, "height": 2200, "unit": "px" },
  "elements": [
    {
      "id": "text_a3f2bc10",
      "type": "text",
      "subtype": "category_header",
      "bbox": { "x": 285.0, "y": 431.0, "w": 210.0, "h": 42.0 },
      "content": "Breakfast",
      "style": { "font_size": 36.0, "font_weight": "bold", "text_align": "left" },
      "column": 0
    },
    {
      "id": "sep_9d1e4f22",
      "type": "separator",
      "subtype": "horizontal_line",
      "bbox": { "x": 43.0, "y": 620.0, "w": 694.0, "h": 2.0 }
    }
  ]
}
```

Element IDs are deterministic MD5 hashes of `type + content + x + y` — same input always produces the same ID.

## Supported Input Formats

| Format | Extraction method |
|---|---|
| PDF | `pymupdf` — precise coordinates, font sizes, embedded images |
| JPG / PNG | `pytesseract` — OCR with bounding boxes |
| PSD | **Not supported** — export to PNG first |

## Front & Back Menus

Several menus in the dataset are printed as landscape spreads (front on the left, back on the right). The tool auto-detects these by aspect ratio and splits them into two outputs (`_front` / `_back`).

Files this applies to (per `menu explanations.pdf`):
- `Front and back_DINNER Menu 11x17`
- `AMI New Cocktail 11x17`
- `bar & Patio 1224` (left = front, right = back)

## Example Outputs

### AMI BRUNCH 2022.pdf
- 2 columns (Breakfast / Lunch)
- 4 sections, ~15 items extracted
- Prices extracted from inline format (`ITEM NAME  PRICE`)

### bar & Patio 1224.pdf
- 2 pages, each processed separately
- 12 separator lines detected on page 1
- Sections: Starters, Soup, Pizza, Burgers, Sandwiches, Salads

## Limitations

- **Stylized / decorative fonts** — Tesseract struggles with script fonts common in upscale menus. PDF extraction is unaffected.
- **Scanned PDFs** — PDFs without embedded text (image-only scans) fall back to OCR and have lower accuracy.
- **PSD files** — Photoshop files are not supported. Export to PNG at 150+ DPI before processing.
- **Separator detection on images** — Light or short decorative lines may be missed; very faint background textures can produce false positives.
- **Multi-price items** — Formats like `CUP 11 / BOWL 13` are kept as-is in the item name; the tool does not split them into size variants.
- **Logos** — Extracted from embedded PDF images only. Logos burned into the page background (flattened) are not detected.

## Improvements (given more time)

- Use a multi-scale OCR pass for image files to improve accuracy on small/decorative text
- Add logo detection for image files via contour analysis in the header region
- Handle multi-price items (`CUP 11 / BOWL 13`) as structured size/price pairs
- Add a rendered preview image (PIL draw) showing all bounding boxes overlaid on the source
