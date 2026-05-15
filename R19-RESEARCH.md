# R19 RESEARCHER REPORT — Root Causes & Recommendations

> Output from Agent A (Root-Cause Researcher) — 2026-05-16. Used by Implementer (Agent B) and QA Verifier (Agent C).

---

## Issue 1: Script/cursive fonts not rendering on any page

**Root cause:** `builder.py:72` + `static/renderer.html:419-465` — Builder always emits `font_style:"italic"` when `block.font_family == "decorative-script"`, but the embedded `@font-face` for `BrittanySignatureRegular` has `font-style: normal`. The Canvas 2D `font-style: italic` request can't match the embedded face → faux-italics or fallback to `"Great Vibes", cursive` with italic-bold synthesis → bold-italic sans look.

**Probe results:**
- PyMuPDF emits `BrittanySignatureRegular`, size=30, italic flag NOT set.
- `_map_font_family` → `"decorative-script"`.
- Builder emits `font_style: italic` because family is decorative-script.
- Template JSON includes Brittany Signature base64 (6848 chars, valid TTF), `@font-face` registered with `font-style: normal`.
- Canvas requesting `italic ... BrittanySignatureRegular` doesn't match the normal face → fallback chain hits Great Vibes with faux italic+bold.

**Recommended change:**
1. `builder.py:72` — only emit `italic` when `block.is_italic` is true:
   ```python
   font_style="italic" if block.is_italic else "normal",
   ```
2. `static/renderer.html:452-456` — when family is signature/script, force `fontStyle = "normal"` regardless of JSON.
3. Optional: `font-style: oblique 0deg 14deg` in `@font-face` for signature fonts so italic requests match.

**Files to edit:** `builder.py`, `static/renderer.html`

**Golden-rule risks:** Low. Scoped to script/signature font families only.

---

## Issue 2: Bar & Patio circular Chateau logo as solid black circle

**Root cause:** Already resolved in current code. Latest `bar & Patio_p1_template.json` has a single `LogoElement` with valid `image_data` (65kb PNG with interior wordmarks intact). `_snapshots/bar & Patio_p1_template.png` shows the logo correctly. The user's observation was based on stale pre-R17 output.

**Recommended change:** No code change. Regenerate `latest_pdf_results/` so user sees current state.

---

## Issue 3: HAPPY HOUR sun-burst cropped on left despite R17

**Root cause:** `pipeline.py:1502` — `cluster_x1 = min(...) - 260` is too small. Captures "APPY" but cuts off the H. The sunburst wordmark extends ~350-400 px left of "DAILY"/"3-5PM".

**Probe results:**
- Stale `latest_pdf_results/bar & Patio_p1_template.json` was generated BEFORE R17 commit (no `img_hhbox_*` element). R17 source is present at `pipeline.py:1468-1536`.
- Re-ran pipeline: `[pipeline] R17: injected HAPPY HOUR box pixel crop (1069,1766) 433×161` — captured PNG shows `"APPY" 3-5 PM DAILY 20%off`, missing leading "H".

**Recommended change:**
1. `pipeline.py:1502` — bump left pad 260 → 420.
2. Bump `cluster_y1` pad 60 → 100 (sun-rays extend above).
3. Clamp: `cluster_x1 = max(canvas_w * 0.45, cluster_x1)` so crop doesn't bleed into left content.
4. Regenerate stale JSONs.

**Files to edit:** `pipeline.py:1502-1504`.

**Golden-rule risks:** Bumping pad could capture item-name text. Mitigation: clamp to right half of canvas.

---

## Issue 4: AMI FFL p1 As-Seen-On panel overlapping badges

**Root cause:** `pipeline.py:1039-1049 (R16)` slot math stacks all missing badges in a single column with only 5 px x-offset per index. Combined with Claude bboxes that have wrong aspect (e.g. `badge/youtube` at 216×90 stuffed into 90×90 slot), the panel pile-up renders as overlapping colored shapes.

**Probe results from `latest_pdf_results/AMI FFL DINNER MENU Combined (4)_p1_template.json`:**
- Panel `(50, 2111, 482, 212)` correct lower-left position.
- Inside: `food_network 130×130 at x=84`, `youtube 216×90 at x=211`, `best_of 130×130 at x=165`. All overlapping.
- Big gray Food Network + Diners' Choice on right at `1788, 2482` / `1788, 2890` — correct (R13 zone parking works).

**Recommended change:**
1. `pipeline.py:R16` — replace slot math with real row-layout:
   ```python
   total = len(missing) + len(present_inline)
   slot_w = panel_w / max(total, 1)
   for i, sl in enumerate(missing):
       slot_idx = len(present_inline) + i
       slot_x = panel_x + slot_idx * slot_w + slot_w * 0.1
       slot_y = panel_y + (panel_h - 80) / 2
       graphic_els.append({..., bbox: {"x": slot_x, "y": slot_y, "w": slot_w*0.8, "h": 80}})
   ```
2. Add a per-panel resolver that lays out ALL As-Seen-On badges in single row.
3. Force `bd["w"] = bd["h"] = 90` for `food_network`/`hulu`/`best_of`/`opentable_diners_choice` inside the panel; leave `badge/youtube` at S3 natural aspect.

**Files to edit:** `pipeline.py` (R16 block, lines 1033-1050 + new post-injection resolver pass).

**Golden-rule risks:** Helps rule 2 (no overlap). Distortion risk for YouTube logo if forced square — guarded by brand whitelist.

---

## Issue 5: AMI FFL p2 wine list — empty categories, vintages as prices, overlapping text

**Root cause:** Multiple analyzer bugs:

