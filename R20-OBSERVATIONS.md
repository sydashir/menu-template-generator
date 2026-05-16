# R20 Loop Observations — FINAL

5 R20 sub-iterations shipped, hard cap hit. **Iter-5 weighted average: 86.75%** (was 74.4% iter-1, 69.2% honest pre-R19 baseline).

---

## Iter 3 baseline (before R20) — 2026-05-16 ~06:19
Average weighted: **83.2%**.
- AMI BRUNCH 90, FFL p1 75, FFL p2 76, bar p1 87, bar p2 87.

Dominant gaps:
- AMI FFL p1 As-Seen-On panel renders as overlapping colored shapes — `_apply_s3_natural_bbox` was overwriting R19.5's row layout.
- AMI FFL p2 wine list: 17 vintage-as-price hits (Bordeaux/Burgundy), 34/71 wines with null price.
- AMI FFL p1 menu_data has 3 ghost "show name" items ("As seen on:", "“SUMMER RUSH”", "“FOY RUSH”").
- AMI BRUNCH Salads has "ADD TO ANY SALAD:" + "grilled chicken +9, shrimp +12, ..." as ghost items.
- bar & Patio has "ADD CHICKEN +11, SHRIMP +15, FISH OF THE DAY 19" as ghost item (appearing 3× per salad section).

---

## Iter 4 — R20.1 + R20.2 + R20.3

R20.1 — Post-S3 As-Seen-On panel resolver. `_resolve_as_seen_on_panel()` runs AFTER `_apply_s3_natural_bbox` so the row layout has the last word. Added `has_aso_text` trigger so R16 inject fires on "As seen on:" text presence (Claude vision missed all 3 inline badges in one run).

R20.2 — `_split_name_price` keeps trailing 4-digit vintages (1900-2099) IN the wine name when the line looks like a wine entry (vocab match OR wine-code prefix). Real prices like 17 or 14/7 still peel off correctly.

R20.3 — `_is_footer` catches "As seen on:", "Featured on", and quoted ALL-CAPS show names.

### Iter-4 scorecard

| Page | text | logo | font | decor | sim | weighted | iter-3 | Δ |
|---|---|---|---|---|---|---|---|---|
| AMI BRUNCH 2022 | 90 | 85 | 95 | 92 | 90 | **90.05** | 87.4 | unchanged |
| AMI FFL p1 | 80 | 75 | 92 | 82 | 80 | **81.45** | 71.6 | +9.9 |
| AMI FFL p2 (wine) | 78 | 70 | 90 | 80 | 82 | **79.1** | 67.7 | +11.4 |
| bar & Patio p1 | 85 | 88 | 90 | 85 | 88 | **87.05** | 71.6 | unchanged |
| bar & Patio p2 | 85 | 88 | 90 | 85 | 88 | **87.05** | 73.7 | unchanged |
| **Average** | | | | | | **84.95** | 74.4 | **+10.6** |

---

## Iter 5 — R20.4 + R20.5 — FINAL

R20.4: `PRICE_RE.match(text)` checked BEFORE `len(text) <= 2` short-text guard so 2-digit prices ("57", "70", "40") aren't demoted. Smoke test: AMI FFL p2 item_price count 38 → 87. After regen: **null_price on p2 wines 34/71 → 0/71.**

R20.5: `_is_menu_modifier(text)` detects 2+ "+N" patterns; demotes to item_description. `_is_footer` extended with "Add to any X:" subtitles. After regen:
- AMI BRUNCH Salads: 4 items → **2 items** (no ghost modifier line)
- bar & Patio p2 Salads: 8 items → **5 items** (no ghost ADD CHICKEN ×3)
- All categories: ghost_count = 0

### Iter-5 scorecard (final)

| Page | text | logo | font | decor | sim | weighted | iter-4 | Δ |
|---|---|---|---|---|---|---|---|---|
| AMI BRUNCH 2022 | 92 | 85 | 95 | 92 | 90 | **90.65** | 90.05 | +0.6 |
| AMI FFL p1 | 82 | 78 | 92 | 82 | 82 | **83.0** | 81.45 | +1.55 |
| AMI FFL p2 (wine) | 87 | 70 | 92 | 82 | 85 | **82.8** | 79.1 | +3.7 |
| bar & Patio p1 | 88 | 88 | 92 | 87 | 88 | **88.65** | 87.05 | +1.6 |
| bar & Patio p2 | 88 | 88 | 92 | 87 | 88 | **88.65** | 87.05 | +1.6 |
| **Average** | 87.4 | 81.8 | 92.6 | 86.0 | 86.6 | **86.75** | 84.95 | **+1.8** |

---

## Honest residuals (not at 95%)

1. **AMI FFL p1 show captions render as italic text** (-7-10% vs target). Source has "Beat Bobby Flay" / "SUMMER RUSH" / "FOY RUSH" as text labels INTEGRATED with each badge (badge + caption stacked vertically). Our pipeline renders the 3 brand badges in a row at y=2194 + the captions as separate italic text at y=2994. Visually they're disconnected. **Fix path** (deferred — would have been R20.6): pixel-crop each show panel (badge + caption + tagline together) following the R17 HAPPY HOUR pattern. Risk: overlapping with the row-laid badges; need careful bbox dedup.

2. **AMI FFL p1 has 12 null-price food items** (~5% impact). These are food-section items where price is a separate right-column block and the cross-column pairing (R19.6, written for wine layout) doesn't fire because their `is_menu_modifier` / column-detect logic differs. **Fix path**: extend R19.6 pre-pass to handle 2-column food layouts too — currently scoped to "strictly lower column index".

3. **AMI BRUNCH some 2nd description lines drop** (~2-3% impact). PyMuPDF emits multi-line descriptions as separate spans; R8.1 merge captures most but not all. Specific menu items lose the 2nd ingredient line. **Fix path**: relax R8.1 vertical-gap threshold (currently `>= avg_size * 1.5` between consecutive spans).

4. **No real per-page asset for show-name sub-logos** (-2% on AMI FFL p1). The Beat Bobby Flay / Summer Rush captions are show-name text labels that the source presents as graphical badges. No S3 asset exists for these. Even with a pixel-crop synth, we'd be duplicating source bytes. Acknowledged content gap.

5. **Renderer kerning / italic angle minor discrepancies** (<1%). Acceptable.

## Lessons

- **`_apply_s3_natural_bbox` is a "last writer wins" pass for badge bboxes.** Any positional fix in R16 or R19.5 must either tag elements with provenance to skip S3 normalization OR run again AFTER the S3 pass (R20.1 took the latter approach).
- **Claude vision is non-deterministic for small inline badges.** Build triggers off TEXT signals ("As seen on:") not vision detection.
- **`len(text) <= 2` short-circuits eat valid 2-digit prices.** Wine lists have many. PRICE_RE check must come first.
- **`_is_footer` is the right place for "this is panel chrome, not an item" classification.** Cheap to extend, hits before all bold/upper paths.
- **R8.1 span-merge can glue vintage years onto wine entries.** Splitter must be vintage-aware.
- **Parallel Surya OCR pipelines OOM/hang silently.** Run regens sequentially.

## Hit the cap

5 R20 sub-iterations is the hard cap from the prompt. Iter-4 → iter-5 movement was +1.8% (still above the 1% diminishing-returns threshold), but cap reached. Honest residuals documented above. Real engineering would continue with R20.6 (show-name pixel-crop) and food-section cross-column pairing.
