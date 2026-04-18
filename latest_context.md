# Latest Context — Menu Template Generator (dev branch)

**Date:** 2026-04-18
**Branch:** `dev`
**Status:** WIP — accuracy improving, remaining issues documented below

---

## What We Did This Session

### Problem
PDF extraction was highly accurate. Image extraction was broken:
- Duplicate content sections (ghost elements)
- Cursive section headers at wrong Y positions
- Logo bbox too small / wrong position
- False positive vertical separators (box borders detected as column dividers)
- Orange horizontal lines from decorative box edges
- Decorative boxes ("Wines by the glass", "Enjoy more") rendering as 4 separate lines

---

## All Code Changes Made

### 1. `main.py`
- Fixed `OUTPUT_DIR` to use absolute path: `Path(__file__).parent / "outputs"` with `mkdir(parents=True, exist_ok=True)`
- Prevents `FileNotFoundError` when uvicorn CWD differs from project root

### 2. `pipeline.py`
**`_run_image_ensemble`** — Removed parallel holistic pass entirely:
- OLD: Ran Surya+SoM AND Claude Vision tool_use in parallel via `ThreadPoolExecutor`, then `merge_layouts(precision, holistic, math_first=True)`
- NEW: Run ONLY Surya+SoM. If it fails → fall back to `extract_full_layout_via_tool_use` alone.
- **Root cause fixed:** Merging two Claude passes created ghost elements wherever the holistic pass returned decorative headers at wrong Y positions with IoU < 0.05 against any Surya block — all such elements survived the merge.

**`_CHUNK_THRESHOLD_H`** raised: `3000 → 5000`
- All menu images in folder are ≤ 2448px tall (no chunking needed)
- Chunking was creating duplicate content in the overlap zone

**Separator element building** — added `border` subtype handling:
- If `RawLine.subtype == "border"` → output as `{subtype: "border", bbox: full_rect_wh, stroke_width: 1.5}` so renderer draws a box outline
- Otherwise → existing horizontal/vertical line logic

**Removed** `extract_full_layout_via_claude` from imports (no longer used)

### 3. `claude_extractor.py`

**`_HYBRID_SYSTEM_PROMPT`** — rewritten:
- Emphasizes using OCR block pixel positions as spatial anchors for decorative elements
- "Anchor decorative element bboxes to nearby OCR block y-coordinates"
- "Do NOT invent decorative elements"

**`extract_layout_surya_som` — block list format:**
- Changed from percentage-based (`top:14%, left:8%`) to absolute pixel coords (`x=160 y=243 w=280 h=22`)
- Claude can now anchor cursive header bboxes precisely to known block positions

**`extract_layout_surya_som` — `user_msg`:**
- TASK 2: Explicit anchor method — find block immediately below the cursive header, set `y2 = that_block.y - 4`, `y1 = y2 - height`
- TASK 3: Logo must encompass ALL branding text + graphics as ONE bbox ("the", "CHÂTeau", "SARASOTA", swash lines = one logo)

**`_draw_som_annotations`** — semi-transparent SoM boxes:
- Changed from opaque outlines to RGBA alpha-composited fill (alpha=60/255 ≈ 24%)
- Cursive text under the boxes remains visible to Claude

**`extract_layout_surya_som` — dual-image prompting:**
- Sends TWO images: IMAGE 1 (clean original) + IMAGE 2 (annotated with SoM boxes)
- Claude reads text from IMAGE 1, uses IMAGE 2 only for OCR block identification
- Both images resized to same dimensions for consistent coordinate system

**`merge_layouts` — `math_first` fix:**
- Changed `if math_first: continue` to `if max_iou_check >= 0.05: continue`
- Holistic elements with IoU < 0.05 against Surya blocks now survive (catches missed cursive headers)
- (Note: this is now less critical since parallel holistic pass was removed)

**`_mask_logo_elements`** — expanded clearance zone:
- OLD: Only masked elements whose center was strictly inside logo bbox
- NEW: Expands masking zone by `min(lh*0.5, 70px)` below logo, `min(lw*0.25, 50px)` on sides
- Catches logo text fragments (e.g., "the" script) placed by Claude just outside the logo bbox

**`_snap_decorative_headers`** — NEW function:
- Post-processing after `extract_layout_surya_som` builds elements
- For each `decorative-script` text element (cursive section header):
  1. Finds the first Surya OCR block in the same column below it
  2. Snaps the header's bottom to `first_block.y - 4px`
  3. Caps height at 70px max
  4. Updates `font_size = h * 0.75`
- Completely deterministic — uses Surya's pixel-accurate positions, not Claude estimates
- Fixes "Course One/Two/Three" overlapping "choose one" text

### 4. `separator.py` — full rewrite

**New architecture:**
1. Detect horizontal lines (as before)
2. Detect vertical lines (as before, with edge filter for full-page box borders)
3. **NEW: `_merge_into_boxes`** — matches 2H + 2V lines into closed rectangles:
   - For each pair of vertical lines: check if 2 horizontal lines cap them at top+bottom
   - If yes → output as single `RawLine(subtype="border")` covering full bbox
   - If no → output as individual lines
4. Return boxes + remaining individual lines

**`_detect_direction` — edge filter (unchanged from before):**
- Vertical lines at x < 7% or x > 93% AND height > 82% of image → rejected (page border false positives)

