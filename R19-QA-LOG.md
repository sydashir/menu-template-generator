# R19 + R20 QA LOG — All Iterations

Date: 2026-05-16
Weights: text 0.30, logo 0.25, font 0.20, decorator 0.15, similarity 0.10.

## Per-iteration weighted averages

| Iter | After | AMI BRUNCH | AMI FFL p1 | AMI FFL p2 | bar p1 | bar p2 | **Avg** |
|---|---|---|---|---|---|---|---|
| 0 | pre-R19 baseline (claimed) | 93 | 92 | 90 | 94 | 94 | 92.6 |
| 0 | pre-R19 baseline (honest, per QA visual) | 78 | 70 | 55 | 70 | 73 | 69.2 |
| 1 | R19.1–R19.7 | 87.4 | 71.6 | 67.7 | 71.6 | 73.7 | **74.4** |
| 2 | R19.8 + R19.9 | ~92 | 71.6 | 67.7 | ~85 | ~85 | ~80.2 |
| 3 (visual fresh) | iter-2 visual-confirmed | 90.05 | 75.4 | 76.5 | 87.05 | 87.05 | 83.2 |
| 4 | R20.1 + R20.2 + R20.3 | 90.05 | 81.45 | 79.1 | 87.05 | 87.05 | 84.95 |
| 5 | R20.4 + R20.5 (final) | **90.65** | **83.0** | **82.8** | **88.65** | **88.65** | **86.75** |

## Iter-5 final scores (axis breakdown)

| Page | Text | Logo | Font | Decor | Similar | Weighted |
|---|---|---|---|---|---|---|
| AMI BRUNCH 2022 | 92 | 85 | 95 | 92 | 90 | **90.65** |
| AMI FFL p1 | 82 | 78 | 92 | 82 | 82 | **83.0** |
| AMI FFL p2 (wine) | 87 | 70 | 92 | 82 | 85 | **82.8** |
| bar & Patio p1 | 88 | 88 | 92 | 87 | 88 | **88.65** |
| bar & Patio p2 | 88 | 88 | 92 | 87 | 88 | **88.65** |
| **Average** | 87.4 | 81.8 | 92.6 | 86.0 | 86.6 | **86.75** |

Total delta (honest baseline → iter-5): **+17.5%**. (74.4% claimed iter-1 → 86.75% iter-5: +12.4%.)

## Iter-1 detail (preserved below)

Reviewer: QA Verifier agent (claude opus 4.7) — iter 1.

| Page                  | Text | Logo | Font | Decor | Similar | Weighted |
|-----------------------|------|------|------|-------|---------|----------|
| AMI BRUNCH 2022       | 88   | 80   | 92   | 90    | 88      | **87.4** |
| AMI FFL p1            | 72   | 55   | 88   | 75    | 70      | **71.6** |
| AMI FFL p2 (wine)     | 55   | 70   | 80   | 80    | 72      | **67.7** |
| bar & Patio p1        | 60   | 82   | 85   | 70    | 72      | **71.6** |
| bar & Patio p2        | 65   | 80   | 85   | 72    | 74      | **73.7** |
| **Average**           | 68   | 73.4 | 86   | 77.4  | 75.2    | **74.4** |

## Per-page notes

### AMI BRUNCH 2022 — weighted 87.4%
**Wins**:
- Script section headers "Breakfast" / "Lunch" / "Salads" / "Add On" all render in cursive on the right side — R19.1 + R19.7 visibly landed.
- "Brunch" top-right corner script badge present (cursive face honored).
- "Add On" grid layout is clean: two columns of items, hairline separators preserved, no phantom diamonds slicing rows — R19.3 holds.
- Footer URL/address strip at bottom present and NOT promoted to item names. `grep WWW.` against menu_data → zero hits. R19.4 holds.
- THE CHATEAU BREAKFAST description complete: "two eggs cooked to your preference, bacon, sausage, home fries, choice of toast" — multi-line append landed.
- Top "The Chateau / ANNA MARIA" logo + the time-strip "8:00AM - 2:30PM" both reproduced.
**Gaps**:
- Several item descriptions truncated to a single line (CRAB CAKE BENEDICT, LOBSTER FRITTATA, FRENCH TOAST, CRÈME BRÛLÉE PANCAKES, GRILLED CHICKEN CAPRESE, TURKEY CLUB, LOBSTER MAC, CROQUE MONSIEUR, TURKEY BRIE, GRILLED PORTOBELLO) — first line captured, follow-on lines (e.g. "spinach", "grilled bread") dropped. Multi-line append fix only partially effective for these.
- B>Y>O OMELET — the `>` glyphs are OCR noise from "BYO"; descriptive ingredient list is mashed into one paragraph with the price-modifier sentence. Cosmetic but visible.
- "ADD TO ANY SALAD" item carries the **Warning** footer in its description field instead of being a true footer strip. Footer caught, but routed into a list item.
- "grilled chicken +9 , shrimp +12, fish of the day" appears as an item name with price "18".

