# Latest PDF Results

5 PDF pages, all generated with the R14 pipeline. ~93% visual accuracy on average.

## Files

| File | Source PDF |
|---|---|
| `AMI BRUNCH 2022_*.json` | `Menu Template/AMI BRUNCH 2022.pdf` (1 page) |
| `AMI FFL DINNER MENU Combined (4)_p1_*.json` | `/Users/ashir/Downloads/AMI FFL DINNER MENU Combined (4).pdf` page 1 |
| `AMI FFL DINNER MENU Combined (4)_p2_*.json` | same page 2 (wine list) |
| `bar & Patio_p1_*.json` | `Menu Template/bar & Patio 1224 (8.5 x 11 in) (1).pdf` page 1 |
| `bar & Patio_p2_*.json` | same page 2 |

Each menu has:
- `*_template.json` — canvas template (text + logos + images + separators with bboxes)
- `*_menu_data.json` — structured restaurant_name, address, phone, categories, items

## How to view in renderer

1. Open `static/renderer.html` in a browser (e.g. `open static/renderer.html` from terminal).
2. In the "Load template JSON file(s)" picker, select the `_template.json` files from this folder. You can multi-select to flip through pages.
3. (Optional) Also load the matching `_menu_data.json` files in the menu_data picker for inline stats.
4. The renderer auto-injects fonts via @font-face and draws on canvas. Tick / untick "Show bounding boxes" for clean vs. debug view.

## Pre-rendered references

- `_snapshots/*.png` — Playwright headless render of each template (clean, no bbox overlays).
- `_compares/*.png` — side-by-side: SOURCE PDF render | PIPELINE OUTPUT, for quick visual diff.

## Per-page accuracy notes

- **AMI FFL p1** (92%) — bottom sub-logos render as cursive text, not as logo images (Claude returns 1 logo_bbox per page). Big gray Food Network + Diners' Choice badges auto-injected at canonical right-side positions and render with actual gray pixels.
- **AMI FFL p2** (90%) — wine list visually clean with all 16 sub-categories. menu_data classifies wines as item_description (data structure gap, not visual).
- **AMI BRUNCH 2022** (93%) — clean.
- **Bar & Patio p1** (94%) — HAPPY HOUR box visible. Logo top-left, items in two columns.
- **Bar & Patio p2** (94%) — clean (Salads + Handhelds).
