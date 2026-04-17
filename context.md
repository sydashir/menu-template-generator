# Menu Template Generator — Session Context

## Project Overview

A FastAPI pipeline that takes restaurant menu images/PDFs and outputs structured JSON:
- `_menu_data.json` — categories, items, prices, restaurant info (MenuData model)
- `_template.json` — canvas elements with bboxes, fonts, separators, logos (Template model)

Entry points: `main.py` (FastAPI), `pipeline.py` (core logic)

Key files:
- `pipeline.py` — orchestrates extraction → analysis → build
- `builder.py` — builds Template and MenuData models from raw/Claude data
- `claude_extractor.py` — Claude Vision extraction (Surya+SoM ensemble)
- `separator.py` — OpenCV-based line detection
- `analyzer.py` — column detection, block classification
- `extractor.py` — PDF/image loading, logo detection
- `models.py` — Pydantic models (Template, MenuData, BBox, TextStyle, etc.)
- `json_canvas_renderer.html` — browser-based canvas renderer/validator

---

## Changes Made This Session

### 1. Combined JSON Output (ATTEMPTED, REVERTED BY USER)

I changed `pipeline.py` to write ONE combined `{base_name}.json` instead of two files:
```json
{
  "menu_data": { ...MenuData... },
  "template": { ...Template... }
}
```
**User reverted this** — they went back to two separate files (`_menu_data.json` + `_template.json`).
Current state: back to two files.

---

### 2. Renderer Changes (json_canvas_renderer.html) — PARTIALLY APPLIED

Changes that ARE in the renderer:

**Italic fix:**
- Google Fonts updated to load Montserrat AND Playfair Display italic variants:
  `Montserrat:ital,wght@0,400;0,700;1,400;1,700`
- Font string construction fixed — only adds `italic`/`bold` when non-normal:
  ```js
  const fontParts = [];
  if (fontStyle === 'italic') fontParts.push('italic');
  if (fontWeight === 'bold') fontParts.push('bold');
  fontParts.push(`${fontSize}px`);
  fontParts.push(fontFamilyCSS);
  ctx.font = fontParts.join(' ');
  ```
- Added `await document.fonts.ready` before rendering so web fonts load first

**Page tabs:**
- Replaced select dropdown + Prev/Next buttons with dynamic page tab buttons
- Tabs only appear when >1 file is loaded
- Each tab is labeled "Page 1", "Page 2", etc.
- Active tab highlighted in green

**Combined format support (STILL IN RENDERER but output reverted to 2 files):**
- Renderer detects combined format: `if (j.template && j.menu_data)` → extracts both
- Falls back to treating whole file as template (legacy format)
- NOTE: The separate "Optional menu_data JSON" input section was REMOVED from the renderer
- This means you can no longer load `_menu_data.json` separately in the renderer
- **This is a sync issue** — renderer expects combined OR legacy template-only, but pipeline now outputs two separate files again

**Remaining renderer sync issue:** Either:
- Re-add the separate menu_data file input to the renderer, OR
- Go back to combined JSON output from the pipeline

---

### 3. Separator Fix — USER APPLIED IN pipeline.py

The user made a smart fix in `pipeline.py` (Claude Vision path):

```python
# Strip Claude's hallucinated separators
elements = [e for e in claude_layout.get("elements", []) if e.get("type") != "separator"]
# Run OpenCV separator detection on the actual image
img_lines = detect_separators(side_img)
# Inject OpenCV lines as separator elements
for ln in img_lines:
    x1, y1 = min(ln.x1, ln.x2), min(ln.y1, ln.y2)
    w = abs(ln.x2 - ln.x1) if ln.orientation == "horizontal" else max(2.0, abs(ln.x2 - ln.x1))
    h = abs(ln.y2 - ln.y1) if ln.orientation == "vertical" else max(2.0, abs(ln.y2 - ln.y1))
    elements.append({
        "type": "separator",
        "subtype": "horizontal_line" if ln.orientation == "horizontal" else "vertical_line",
        "orientation": ln.orientation,
        "bbox": {"x": float(x1), "y": float(y1), "w": float(w), "h": float(h)},
        "style": {"color": "#000000", "stroke_width": float(h if ln.orientation == "horizontal" else w), "stroke_style": "solid"},
    })
claude_layout["elements"] = elements
```

**Why:** Claude was hallucinating separator positions — confirmed with data that a separator at `y=1070-1120` was cutting through "GRILLED PORTOBELLO PANINI" and "balsamic marinated portobello mushroom" text (100% overlap).

**Accuracy impact:** ~0% on the score metric (score only checks separator COUNT not position). But significant visual quality improvement.

---

## Confirmed Data Issues (from actual JSON inspection)

### AMI_brunch_Lunch_Menu.json — Logo Text Duplication (NOT YET FIXED)

Logo bbox: `x=300-740, y=10-235` (canvas 1215x2000)