### AMI FFL p1 — weighted 71.6%
**Wins**:
- Three-column structure preserved; "Sharable" / "Starters" / "Broths & Greens" / "Entrées" / "Sides" script headers all rendered.
- "Experience it!" top-left script wordmark reproduced.
- Center "Château / ANNA MARIA" logo rendered.
- "Dinner Menu" script header at top of right column present.
**Gaps**:
- As-Seen-On panel (bottom-left of source): source has 3 grayscale circular logo badges in a row (Food Network "Beat Bobby Flay", Bravo "Summer Rush", "FOX Rush"). Pipeline shows a **green-and-red color block** where the badges should be — R19.5 row layout did NOT land visually; the panel is mis-classified or its image refs broke. Hard miss.
- "Big gray badges" in the center (e.g. "Best of the Best", "Diner's Choice") are partially preserved as a single dark blob — recognizable but flattened.
- Items "Small 25 / Large 38", "Small MP / Large MP", "half dozen 18/ dozen 36" — these are price-line continuations of CHARCUTERIE / SEAFOOD TOWER / OYSTERS, promoted to standalone items. Wine-list-style "promotion" leaked into food section.
- "make it an entree, add pasta+15" treated as an item.
- "“SUMMER RUSH”" / "“FOY RUSH”" promoted to items in Starters (these are TV show captions next to badges).
- "anna maria island, FL" leaked into Sides as a price-line description on "1/2 LB 24 / 1 LB".

### AMI FFL p2 (wine) — weighted 67.7%
**Wins**:
- 16 categories detected (matches source section count: Sparkling, France, California Whites, Rosé, Additional Whites, Red Wines header, California Reds, Rhone, Bordeaux Left/Right Bank, Burgundy, Wash/OR, Other Reds, Australia, Italy/Spain, S. America) — section taxonomy is essentially complete.
- ROSÉ / BORDEAUX with diacritics preserved.
- "Red Wines" + "Other Reds We Love" headers present as section dividers (empty items expected as they are visual banners).
**Gaps**:
- **17 vintage-as-price hits remain** (e.g. 609 Frank Family Reserve → price="2021", 616 Joseph Phelps Insignia → "2022", every Bordeaux entry, every Burgundy entry). R19.6's vintage-no-longer-price rule is **partially landed** — California Cabs above index ~600 and the entire Bordeaux/Burgundy block still leak vintages into price.
- Many entries have `price: null` where source clearly has a price — cross-column pairing only resolved a subset (e.g. 100, 101 Chardonnay missing prices that exist in source).
- Item names sometimes truncated at trailing comma: "605A Château Les Pagodes de Cos, St. Estèphe,". The trailing-comma artifact suggests the price was peeled off into a vintage and the name kept the comma.

### bar & Patio p1 — weighted 71.6%
**Wins**:
- "Patio & Bar Menu" top-right script header rendered cursive.
- HAPPY HOUR sun-burst block (lower right of pizza column) clearly present on pipeline output with sun-rays radiating from the badge — **R19.2 visibly landed**.
- "Chateau / ANNA MARIA" logo top-center reproduced.
- Section script headers ("Starters", "Soup", "Pizza") in cursive.
**Gaps**:
- Item-vs-description splitting is mangled: every starter description is hoisted into a separate item entry. e.g. "bone in wings, celery, ranch" sits as its own item between CHATEAU WINGS and BEEF CARPACCIO. This is the structural issue from `column=1` overgrouping (all 3 source columns collapsed into one).
- Pizza section: "basil, olive oil ADD" and ": spicy soppressata +2" appear as orphan items.
- "DAILY" / "3-5PM" / "BAR menu" / "$7 select house wines" / "$5 draft beer" appear as standalone items in Pizza section — these are the HAPPY HOUR badge text bleeding into the item list (the badge ALSO renders correctly visually, so the same text is double-counted).
- "Truffle Fries 14/7" — the "7" looks like a typo from "$14 truffle / $7 plain" condensed.

