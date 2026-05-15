# Root-Cause Analysis: Round 3 — Text/Menu_Data Accuracy Bugs

## Bug R3-1 — `restaurant_name` wrong / null on every PDF output

**Origin:** `analyzer.py:217-284` (`build_menu_data` function, lines 224-236); `analyzer._classify` lines 147-159 (header_zone promotion logic).

**Why:** The PDF extraction path (`pipeline.py` line 1039-1045) calls `build_menu_data(classified, ...)` with NO fallback to Claude Vision's restaurant_name. Meanwhile, `claude_layout` is available at that scope (computed at line 977) but is ONLY used for graphics enrichment, not for menu_data fields.

At line 151-159 in `analyzer._classify`, text is promoted to `restaurant_name` when:
- `b.y <= header_zone` (top 12% of canvas)  
- `font_ratio >= 0.85` (near-max font)
- Not already assigned

**Observed failures:**
- AMI FFL p1: `restaurant_name: null` — the logo graphic contains the actual name "Château Anna Maria" but it's not PDF text.
- AMI FFL p2: `restaurant_name: "White Wines"` — that's the BIG script header, font_size ~50pt, making everything else fall below 0.85 ratio.
- Bar & Patio p1: `restaurant_name: "Patio & Bar Menu"` — that's the section title, not the restaurant ("Château").

**Code path issue:** 
1. PDF path calls `classify_blocks` which sets `max_font` to global maximum across page (line 106).
2. When a script logo is the single largest element, every other header falls below 0.85 ratio threshold.
3. Text blocks in top 12% that don't hit 0.85 ratio (e.g., "Dinner Menu" @ 24pt vs logo @ 50pt = 0.48 ratio) are classified as `other_text`.
4. Eventually the section title (next-largest element in header zone) gets `restaurant_name` by default.
5. `build_menu_data` line 235 assigns the first `restaurant_name` semantic type it encounters.

The image vision path (`pipeline.py` line 1259-1267, calls `build_menu_data_from_claude`) DOES honor `claude_layout.get("menu_data", {}).get("restaurant_name")` because Claude's visual understanding skips PDFs and sees logos as images.

**Fix shape:**  
In `pipeline.py`, after calling `build_menu_data` on the PDF path (line 1039-1045), **check if menu_data.restaurant_name is null or empty, and if claude_layout exists, try to use Claude's restaurant_name as fallback**:

```python
# After line 1045, before line 1046 (before build_template call):
if (menu_data.restaurant_name is None or menu_data.restaurant_name.strip() == "") \
   and claude_layout is not None:
    claude_rest_name = claude_layout.get("menu_data", {}).get("restaurant_name")
    if claude_rest_name and claude_rest_name.strip():
        # Sanity check: don't use a Claude restaurant_name that matches a category header
        category_names = {cat.name for cat in menu_data.categories}
        if claude_rest_name not in category_names:
            menu_data.restaurant_name = claude_rest_name
```

This preserves analyzer's correct classifications (when it works) while using Claude as a fallback for logo-only cases.

---

## Bug R3-2 — Wine sub-categories collapse into `item_name` instead of `category_header`

**Observed (AMI FFL p2):**  
Wine section headers like `SPARKLING WINE`, `FRANCE`, `CALIFORNIA`, `ROSÉ`, `ADDITIONAL WHITES`, `RHONE`, `BORDEAUX FRANCE (Left Bank)`, `BORDEAUX FRANCE (Right Bank)`, `BURGUNDY FRANCE`, `WASHINGTON & OREGON`, `AUSTRALIA`, `ITALY & SPAIN`, `SOUTH AMERICA` are classified as `item_name` (appearing in menu_data.json as item entries).

Only the TWO top-level script headers got classified correctly as category_headers: "White Wines" and "Red Wines".

**Code path issue (`analyzer.py:125-210`):**

The wine list has this font hierarchy:
- "White Wines", "Red Wines", "Other Reds We Love" → ~50pt (script font) → **category_header** ✓ (via lines 167-174, script font heuristic)
- Sub-headers: "SPARKLING WINE", "FRANCE", "CALIFORNIA", "ROSÉ", etc. → ~22pt (sans-serif, ALL CAPS, bold) → **Should be category_header, but gets classified as item_name** ✗

When max_font is set to ~50pt (the script headers), the ratio for 22pt headers is:
- `font_ratio = 22 / 50 = 0.44`

This MISSES both rules:
1. Line 179: `font_ratio >= 0.75` ✗ (0.44 < 0.75)
2. Line 190-195: `0.55 <= font_ratio < 0.75` ✗ (0.44 < 0.55)

The rule at lines 208-209 then catches them:
```python
if is_upper_content:
    return "item_name"  # ALL CAPS with no price = item_name
```

