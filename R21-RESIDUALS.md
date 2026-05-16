# R21 Residuals — what's still <95% and why

Iter-6 weighted average: **88.66%**. Below the 93% stop-and-merge threshold; well below 95% per-page. Per the R21 prompt: "Realistic ceiling is ~92-93%; 95% requires real badge asset PNGs from a designer."

## Iter-6 per-page

| Page | text | logo | font | decor | sim | weighted | iter-5 | Δ |
|---|---|---|---|---|---|---|---|---|
| AMI BRUNCH 2022 | 92 | 85 | 95 | 92 | 90 | **90.65** | 90.65 | 0 |
| AMI FFL p1 | 85 | 88 | 92 | 85 | 88 | **87.45** | 83.0 | +4.45 |
| AMI FFL p2 (wine) | 90 | 70 | 92 | 82 | 88 | **84.0** | 82.8 | +1.2 |
| bar & Patio p1 | 90 | 92 | 92 | 88 | 90 | **90.6** | 88.65 | +1.95 |
| bar & Patio p2 | 90 | 92 | 92 | 88 | 90 | **90.6** | 88.65 | +1.95 |
| **Average** | 89.4 | 85.4 | 92.6 | 87.0 | 89.2 | **88.66** | 86.75 | **+1.91** |

## What R21 fixed (verified visually + via menu_data probes)

- **R21.1** As-Seen-On panel anchored to "As seen on:" caption Y (was at y=2194/ratio 0.658, now at the caption Y ratio 0.881). 3 brand badges in a row at bottom-left, no longer middle-of-page.
- **R21.2** HAPPY HOUR clamp 0.45→0.30. Bar & Patio p2 crop went 321×194 → **574×194** (78% wider), full wordmark captured.
- **R21.3** Heritage wordmarks for AMI FFL p1: "the Château ON THE LAKE" + "the Château ANNA MARIA" now pixel-cropped above each location line. No false-positive on AMI BRUNCH (startswith keyword match).
- **R21.5** Lowercase continuation lines ("peppers, microgreens" — 20 chars exactly) now classified as item_description. Catches 2nd-line description tails that were falling to other_text.
- **R21.6** Same-column right-aligned price pairing for food layouts (in addition to wine 3-col).
- **R21.4** Not needed — R21.1's anchoring already places badges directly above the existing show captions (SUMMER RUSH / FOY RUSH at y=3177/3223), no manual tuck required.

## Residuals (file:line, why hard, what was ruled out)

### 1. AMI FFL p1: 12 null-price food items (-3-5% on p1)

**Where:** `analyzer.py:413-456` cross-column price pairing pre-pass.
**What:** Food items like "GRAHAM CRACKER CALAMARI", "GRILLED LITTLE NECK CLAMS" have no price in `menu_data`. The 2-col food layout has price right-aligned in the SAME column as the name. R21.6 added same-column pairing with `Δx > 100`, but on this page the prices live in a SEPARATE classified block stream that doesn't reach the pre-pass — Claude vision detection grouped some price digits with the name in the template but the analyzer's classify_blocks operates on RawBlocks before that merge.
**Ruled out:** Reclassifying after Claude merge — would invalidate the column-detection state. Adding a third pairing strategy "look up the canvas for the nearest numeric block within ±200px x" — bleeds into adjacent items in dense layouts.
**Honest impact:** AMI FFL p1 → 87.5% is the ceiling without restructuring the column detection.

### 2. AMI FFL p1: Show captions "SUMMER RUSH" / "FOY RUSH" render as italic text, not as integrated badge units (-3-5% on p1)

**Where:** Source has each show's badge + caption + tagline stacked vertically as ONE visual unit. Pipeline keeps them as separate text elements.
**What:** The 3 brand badges (R21.1 row at y=2994+caption_h) sit above the show captions, but each badge isn't VISUALLY paired with its specific show. Source visually conveys "Beat Bobby Flay → food_network badge", "Summer Rush → hulu badge", "FOY RUSH → youtube badge". Pipeline shows 3 badges in a row, 2 captions below, no association.
**Ruled out:** Pixel-cropping the entire bottom-left As-Seen-On region (badges + captions + tagline) would overlap with R21.1's row layout — double-render. Separating each show into its own crop is feasible but each crop bbox must be detected manually from source pixel boundaries.
**Honest impact:** -5% on p1 logo score.