Claude extracted these as SEPARATE elements even though they're inside the logo image:
```
y=49-76   x=570-623   [text]      'the'
y=85-170  x=388-802   [text]      'CHâTeau'
y=155-158 x=310-350   [separator] (dash left of ANNA MARIA)
y=155-158 x=670-710   [separator] (dash right of ANNA MARIA)
y=175-221 x=463-725   [text]      'ANNA MARIA'
y=232-268 x=514-678   [text]      'CAR'  ← decorative swash misread as text
```

**"CAR"** is a decorative swash/flourish at the bottom of the logo misidentified as text.
It starts at y=232, logo ends at y=235 — so "CAR" (h=36) pokes 33px BELOW the logo image, appearing visually as text under the logo.

**Proposed fix (NOT yet applied — tool use was rejected):**
In `build_template_from_claude` in `builder.py`, after building all elements:
- Remove text/separator elements where `element.y1` is within logo's y-range AND `element.center_x` is within logo's x-range
- This correctly removes: `the`, `CHâTeau`, `ANNA MARIA`, `CAR`, and the two dash separators
- Correctly keeps: `Brunch` (x=870-1100, center_x=985, outside logo x=300-740)
- Correctly keeps: `9:00AM - 2:00PM` (center_x=1033, outside logo x range)
- Correctly keeps: `Breakfast`, `Lunch` (y=260, below logo y2=235)

### AMI_brunch_Lunch_Menu.json — False Separator (NOW FIXED by user's pipeline change)

Separator at `y=1070-1120, x=350-850`:
- 65% overlap with "GRILLED PORTOBELLO PANINI 17" (y=1062-1085)
- 100% overlap with "balsamic marinated portobello mushroom" (y=1087-1107)
→ Caused strikethrough visual effect. Fixed by replacing Claude separators with OpenCV.

---

## Accuracy Score Formula (in renderer)

```js
total = textCoverage * 0.70 + hasLogo * 0.10 + hasSep * 0.10 + hasHeaders * 0.10
```

- `textCoverage` = % of PDF words found in JSON text elements
- `hasLogo` = 0 or 1
- `hasSep` = min(1, separatorCount / 2) — only checks COUNT not position
- `hasHeaders` = 1 if any category_header elements exist

Score is blind to: separator positions, false-positive text elements, elements within logo bbox.

---

## Current State (what's done, what's pending)

### DONE:
- [x] Renderer: italic font fix (Google Fonts + canvas font string + fonts.ready)
- [x] Renderer: page tabs for multi-page navigation
- [x] Renderer: combined JSON support (fallback to legacy works)
- [x] Pipeline: OpenCV separators replace Claude's hallucinated ones

### PENDING / KNOWN ISSUES:
- [ ] **Logo text duplication** — `the`, `CHâTeau`, `ANNA MARIA`, `CAR` still appear as text elements within logo bbox in `build_template_from_claude` (builder.py). Fix: filter elements where y1 is inside logo y-range AND center_x is inside logo x-range.
- [ ] **Renderer sync** — Renderer lost the separate menu_data file input but pipeline still outputs two files. Need to either: (a) re-add menu_data input to renderer, or (b) revert pipeline to combined output.

---

## Output File Structure

### Current (two files per page/side):
```
outputs/
  {menu_name}/
    {stem}_menu_data.json    ← MenuData model
    {stem}_template.json     ← Template model
    (multi-page: {stem}_p1_menu_data.json, {stem}_p1_template.json, etc.)
```

### Template JSON structure:
```json
{
  "version": "1.0.0",
  "metadata": { "source_file", "page", "side", "generated_at", "num_columns" },
  "canvas": { "width", "height", "unit", "background_color" },
  "elements": [
    { "type": "text", "subtype": "item_name|category_header|...", "bbox": {x,y,w,h}, "content": "...", "style": {font_size, font_weight, font_style, font_family, color, text_align}, "column": 0 },
    { "type": "separator", "subtype": "horizontal_line|decorative_divider|...", "orientation": "horizontal|vertical", "bbox": {x,y,w,h}, "style": {color, stroke_width, stroke_style} },
    { "type": "logo", "bbox": {x,y,w,h}, "image_data": "base64...", "position_hint": "top_center" }
  ]
}
```

### MenuData JSON structure:
```json
{
  "source_file": "...",
  "side": "full|front|back",
  "restaurant_name": "...",
  "tagline": null,
  "address": null,
  "phone": null,
  "categories": [ { "name": "...", "column": 0, "items": [ { "name", "description", "price" } ] } ],
  "logo_detected": true,
  "num_separators": 1,
  "num_columns": 2,
  "layout_notes": "..."
}
```

---

## Pipeline Flow (Image path)

```
Image → load_pages()
      → _process_side_image()  [Surya+SoM + Claude Vision parallel ensemble]
      → Strip Claude separators, inject OpenCV separators (detect_separators)
      → build_menu_data_from_claude()  → _menu_data.json
      → build_template_from_claude()   → _template.json
```

## Pipeline Flow (PDF path / fallback)

```
PDF  → extract_blocks_pdf()  [PyMuPDF vector text]
     → extract_separators_pdf() or detect_separators()
     → detect_columns(), classify_blocks()
     → build_menu_data()    → _menu_data.json
     → build_template()     → _template.json
```