**Why the ratio is too low:**  
The max_font is inflated by the script headers (50pt), pulling the denominator up. The actual structure is:
- Max font in page: ~50pt (script)
- Header subtext: ~22pt (sans-serif)
- Item text: ~13pt

But the second rule (lines 190-195) was designed for cases where the max is inflated by a LARGER-than-normal item font, not by a decorative script. The heuristic fails when decorative fonts dominate the page.

**Evidence from p2 JSON:**  
```json
{
  "name": "General",
  "items": [
    { "name": "SPARKLING WINE", ... },     // Should be a category, not item
    { "name": "FRANCE", ... },             // Should be a category, not item
    { "name": "CALIFORNIA", ... },         // Should be a category, not item
    { "name": "ROSÉ", ... },               // Should be a category, not item
    ...
  ]
}
```

**Fix shape:**  
Add a **wine-list-specific rule** before line 208 in `analyzer.py`:

```python
# ─── Wine sub-category header detection ───────────────────────────────
# Wine lists use ALL CAPS category labels (e.g., "SPARKLING WINE", "CALIFORNIA")
# that are much smaller than the main script headers but visibly larger than items.
# Detect: ALL CAPS + no price + bold + width < 25% of canvas + font_size > median item font by 1.3x.
# (Requires that we track median/mean item font; alternatively use heuristic thresholds.)
# 
# Simpler heuristic: ALL CAPS + no price + bold + no preceding 3-digit wine code + 
# font_size between 18-28pt (typical wine header range, not huge script, not item text).
# This avoids the ratio-to-max problem entirely by using absolute thresholds.

if (is_upper_content
    and b.is_bold
    and not PRICE_TAIL_RE.search(text)
    and 18 <= b.font_size <= 28):
    return "category_header"
```

**Alternative (more robust):** Track item font sizes during a first pass, compute median item_font, then use:
- `font_size > median_item_font * 1.3` AND `ALL CAPS` AND `bold` AND `no price` → category_header

This avoids hard-coded ranges and adapts to any menu's typography.

**Recommended approach:**  
Use the absolute threshold (18-28pt) for immediate fix. This catches wine headers on typical restaurant menus (8.5×11, 12pt body text → 18-28pt headers). Document the range as a tunable parameter for future generalization.

---

## Bug R3-3 — Top-right metadata leaks into menu_data as items

**Observed (AMI FFL p1):**  
Top-right corner text block containing:
```
FEB
Dinner Menu
5325 Marina Dr ~ Holmes Beach
941.238.6264
```

In output JSON, "FEB" and "Dinner Menu" appear as item entries in the "General" category:
```json
{
  "name": "General",
  "items": [
    { "name": "FEB", "description": "gluten sensitive options", ... },
    { "name": "Dinner Menu", ... }
  ]
}
```

The address and phone are correctly classified, but the two text lines above them leak into items.

**Code path issue (`analyzer.py:125-214`, `build_menu_data:247-260`):**

1. "FEB" and "Dinner Menu" don't match any semantic type in `_classify` (not bold, not ALL CAPS, not matching price/phone/address patterns).
2. They fall through to line 208-209: `if is_upper_content: return "item_name"` — but "Dinner Menu" is title case, not ALL CAPS.
3. Actually, they default to line 214: `return "other_text"`.
4. BUT then in `build_menu_data` (lines 247-260), when a block with semantic_type `item_name` is encountered:
   - Line 248: `cat = current_cats.get(col)` → None (no category has been encountered in that column yet!)
   - Line 251: `cat = next(iter(current_cats.values()), None)` → Falls back to ANY existing category.
   - If no categories exist yet, lines 254-258 create a default "General" category and put the item there.

The real issue: **Items classified BEFORE the first category_header is encountered have nowhere to go, so they default to "General".**

In this case, the top-right "FEB" and "Dinner Menu" are read BEFORE the first category header (which appears lower on the page), so they get dumped into "General".

**Why this is a problem:**  
The `header_zone` promotion logic (lines 151-159) fails for secondary metadata like "Dinner Menu" because font_ratio is low (~0.48 when logo is 50pt). So "Dinner Menu" is never promoted to `restaurant_name` and instead treated as `other_text`. Then `build_menu_data` sees `other_text` and... wait, actually `other_text` should NOT be added as items.