### bar & Patio p2 — weighted 73.7%
**Wins**:
- Two clean columns (Salads + Handhelds) match source layout.
- HAPPY HOUR sun-burst again present bottom-left — R19.2 holds on p2.
- "Salads" + "Handhelds" script headers rendered cursive.
- "Served with fries" tagline preserved as opening item under Handhelds.
- "Warning— Consuming raw or undercooked..." footer routed into the BLACKENED MAHI SANDWICH description (correct content, wrong slot — should be footer, but at least not promoted to item).
**Gaps**:
- Same description-as-item leakage as p1: "romaine, shaved parmesan...", "iceberg, shaved red onion...", "blackened mahi, pepper jack cheese..." each appear as separate items.
- "KALE/ARUGULA/BEET SALAD SIDE 8/ENTREE 14" then "kale," then "baby arugula..." — the kale word fragment split off as its own item.
- "ADD CHICKEN +11, SHRIMP +15, FISH OF THE DAY 19" repeats 3 times as an item (once per salad) rather than as a salad-section footer.
- "DAILY" / "3-5PM" again appear as items in Salads (HAPPY HOUR badge text).

## Iteration delta
- Pre-R19 baseline (per `latest_pdf_results/README.md`): 92–94% claimed / 70–75% actual visual per user.
- Post-R19 average weighted score (this iteration): **74.4%**.
- AMI BRUNCH alone (the cleanest source) is now solidly at 87% — the cursive font fix gives a real visual lift.
- The multi-column / multi-row dinner & bar menus regressed in the **item-vs-description splitter** dimension — descriptions are routinely getting promoted to item names. This is the dominant residual error.

## R19 fix verification

| Fix | Evidence | Verdict |
|---|---|---|
| R19.1 — script fonts no italic in JSON | `AMI BRUNCH 2022_template.json` elements 0, 3, 4, 50 → `font_family=BrittanySignatureRegular`, `font_style=normal` (4/4 hits) | ✓ |
| R19.7 — cursive renders in canvas | AMI BRUNCH compare right side: Breakfast / Lunch / Salads / Add On all visibly cursive (no longer bold sans-serif) | ✓ |
| R19.2 — HAPPY HOUR includes sun-burst | bar & Patio p1 + p2 compare right sides: sun-burst rays clearly visible behind the HAPPY HOUR badge | ✓ |
| R19.3 — no phantom diamonds | AMI BRUNCH compare Add On grid: clean two-column item grid, no diamond decorators slicing rows | ✓ |
| R19.4 — desc multi-line + no footer-as-item | `grep -i WWW.` against `AMI BRUNCH 2022_menu_data.json` → 0 hits; THE CHATEAU BREAKFAST description is full multi-line; **Warning** footer routed into a description (caught, not promoted to item name) | ✓ (partial — see Gaps for items where 2nd description line dropped) |
| R19.5 — As-Seen-On panel non-overlapping | AMI FFL p1 compare: panel area shows a colored block, NOT three side-by-side badges | ✗ |
| R19.6 — wine list categories present | `AMI FFL DINNER MENU Combined (4)_p2_menu_data.json`: 16 categories detected (✓), but 17 vintage-as-price hits remain | ✓ categories / ✗ vintage rule |

## Regressions (if any)
- bar & Patio splits each ingredient line of every starter into a standalone item — this is worse than a typical "description truncation" failure. Suspected: column re-detection now flattens all 3 source columns into `column=1`, then the row-splitter promotes every line. Worth checking if R19.3 (ornament gating) or R19.4 (footer classification) inadvertently changed the row-splitting threshold.
- "DAILY / 3-5PM / $7 select house wines / $5 draft beer" badge text now appears as items in p1 Pizza and p2 Salads. The HAPPY HOUR badge is rendered correctly graphically but its underlying OCR text is also feeding the item extractor — the badge crop and the item extractor are double-counting.

## Convergence recommendation
- [ ] **STOP** — all 5 pages ≥ 95% weighted
- [x] **CONTINUE** to iteration 2 — residual issues:
  1. **Description-as-item promotion** on bar & Patio + FFL p1 (item-vs-description splitter is the biggest text-accuracy hit; pushes those pages from ~85% achievable down to ~72%).
  2. **HAPPY HOUR / As-Seen-On badge text double-counted** as items (badge renders graphically AND OCR text re-enters as items).
  3. **As-Seen-On row layout** on FFL p1 did not visually land — currently a colored block where 3 badges should be.
  4. **Vintage-as-price** still leaks for Bordeaux Left/Right Bank and Burgundy blocks on FFL p2; R19.6's rule needs to extend beyond the California cab section.
  5. AMI BRUNCH item descriptions occasionally still truncate to one line despite R19.4 — verify multi-line append actually loops over all visual lines (not just line 2).

Target for iteration 2: bring weighted average from 74.4% to 85%+, with AMI BRUNCH ≥ 95% and the dinner/bar pages ≥ 80%.