### 5. `models.py`
- Added `subtype: Optional[str] = None` field to `RawLine`
- Allows `separator.py` to signal "border" boxes vs regular lines

---

## Remaining Issues (to fix when returning to dev)

1. **Logo bbox still too small** — "SARASOTA" text and decorative swash lines below it are outside the logo bbox. Logo prompt improvement partially helps but Claude consistently underestimates the logo extent. Need a programmatic post-processing step to extend the logo bbox downward to include all text within the logo's x-range.

2. **"Course One" missing** — In the latest run, "Course One" cursive header didn't appear (possibly masked by the expanded logo clearance zone — logo is at top-left, "Course One" starts below it in the same column). Need to tune the clearance zone or add a guard: don't mask elements below `logo_y2 + 30px` unless they're in the logo's x-band.

3. **Horizontal orange line at top** — A long horizontal line still appears at the top of the content area. This is the gold decorative rule at the top of the menu. The issue is it's being rendered as a thick orange stroke rather than a thin dark line. Possible causes: (a) OpenCV detecting it with h=20px causing `stroke_width=20`, (b) renderer mapping the line style to a theme color. Fix: clamp `stroke_width` to max 3px for horizontal lines in pipeline.py.

4. **"the" decorative script from "LE PREMIER MENU" header** — The large decorative "the" script that overlaps "LE PREMIER MENU" in the original is either being misplaced or masked. In the original it's at approximately x=360, y=105 (center-right area). Need to ensure Claude places it there, not in the logo's x-band.

5. **Text bounding boxes visually overlap** — Consecutive Surya OCR lines (item descriptions wrapping to multiple lines) have adjacent tight bboxes that the renderer draws with visible borders. These borders touching creates a "cluttered" look. Potential fix: merge adjacent same-subtype, same-column text blocks within 2px y-gap.

---

## Current Architecture (Image Path)

```
Image Upload (JPG/PNG)
  ↓
_process_side_image(side_img)
  ├── Upscale if height < 1600px (target 2400px)
  ├── Skip chunking (threshold now 5000px)
  └── _run_image_ensemble(upscaled_img)
        ├── extract_layout_surya_som(img)  ← PRIMARY
        │     ├── Surya OCR → pixel-accurate text blocks
        │     ├── Draw semi-transparent SoM annotations
        │     ├── Send clean image + annotated image to Claude
        │     │   (dual-image prompting)
        │     ├── Claude: label blocks + find decorative elements + logo
        │     ├── _snap_decorative_headers() ← NEW post-processing
        │     └── Return layout dict
        └── extract_full_layout_via_tool_use(img)  ← FALLBACK only
  ↓
Scale bboxes back to original dimensions (inv_scale)
  ↓
detect_separators(side_img)  ← OpenCV
  ├── H lines detection
  ├── V lines detection
  └── _merge_into_boxes() → border elements  ← NEW
  ↓
Replace all Claude separators with OpenCV results
  ↓
_mask_logo_elements() with expanded clearance  ← UPDATED
  ↓
build_template_from_claude() → template.json + menu_data.json
```

---

## Files Changed Summary

| File | What changed |
|------|-------------|
| `main.py` | Absolute OUTPUT_DIR |
| `pipeline.py` | No parallel ensemble, raised chunk threshold, border separator handling |
| `claude_extractor.py` | Improved prompts, dual-image, _snap_decorative_headers, expanded logo masking |
| `separator.py` | Full rewrite with rectangle/box detection |
| `models.py` | Added `subtype` field to `RawLine` |

---

## Image Sizes in Menu Template Folder

| File | Size | Notes |
|------|------|-------|
| `EARLY BIRD MENU  SRQ_2.jpg` | 1158×1500 | upscales to 1853×2400 |
| `Sarasota_Chateau_Dinner_menu.jpg` | 970×1500 | upscales to 1552×2400 |
| `AMI_brunch_Lunch_Menu.JPG` | 1215×2000 | no upscale |
| `valentines_day 23.png` | 1728×2304 | no upscale |
| `group menu_large_partymenu.png` | 1545×2000 | no upscale |
| `kidsthanksgiving_menu.JPG` | 1545×2000 | no upscale |
| `SRQ BRUNCH MENUS.png` | 910×1500 | upscales to 912×1500 |
| `AMI New Cocktail 11x17` | 3198×2448 (landscape) | no extension — pipeline rejects |
| `Front and back_DINNER Menu 11x17` | 2478×2016 (landscape) | no extension — pipeline rejects |

Files without extensions (`AMI New Cocktail 11x17`, `Front and back_DINNER Menu 11x17`) need to be renamed with `.jpg` before the pipeline will accept them.

---

## Server Setup (Azure VM)

- **VM:** Standard_B2als_v2
- **IP:** 20.187.152.110
- **Run command:** `uvicorn main:app --host 0.0.0.0 --port 8000` (NO `--reload` — causes mid-processing restarts)
- **Endpoint:** `POST /process` with multipart file upload

---

## Next Steps When Returning to dev

1. Fix logo bbox expansion (programmatic — extend bbox down to include all text in logo's x-range)
2. Fix "Course One" masking issue (tune clearance zone guard)
3. Clamp `stroke_width` to 3px max for horizontal separators
4. Fix "the" header script placement (should be at x≈360, not x≈160)
5. Test on all other images in Menu Template folder
6. Merge dev → main when accuracy targets met