1. `analyzer.py:259-275` — wine category headers (`"SPARKLING WINE"`, font_size=36.8, max_font=67.1, ratio=0.548) just below the 0.55 secondary threshold. They become `item_name`.
2. 3-column layout splits one logical row across columns; price in col 2 doesn't know which item in col 1 it belongs to.
3. `PRICE_RE = r"^\$?\s?\d{1,4}(?:[./]\d{1,4})?(?:\.\d{2})?$"` matches `2017`/`2022`/`2023` as prices.
4. `item_description` OVERWRITE in `build_menu_data:343-347` — collapses 10 wine entries to last description only.
5. No `_looks_like_wine_entry` promotion to `item_name`.

**Probe results:**
- 192 blocks, 16 expected categories, ~5 detected.
- Column distribution: 56 / 98 / 38.
- `BORDEAUX, FRANCE (Left Bank)` has 8 items, item 3 `price='2017'` (vintage), items 4-8 description-only.

**Recommended change:**
1. Exclude wine vintages from `PRICE_RE` match: skip 4-digit numbers in 1900-2099.
2. Exclude script/signature fonts from `max_font` baseline so wine headers in Montserrat-SemiBold get fair ratio.
3. Promote wine entries to `item_name` when `_looks_like_wine_entry(text)` AND starts with 2-4 digit code.
4. Pair right-column prices to left-column items by y-proximity in `build_menu_data`.
5. APPEND descriptions instead of overwrite.

**Files to edit:** `analyzer.py` primarily; possibly `models.py` for `vintage` field.

**Golden-rule risks:** Data-only restructure. Visual rendering already uses font/position.

---

## Issue 6: Phantom diamond/dot decorators between rows

**Root cause:** `pipeline.py:615-699 _synthesize_header_flourishes` unconditionally injects `ornament/floral_swash_left` under EVERY `category_header`. The S3 asset has a prominent center diamond. In tight grids ("Add On"), only the diamond is visible → appears as phantom dot between rows.

**Probe results:**
- `latest_pdf_results/AMI BRUNCH 2022_template.json` has `ornament/floral_swash_left` at `(1075, 1942, 426, 64)` directly under "Add On" header at y=1870, slicing across the 4-column grid.
- `_cleanup_duplicate_graphics:463` exempts `ornament/floral_swash_*`, `calligraphic_rule`, `scroll_divider`, `diamond_rule`, `vine_separator` — too broad.

**Recommended change:**
1. Tag synth flourishes with `provenance: "r19_6_synth_header_flourish"`.
2. Only inject if Claude vision OR OpenCV blob saw an ornament near the header in source.
3. Per-header column-density check: skip if another `category_header` within 350 px below (small section).
4. Narrow cleanup exemption to provenance-tagged elements AND no body text within 30 px below.

**Files to edit:** `pipeline.py` (`_synthesize_header_flourishes` + `_cleanup_duplicate_graphics`).

**Golden-rule risks:** Fixes rule 1 violation. Regresses cases where unconditional inject was the only flourish present — but those were already non-faithful.

---

## Issue 7: menu_data first-line missing; footer URLs/dates as items

**Root cause:** Two analyzer bugs:

1. `analyzer.py:343-347 build_menu_data` — `item_description` OVERWRITES `cat.items[-1].description`. Multi-line PyMuPDF descriptions collapse to last line.
2. `analyzer.py:_classify` has no `_is_url` / `_is_footer` — `WWW.CHATEAURESTAURANTS.COM 941.238.6264`, `SARASOTA ~ ANNA MARIA ~ BOLTON LANDING`, `est. 2013` fall through to bold+upper → classified as `item_name`.

**Probe results from `AMI BRUNCH 2022_menu_data.json`:**
- `THE CHATEAU BREAKFAST` `desc='home fries, choice of toast'` (missing line 1 "two eggs cooked to your preference, bacon, sausage,").
- `LOBSTER FRITTATA` similar truncation.
- `Salads` category has bogus items: `WWW.CHATEAURESTAURANTS.COM 941.238.6264`, `SARASOTA ~ ANNA MARIA ~ BOLTON LANDING`.

**Recommended change:**
1. Add `_URL_RE`, `_ESTABLISHED_RE`, `_CITY_LIST_RE`, `_is_footer` in `analyzer.py`. Call BEFORE bold/upper classification → return `"other_text"`.
2. APPEND descriptions in `build_menu_data`:
   ```python
   combined = (last.description + " " + text).strip() if last.description else text
   cat.items[-1] = MenuItem(name=last.name, description=combined, price=last.price)
   ```
3. Y-window guard for description→item pairing (±200 px, same column).

**Files to edit:** `analyzer.py`.

**Golden-rule risks:** Pure menu_data structure change. No visual impact.

---

## Implementation priority order

| Rank | Issue | Reason |
|---|---|---|
| **1** | Issue 1 (script fonts) | Highest visible impact, 2-line fix, near-zero regression risk if scoped. |
| **2** | Issue 3 (HAPPY HOUR crop) | Quick fix (pad 260→420 + clamp). Explicit user complaint. Requires JSON regen. |
| **3** | Issue 6 (phantom decorators) | Violates Golden Rule 1 explicitly. Targeted: stop unconditional inject. |
| **4** | Issue 7 (descriptions + footer) | Data-only, low risk, prerequisite for Issue 5 wine fixes. |
| **5** | Issue 4 (As-Seen-On panel) | R16 row-layout + aspect-force. Moderate complexity. |
| **6** | Issue 5 (wine list) | Largest analyzer change — thresholds, vintage, column pairing. |
| **7** | Issue 2 (logo black circle) | Already resolved; regenerate latest_pdf_results/ to confirm. |

Issues 1, 3, 6 are surgical → ship together. Issues 4-5 share "Claude bbox isn't trustworthy" → bundle.