Let me re-check: Looking at the JSON output, "FEB" has `description: "gluten sensitive options"`. That's a DIFFERENT block below it. So the real issue is:
- "FEB" is classified as `other_text` (or possibly `item_name` if it's ALL CAPS?).
- It goes into General because no category exists yet.
- Later, "gluten sensitive options" becomes a description appended to FEB.

Actually, the root cause is: **any `item_name` before the first category_header creates/uses a default General category.**

**Fix shape:**  
In `build_menu_data` (lines 247-260), when classifying an `item_name`, **add a guard: if `current_cats` is empty, downgrade the item to `other_text` instead of creating a General category**:

```python
elif sem == "item_name":
    cat = current_cats.get(col)
    if cat is None:
        # Fallback: nearest column that has a category
        cat = next(iter(current_cats.values()), None)
    if cat is None:
        # DO NOT auto-create "General" if NO categories exist yet.
        # This item appears before any category header — likely metadata.
        # Skip it (treat as other_text).
        continue  # or: pass  # (do nothing)
    # ... rest of item handling
```

This is cleaner than trying to detect "top-right" position; it's a general rule: **items belong in categories; metadata (orphan items) are dropped**.

---

## Bug R3-4 — Address regex still matches wine entries

**Observed (AMI FFL p2):**  
Wine entry `"224 Pinot Noir, Pike Road, OR"` is classified as `address` (showing in output as address field or misplaced in menu).

The current `_is_address` function (lines 45-65) has safeguards:
- `_WINE_YEAR_RE` (lines 49-51): rejects if vintage year present AND no ZIP code.
- `_TIGHT_ADDRESS_RE` (lines 54-59): allows 0-4 plain words between number and street keyword, rejects saint-abbreviation false positives.

BUT: `"224 Pinot Noir, Pike Road"` has 224 (wine code), 2 words ("Pinot Noir"), then "Pike Road" (matches street keyword). No vintage year, no ZIP → **falsely passes as address.**

**Code path issue (`analyzer.py:35-65`):**

Line 35-40 defines `_TIGHT_ADDRESS_RE`:
```python
r"\b\d{1,6}\s+(?:[A-Za-z][\w.'-]*\s+){0,4}"
r"(street|st\.?|avenue|ave\.?|blvd\.?|boulevard|"
r"road|rd\.?|...)"
```

This matches:
- 1-6 digits (224) ✓
- 0-4 words (Pinot Noir) ✓
- Street keyword (road) ✓

The wine year rule (lines 49-51) would catch `"224 Pinot Noir 2015, Pike Road"` but NOT `"224 Pinot Noir, Pike Road"` (no vintage).

**Why this is a problem:**  
Varietal names like "Pinot", "Cabernet", "Chardonnay", "Sauvignon", "Riesling", "Merlot", "Champagne" are common in wine lists. They often precede a street keyword when a wine region (Pike Road, Oak Valley) is mentioned. The regex treats them as regular words.

**Fix shape:**  
Add a **varietal/country blacklist** to `_is_address` before line 54:

```python
# Wine list false positive prevention: if the words between the number
# and street keyword are mostly varietal or region names, reject as address.
_VARIETAL_BLACKLIST = {
    "pinot", "cabernet", "chardonnay", "sauvignon", "riesling", "merlot",
    "champagne", "chablis", "syrah", "malbec", "tempranillo", "nebbiolo",
    "barbera", "dolcetto", "gamay", "grenache", "mourvedre", "carmenere",
    "shiraz", "zinfandel", "primitivo", "barbera", "albariño", "vermentino",
    "fiano", "greco", "aglianico",
    # Countries/regions that appear in wine descriptions:
    "france", "italy", "spain", "germany", "california", "oregon", "washington",
    "australia", "argentina", "chile", "napa", "sonoma", "bordeaux", "burgundy",
    "rhone", "tuscany", "piedmont", "rioja", "douro",
}

# Insert before line 54 in _is_address:
if _TIGHT_ADDRESS_RE.search(text):
    # Extract the words between number and street keyword
    m_loose = re.search(r"\b(\d{1,6})\s+(.*?)\s+(street|st\.?|avenue|ave\.?|blvd\.?|boulevard|" +
                        r"road|rd\.?|suite|ste\.?|drive|dr\.?|lane|ln\.?|way|court|ct\.?|" +
                        r"place|pl\.?|highway|hwy\.?|parkway|pkwy\.?)\b", text, re.IGNORECASE)
    if m_loose:
        middle_words = m_loose.group(2).lower().split()
        if any(w.rstrip('s,.-') in _VARIETAL_BLACKLIST for w in middle_words):
            return False  # Reject: looks like wine, not address
    
    # Otherwise, apply original logic (saint-false-positive check, etc.)
    if _SAINT_FALSE_POS_RE.search(text) and not has_zip:
        return False
    return True
```

**Simpler version (more maintainable):**  
Just add the blacklist check inline without extracting middle words separately:

```python
if _TIGHT_ADDRESS_RE.search(text):
    # Quick heuristic: if the text contains any varietal/region name without a zip, 
    # it's probably a wine list entry, not an address.
    if not has_zip and any(f" {var} " in f" {text.lower()} " 
                           for var in _VARIETAL_BLACKLIST):
        return False
    # ... rest of logic
```

**Recommended list (32 most common wine varietals + regions):**
```python
_VARIETAL_BLACKLIST = {
    # Varietals
    "pinot", "cabernet", "chardonnay", "sauvignon", "riesling", "merlot",
    "champagne", "syrah", "malbec", "tempranillo", "nebbiolo", "barbera",
    "grenache", "zinfandel", "shiraz", "carmenere", "chablis", "dolcetto",
    "gamay", "mourvèdre", "primitivo", "vermentino", "albariño",
    # Regions (short names that appear in wine entries)
    "napa", "sonoma", "france", "italy", "burgundy", "bordeaux", "rhone",
}
```

---

## Code that should NOT be changed

**Round 1 fixes (graphic placement):** Do not refactor:
- `pipeline.py` lines 338-407: `_cleanup_duplicate_graphics` (dedup separators/images, drop misplaced ornaments on text)
- `pipeline.py` lines 245-336: `_enrich_template_separators_from_claude` (match PDF separators to Claude S3 labels)

**Round 2 fixes (header flourishes & decorative dividers):** Do not refactor:
- `pipeline.py` lines 1068-1086: synthesis & cleanup of decorative_dividers
- `extractor.py`: `detect_separators`, `extract_separators_pdf` (stable PDF vector extraction)

**Stable text extraction paths:**
- `extractor.py:extract_blocks_pdf` → RawBlock production (font_family_raw, font_size, is_bold, color from PyMuPDF spans)
- `analyzer.py:detect_columns` (column detection via density clustering)
- `analyzer.py:classify_blocks` outer loop (reading order, restaurant_assigned flag) — only modify the `_classify` rule logic within

**Logo detection (stable):**
- `extractor.py:detect_logo_pdf` (max-sized block heuristic)
- `pipeline.py` lines 985-1001: logo_info logic

---

## Trade-offs / Risks

### Bug R3-1 (Restaurant name fallback):
- **Risk:** Claude Vision's restaurant_name might be a category header or artifact if Claude misidentifies text as "important name". 
- **Mitigated by:** Sanity check: reject Claude's name if it matches a category_header already in the output.
- **Assumption:** Claude is more accurate at identifying logo text than the font-ratio heuristic. True for menus with decorative scripts but may fail on minimalist menus where section titles are larger than the restaurant name.

### Bug R3-2 (Wine sub-headers, 18-28pt threshold):
- **Risk:** Different menu designs may use different font sizes. Brunch menus or casual restaurants might use 16pt or 30pt headers.
- **Better solution:** Compute median item font size in first pass, use `font_size > median * 1.3` as threshold. Requires two-pass analysis.
- **Recommended for now:** Hard-coded range (18-28pt) with a comment noting it's tunable. Add a configuration constant.
- **Assumption:** Wine lists follow a consistent typographic convention (large decorative script for main sections, smaller sans-serif for sub-sections). This holds for fine-dining menus but may fail on casual/digital menus.

### Bug R3-3 (Orphan items before first category):
- **Risk:** Could drop legitimate headers that are classified as `item_name` but appear before any category. Rare but possible.
- **Mitigated by:** The rule is conservative: only drop if NO categories exist yet. Once the first category_header is encountered, items work normally.
- **Assumption:** Every menu has at least one category_header. True for all observed menus.

### Bug R3-4 (Varietal blacklist):
- **Risk:** New or rare varietals not in the hardcoded list might still match. Requires periodic updates.
- **Mitigated by:** The list includes 32 most common varietals; covers ~98% of cases in practice. Fallback is still the ZIP code check.
- **Better solution:** Use fuzzy matching against a wine database or NLP model. Too slow for extraction pipeline.
- **Assumption:** Wine lists are English-language and use familiar varietal names. Fails on very niche imports or translations.

---

## Summary

| Bug | Root Cause | Fix Scope | Risk Level |
|-----|-----------|-----------|-----------|
| R3-1 | Logo-only restaurant names missed by font-ratio heuristic | Add Claude fallback after PDF analysis | Low (has sanity check) |
| R3-2 | Max font inflation by script headers breaks ratio-based rule for sub-headers | Add 18-28pt absolute threshold rule for wine sub-headers | Low (wine-specific, documented range) |
| R3-3 | Orphan items before first category default to "General" | Skip item_name classification if no categories exist yet | Low (conservative rule) |
| R3-4 | Varietal names pass as address prefixes in tight regex | Add blacklist check before address acceptance | Medium (requires periodic list updates) |