### 3. AMI FFL p2: 64/71 wine items have null description (-cosmetic data, no visual impact)

**Where:** `analyzer.py:build_menu_data` + R8.1 span merge in `extractor.py`.
**What:** Wine entries are SINGLE-LINE in source (e.g., "165 Intercept Crémant de Bourgogne"). No description text follows. So null `description` is CORRECT for the wine list, not a bug. The 7 wines that DO have descriptions are ones where Claude/PyMuPDF accidentally captured a producer note alongside.
**Honest impact:** None — `text` score 90 still reflects price coverage (0 null_price) and category structure (16 cats). The high null_desc count is structural to the source, not a regression.

### 4. AMI BRUNCH: 15 items have null description (-cosmetic)

**Where:** Same as #3. Add-On grid items (coffee/tea, pancake, orange juice, etc.) have no description in source — they're price-only menu items. Null is correct.
**Honest impact:** None.

### 5. AMI BRUNCH font fidelity: minor kerning/letter-spacing in Brittany Signature cursive (-1-2% on font score)

**Where:** Canvas 2D rendering of embedded TTF subset.
**What:** Cursive face renders correctly (R19.7 fix landed) but kerning is slightly tighter than source. The TTF subset embedded in template has 17 cmap entries (some characters are missing — e.g., 'O' uppercase not in subset for "Add On"). The browser falls back to "Great Vibes" for missing glyphs, causing minor visual drift.
**Ruled out:** Embedding the full TTF (uncapped subset) — increases JSON size 10× per template. Acceptable trade.
**Honest impact:** -2% on font score for AMI BRUNCH (capped at 95 not 100).

### 6. Bar & Patio p1/p2: "DAILY" + "3-5PM" leak into menu_data items (-cosmetic data, no visual)

**Where:** `analyzer.py:_classify` — short ALL-CAPS strings ("DAILY" = 5 chars, "3-5PM" = 5 chars with digit) classify as item_name via `is_upper_content`.
**What:** R19.9 correctly drops these text spans from `template.elements` (so they don't render twice with the HAPPY HOUR pixel crop), but `menu_data` is built from RawBlocks BEFORE R19.9 runs. Visual is clean; data has 2 ghost items per page.
**Ruled out:** Post-build menu_data scrub against R17 crop bbox — adds inter-pass coupling.
**Honest impact:** None visual. -1% on text score per page.

### 7. AMI FFL p1: As-Seen-On caption-to-badge association is positional, not pixel-perfect

**Where:** Pipeline can't VISUALLY connect each badge to its show caption.
**What:** Source has black thin "Beat Bobby Flay" / "SUMMER RUSH" / "FOY RUSH" text just below each respective badge. Pipeline renders captions independently. Even with R21.1 positioning, the captions are at y=3177+ (below the badges at y=3054) but not pixel-aligned per-badge column.
**Honest impact:** Slight visual gap on p1. Acceptable for current scope.

## Recommendation

Branch `r19-sprint` head: `<latest after R21 commit>`. Tag `r21-final-green` to be placed.

### Merge `r19-sprint` → `dev`?

**Yes, with caveats.** The 88.66% average is a real, honest improvement over the 69.2% honest pre-R19 baseline (+19.4 percentage points). All R19/R20/R21 commits are atomic and revert-able. No critical regressions; R21.3 false-positive was caught and fixed before final.

Caveat: if 95% is the strict business requirement, hold the merge until a designer provides real badge asset PNGs for the show-caption units (Beat Bobby Flay, Summer Rush, etc.). Real per-show PNGs would let R20.5's panel resolver layout them cleanly and bypass the pixel-crop hacks entirely.

### Realistic next round (R22)

- Wire each show caption to its corresponding badge via per-show pixel-crop (one crop per badge+caption+tagline unit), like R17 HAPPY HOUR but smaller. Estimate +3-5% on AMI FFL p1.
- Add fallback pairing for AMI FFL p1 food-section prices (Claude-merged price in same template block as item_name). Estimate +2-3% on p1.
- Source-asset acquisition: get clean PNGs for "as_seen_on/beat_bobby_flay" etc. into `local_assets/`. Then R20.1 row layout uses them directly. Estimate +5% on p1 logo score.

Combined R22 should land all 5 pages in the 92-95% range without pretending.
