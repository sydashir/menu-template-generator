# Latest PDF Results — Post-R19 Sprint

5 PDF pages from 3 source PDFs, all generated post-R19.9. **Honest accuracy ~85% PDF-wide** (up from ~70-75% actual pre-R19).

R19 was a multi-agent accuracy push — see `../R19-RESEARCH.md` (root causes), `../R19-QA-LOG.md` (scorecard), and `../R8-R11-ITERATION-LOG.md` (R19.0-R19.9 entries) for the full record.

## Files

| File | Source PDF |
|---|---|
| `AMI BRUNCH 2022_*.json` | `Menu Template/AMI BRUNCH 2022.pdf` (1 page) |
| `AMI FFL DINNER MENU Combined (4)_p1_*.json` | `Menu Template/AMI FFL DINNER MENU Combined (4).pdf` page 1 |
| `AMI FFL DINNER MENU Combined (4)_p2_*.json` | same page 2 (wine list) |
| `bar & Patio_p1_*.json` | `Menu Template/bar & Patio 1224 (8.5 x 11 in) (1).pdf` page 1 |
| `bar & Patio_p2_*.json` | same page 2 |

Each menu has:
- `*_template.json` — canvas template (text + logos + images + separators with bboxes)
- `*_menu_data.json` — structured restaurant_name, address, phone, categories, items

## How to view in renderer

1. Open `static/renderer.html` in a browser (`open static/renderer.html` from terminal).
2. In the "Load template JSON file(s)" picker, select the `_template.json` files from this folder. Multi-select to flip through pages.
3. (Optional) Also load the matching `_menu_data.json` files in the menu_data picker for inline stats.
4. The renderer auto-injects fonts via `@font-face` (R19.7 fixed the document.fonts.check fallback) and draws on canvas. Tick / untick "Show bounding boxes" for clean vs debug view.

## Pre-rendered references

- `_snapshots/*.png` — Playwright headless render of each template (clean, no bbox overlays).
- `_compares/*.png` — side-by-side: SOURCE PDF render | PIPELINE OUTPUT, for quick visual diff.

## Per-page accuracy notes (honest, weighted score per R19-QA-LOG)

| Page | Weighted | Wins | Remaining gaps |
|---|---|---|---|
| **AMI BRUNCH 2022** | **~92%** | Cursive headers (Breakfast/Lunch/Salads/Add On/Brunch); full multi-line descriptions; clean Add On grid; no footer-as-item; no phantom diamonds. | Some 2nd description lines on Lunch items drop occasionally. "ADD TO ANY SALAD" treated as item. |
| **bar & Patio p1** | **~85%** | Items + descriptions properly paired (was 20+ ghost items, now 9 real); HAPPY HOUR sun-burst visible; no DAILY/3-5PM ghost items; Best Of badge present. | Cross-column price pairing imperfect (some descriptions paired to wrong item); "ADD CHICKEN..." lines still classified as items. |
| **bar & Patio p2** | **~85%** | Same wins as p1; clean Salads + Handhelds grid; "Warning" footer routed to description. | Same residuals as p1. |
| **AMI FFL p1** | iter 2 in flight | Script headers in cursive (Sharable/Entrées/Sides/Starters/Broths & Greens/Experience it!); big gray Food Network + Diners' Choice badges in correct right-side position. | As-Seen-On panel still mis-renders as colored block (R19.5 layout math fixed; image_data path issue). Some food-section descriptions still promoted to items per pre-R19.8 outputs (will improve with iter 2 regen). |
| **AMI FFL p2** (wine) | iter 2 in flight | 16 wine categories detected; multi-column wine list visually intact. | Bordeaux/Burgundy vintages still leak into price field (~17 hits); some wine entries missing prices (cross-pairing partial). |

## Known content/asset gaps (not pipeline bugs)

1. **AMI FFL p1 small sub-logos** (Beat Bobby Flay, Summer Rush, FOX Rush): Claude returns 1 logo_bbox per page despite schema supporting multiple. Acknowledged.
2. **S3 asset gap**: library has scroll_divider / floral_swash; some sources use elaborate per-menu flourishes. Pipeline injects something close but not pixel-identical. Asset gap, not pipeline.
3. **menu_explanations.pdf** is a documentation PDF, not a menu. Out of scope.

## Suggested next round (R20) priorities

1. **R19.5 follow-up** — fix As-Seen-On panel image_data injection so the row-layout math actually shows 3 badges (not a colored block).
2. **R19.6 vintage-as-price extension** — probe why Bordeaux/Burgundy entries leak 4-digit vintages despite the `1900-2099` rule. Suspect span-merge concatenation.
3. **Span-grouping for descriptions** — `extractor.py` R8.1 occasionally drops the 2nd description line; investigate per-line grouping for Roman body type.
4. **`PRICE_TAIL_RE` guard for "add" lines** — `"ADD CHICKEN +11, SHRIMP +15, FISH OF THE DAY 19"` shouldn't be a standalone item; needs a "menu modifier" classifier.
