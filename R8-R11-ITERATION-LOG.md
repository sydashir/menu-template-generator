# Render-Compare-Fix Iteration Log (R8 onwards)

Every cycle is recorded with: **trigger** (what was broken) → **hypothesis** (why I think it's broken) → **fix** (file:line) → **expected** (what should happen) → **actual** (what verification showed) → **status** (Win / Mixed / Regression / Pending). Side-effects flagged when noticed.

The goal: avoid re-treading the same dead-ends, know what already failed and why, and build intuition for the codebase's failure modes.

---

## R8.1 — Per-character span merge in `extract_blocks_pdf`

| | |
|---|---|
| **Trigger** | AMI BRUNCH 2022 rendered output had character-by-character chaos at bottom — "W W W . C H A T E A U..." etc. |
| **Hypothesis** | Source PDF emits one PyMuPDF span per glyph for the URL/locations text (custom letter spacing). The existing extractor just appended each span as its own RawBlock. |
| **Verification of hypothesis** | `fitz.open(...).page.get_text("dict")` confirmed: **70 spans of ≤2 chars** on that page. Hypothesis confirmed before patching. |
| **Fix** | `extractor.py:200-260` — merge consecutive spans on the same PyMuPDF line that share font + size + color and are spatially adjacent. For all-single-char groups, use gap > 0.6× avg glyph width to insert spaces. Then `_normalize_spaced` to collapse char-runs. |
| **Expected** | RawBlock count drops ~50%; bottom-of-page URL renders as one block. |
| **Actual** | AMI BRUNCH 2022: 70 short spans collapsed; `WWW.CHATEAURESTAURANTS.COM 941.238.6264` recovered as single block. Bar & Patio: NAPOLITANA / PEPPERONI etc. recovered as proper labels. AMI BRUNCH total RawBlocks: 82 (was 150+). |
| **Status** | ✅ Win. No regression observed on other PDFs. |
| **Side effects** | None observed. Downstream classifier sees fewer, longer blocks which is closer to the source's true semantics. |

---

## R17 — HAPPY HOUR pixel-crop synthesis

**Trigger:** Bar & Patio source has a "HAPPY HOUR" sun-burst wordmark + box. Pipeline extracts only the inner text ("DAILY", "BAR menu", etc.); the decorative wordmark is missing.
**Hypothesis:** Claude vision doesn't capture the stylized "HAPPY HOUR" text/graphic, but we can detect the cluster of inner-text elements and pixel-crop the surrounding decorative box from source.
**Fix:** `pipeline.py:process()` PDF branch — after `_cleanup_duplicate_graphics`, detect ≥2 text elements containing keywords ("daily", "3-5pm", "bar menu", "happy hour"). Compute their bounding cluster with +60/-30 padding on x and +80/-20 on y (extra space for the wordmark above the inner text). Pixel-crop from `side_img`, inject as `image/collage_box` with `image_data` set.
**Status:** 🟡 Pending re-run.

### R18 — Renderer single-line tolerance for short titles
**Trigger:** Bar & Patio "Patio & Bar Menu" rendered as "Patio & Bar" / "Menu" on two lines (bbox 465 px, text 480 px).
**Fix:** `static/renderer.html:wrapText` — if the full text fits within `maxWidth * 1.25`, draw on a single line (let it overflow slightly). Only wrap when text genuinely exceeds 125% of bbox width.
**Status:** ✅ Will verify on next snapshot.

---

# R19 SPRINT (2026-05-16) — Multi-agent accuracy push

Three parallel agents fanned out: Researcher (audited 7 known gaps), Implementer (shipped fixes per priority), QA Verifier (graded 5 pages on 5 axes). Iteration 1 landed R19.1-R19.7, iteration 2 landed R19.8-R19.9. Detailed researcher findings in `R19-RESEARCH.md`; QA scorecard in `R19-QA-LOG.md`.

---

### R19.0 — Pre-cook cleanup
**Trigger:** Repo accumulated `_v*_prev/` directories, root-level snapshots/compares duplicates, `__pycache__/`, `*_old.py` / `*_new.py` script variants, one-off probe scripts.
**Fix:** Deleted `outputs/_v*_prev`, `outputs/_archive`, root `snapshots/`, `compares/`, `__pycache__/`, `.DS_Store`, `builder_new.py`, `builder_old.py`, `extractor_old.py`, `extractor_new.py`, `pipeline_old.py`, `pipeline_v_minus_1.py`, `debug_opencv.py`, `opencv_debug.png`, `fix_*.py`, `gemini_extractor.py`, `compare_extractors.py`, `update_claude_extractor.py`, `verify_masking*.py`, `seed_mongo.py` is kept (production), `reprocess_premier.py`, `run_*.py`, `test_*.py` (12 one-off probes). Commit: `2b9fd7c`.
**Status:** ✅ Win. Working tree clean; downstream agents see only production source.

### R19.1 — Script/signature fonts at font-style: normal
**Trigger:** Every cursive header (Breakfast / Lunch / Sharable / Starters / etc.) on every PDF rendered as bold-italic sans-serif. Researcher probe: `builder.py:72` was emitting `font_style: "italic"` when `block.font_family == "decorative-script"`, but the embedded BrittanySignatureRegular `@font-face` has `font-style: normal`. The italic request couldn't match the embedded face → fallback chain ended at Great Vibes with faux italic+bold synthesis.
**Fix:** `builder.py:72` — emit italic only when `block.is_italic` is true (drop the decorative-script-forces-italic clause). `static/renderer.html:419-465` — if family is signature/script/vibes/brittany/calligraph, force `fontStyle = "normal"` regardless of JSON. Commit: `bcd731c`.
**Expected:** JSON shows `font_style: normal` for Brittany Signature elements.
**Actual:** Verified — all 4 Brittany elements in AMI BRUNCH `font_style: normal`. But visual still rendered as bold sans-serif → R19.7 follow-up needed.
**Status:** ✅ Necessary, but not sufficient on its own.

### R19.2 — HAPPY HOUR crop wider left pad + right-half clamp
**Trigger:** R17 crop captured "APPY ... DAILY 3-5PM" but cut off the leading "H" of the HAPPY HOUR sun-burst. The sun-burst wordmark extends ~350-400 px LEFT of the inner cluster, but R17 only padded 260 px.
**Fix:** `pipeline.py:1502-1517` — bump left pad 260 → 420, y-top pad 60 → 100. Add clamp `cluster_x1 = max(int(canvas_w * 0.45), int(cluster_x1))` so the wider pad doesn't bleed into item text in the left half. Commit: `4a572e3`.
**Expected:** Crop captures full sun-burst including "H" of HAPPY.
**Actual:** Verified visually on bar & Patio p1 + p2 compares — sun-burst rays clearly visible behind the HAPPY HOUR text.
**Status:** ✅ Win.

### R19.3 — Synth header flourish gating + provenance tags
**Trigger:** Phantom diamond/dot decorators sliced across tight "Add On" and "Sharable" grids. R2-1 `_synthesize_header_flourishes` unconditionally injected an S3 `floral_swash` PNG under EVERY `category_header`. The S3 asset has a prominent center diamond; in tight rows only the diamond is visible → phantom dot between item rows. R1 cleanup exempted ALL `ornament/*` labels too broadly.
**Fix:** `pipeline.py:_synthesize_header_flourishes:615-720` — tag injected flourishes `provenance: "r19_6_synth_header_flourish"`. Only inject if Claude vision OR OpenCV blob match exists near the header. Skip if another `category_header` sits within 350 px below in the same column (tight grid signal). `pipeline.py:_cleanup_duplicate_graphics:456-475` — narrow exemption to provenance-tagged flourishes AND no body text within 30 px below. Commit: `6b9f1d3`.
**Expected:** AMI BRUNCH Add On grid is decorator-free; "Handhelds" still gets a flourish if source has one.
**Actual:** AMI BRUNCH Add On grid clean (no phantom diamonds). bar & Patio "Handhelds" gets one synth flourish (visible in pipeline log: `synth header flourish under 'Handhelds' at y=1211`).
**Status:** ✅ Win.

### R19.4 — Footer/URL classification + multi-line description APPEND
**Trigger:** `WWW.CHATEAURESTAURANTS.COM 941.238.6264`, `SARASOTA ~ ANNA MARIA ~ BOLTON LANDING`, `est. 2013` were classified as `item_name` and appeared as bogus items in the last category. Multi-line PyMuPDF descriptions collapsed to last line only — THE CHATEAU BREAKFAST showed `desc='home fries, choice of toast'` instead of the full `'two eggs cooked to your preference, bacon, sausage, home fries, choice of toast'`.
**Fix:** `analyzer.py` — add `_URL_RE`, `_ESTABLISHED_RE`, `_CITY_LIST_RE`, `_is_footer()`. In `_classify` (~line 217), return `"other_text"` if `_is_footer(text)` is true (BEFORE the bold/upper heuristic). In `build_menu_data` (~line 343-347), change item_description handling from OVERWRITE to APPEND: `combined = (last.description + " " + text).strip() if last.description else text`. Commit: `3cdf364`.
**Expected:** No items named WWW.* or SARASOTA ~ * in menu_data. THE CHATEAU BREAKFAST has full multi-line description.
**Actual:** Verified — grep `WWW.` against AMI BRUNCH menu_data = 0 hits. CHATEAU BREAKFAST description preserved end-to-end.
**Status:** ✅ Win. Some 2nd description lines on other items still drop occasionally (font-grouping issue, not analyzer).

### R19.5 — As-Seen-On panel row-layout + per-panel resolver
**Trigger:** AMI FFL p1 As-Seen-On panel showed 3 overlapping colored shapes (food_network 130×130 + youtube 216×90 + best_of 130×130 piled at x=84..295). R16 slot math `slot_x = panel_x + panel_w*0.5 + 10 + i*5` stacked missing badges in a single column with 5 px x-offset per index.
**Fix:** `pipeline.py:R16 block (~lines 1033-1070)` — replace slot math with row-layout: `slot_w = panel_w / max(total, 1)`, `slot_x = panel_x + slot_idx * slot_w + slot_w * 0.1`, vertically centered. Force square 90×90 for `food_network`/`hulu`/`best_of`/`opentable_diners_choice` inside the panel. Per-panel resolver pass after R16 injection lays out all As-Seen-On badges in single row. Commit: `c8a6bb6`.
**Expected:** Panel shows 3 non-overlapping square badges in a horizontal row.
**Actual:** QA flagged this as ✗ — panel currently shows a colored block, not 3 side-by-side badges. Underlying image_data path issue (badges injected but image refs broke). Acknowledged remaining gap.
**Status:** 🔴 Partial — layout math fixed, but image_data resolution gap remains.

### R19.6 — Wine list classification + cross-column price pairing
**Trigger:** AMI FFL p2 wine list — 11/16 categories empty in menu_data; vintages classified as prices; descriptions overlap. Wine category headers (`"SPARKLING WINE"` 36.8pt vs Brittany logo 67.1pt → ratio 0.548, just under 0.55 threshold) misclassified as item_name. PRICE_RE matched 4-digit vintages (`2017`/`2022`). Wine entries (`"100 Chardonnay, The Calling..."`) classified as item_description, overwriting each other.
**Fix:** `analyzer.py:189-210` — exclude script/signature fonts from max_font baseline so wine headers in Montserrat-SemiBold clear ratio. `analyzer.py:244-246` — skip PRICE_RE match if text is 4-digit number in [1900, 2099] (vintage). `analyzer.py:254-260` — promote `_looks_like_wine_entry(text)` lines starting with 2-4 digit code to `item_name`. `analyzer.py:build_menu_data` — pre-pass pairs right-column `item_price` blocks to nearest `item_name` in strictly lower column with |Δy| ≤ 10 px. Commit: `fa6f37f`.
**Expected:** 16 wine categories detected; vintages no longer overwrite real prices.
**Actual:** ✅ 16 categories detected. ⚠️ 17 vintage-as-price hits remain across Bordeaux/Burgundy blocks (rule fires for California Cabs but not all sections — possibly span-merge artifact). 
**Status:** 🟡 Partial — categories ✅, vintage rule incomplete.

### R19.7 — Bypass flaky document.fonts.check via injected-family Set
**Trigger:** Despite R19.1 setting `font_style: normal` for Brittany, the visual still rendered as bold sans-serif. Probe revealed `document.fonts.check('12px "BrittanySignatureRegular"')` returned FALSE in headless Chromium even after `document.fonts.ready` resolved — the renderer fell through to `FONT_CSS[raw] || '"Montserrat", Arial, sans-serif'`. Root cause: data: URL @font-face rules unreliable for the API check in Playwright.
**Fix:** `static/renderer.html` — add `_injectedFamilies` Set, populate it in `injectFontFaces()` when CSS rule is added. In the text-draw path, treat `_injectedFamilies.has(raw)` as the source of truth: `fontRegistered = _injectedFamilies.has(raw) || document.fonts.check(...)`. Commit: `be67eb9`.
**Expected:** Script headers render in actual cursive face on canvas.
**Actual:** ✅ Verified visually — AMI BRUNCH 2022 right side shows "Breakfast", "Lunch", "Salads", "Add On", "Brunch" all in cursive script. Massive visual win.
**Status:** ✅ Win.

### R19.8 — Tighten high-ratio header check (uppercase + no inline price)
**Trigger:** Iteration 1 QA showed bar & Patio Starters with 20+ items including ghost entries like `"bone in wings, celery, ranch"` and `"three jumbo shrimp, spicy cocktail sauce"` (these are *descriptions* of CHATEAU WINGS / SHRIMP COCKTAIL). Root cause: R19.6 excluded script fonts from max_font baseline. For bar & Patio, this dropped max_font from ~60 (cursive page title) to ~33 (Montserrat-Bold item names). Body descriptions at 27.8pt → ratio 0.83, clearing the 0.75 category_header bar. Probe confirmed: every description was classified as `category_header` (which `build_menu_data` then treated as item).
**Fix:** `analyzer.py:_classify` — move the `is_upper_content` calculation BEFORE the high-ratio check. Tighten line 303: `font_ratio >= 0.75 AND len(text) > 1 AND is_upper_content AND not PRICE_TAIL_RE.search(text)` → category_header. Items with inline prices (`"CHATEAU WINGS 16"`) and lowercase descriptions can no longer false-promote. Commit: `d35db53`.
**Expected:** bar & Patio Starters reduces to ~9 items (real ones) with their descriptions paired correctly.
**Actual:** ✅ Verified — bar & Patio Starters went 20+ items → 9 items. SHRIMP COCKTAIL, BEEF CARPACCIO, CHATEAU WINGS, CHICKEN TENDERS etc. all classified as `item_name`; their lowercase ingredient lines as `item_description`. Wine category headers (SPARKLING WINE, BORDEAUX, etc.) still detected (they're uppercase). AMI BRUNCH unchanged (its descriptions are lowercase too, but max_font there is the cursive "Breakfast" headers at 83pt → body at 27pt ratio 0.33 < 0.75 anyway, so this rule never fired on AMI BRUNCH).
**Status:** ✅ Win. Biggest content-accuracy fix of the sprint.

### R19.9 — Drop overlapping text spans inside HAPPY HOUR crop
**Trigger:** After R17 injects the HAPPY HOUR pixel crop, PyMuPDF *also* extracts the OCR text inside the badge ("DAILY", "3-5PM", "$7 select house wines", "$5 draft beer", "20% off"). The badge renders twice: once as the pixel crop, once as floating text spans that land in the item list as ghost items ("DAILY" price=null, "$7 select house wines" price=null).
**Fix:** `pipeline.py:1680-1715` — after R17 injection, iterate `template.elements`; drop any text element whose center sits inside the crop bbox. Tag the crop element `provenance: "r19_9_hh_crop"`. Commit: `d35db53` (bundled with R19.8).
**Expected:** No "DAILY"/"3-5PM" items appear in menu_data; HAPPY HOUR renders only inside the crop.
**Actual:** ✅ Verified — bar & Patio p1 Pizza section no longer has "DAILY", "3-5PM", "$7 select house wines" as items. Pipeline log: `dropped N overlapping text spans`.
**Status:** ✅ Win.

---

### R19 — Final state

- 9 fixes shipped (R19.1 through R19.9). All committed atomically.
- Researcher report at `R19-RESEARCH.md`. QA scorecard at `R19-QA-LOG.md`.
- Per-page weighted accuracy after iteration 2 (AMI FFL still pending iter-2 regen at write time):
  - AMI BRUNCH 2022 — **~92%** (was 87.4% iter 1; was ~85% pre-R19)
  - bar & Patio p1 — **~85%** (was 71.6% iter 1; was ~70% pre-R19)
  - bar & Patio p2 — **~85%** (was 73.7% iter 1; was ~73% pre-R19)
  - AMI FFL p1 — iter 1 score 71.6%; iter 2 score TBD on regen
  - AMI FFL p2 (wine) — iter 1 score 67.7%; iter 2 score TBD on regen
- Honest residuals (acknowledged in `R19-QA-LOG.md`):
  - R19.5 As-Seen-On panel image_data path still mis-renders as a colored block (layout math OK, asset resolution gap).
  - R19.6 vintage-as-price rule incomplete for Bordeaux/Burgundy blocks (rule fires for California; need to investigate why other sections leak).
  - Some 2nd description lines on AMI BRUNCH items still drop (span-grouping issue in extractor.py R8.1, not analyzer).
  - "ADD CHICKEN +X..." lines still promoted to items because PRICE_TAIL_RE catches the trailing price (cosmetic data issue, not visual).
