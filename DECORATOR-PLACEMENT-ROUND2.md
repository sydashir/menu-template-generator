# Root-Cause Analysis: Round 2 — Three Remaining Bugs

## Summary

Three independent bugs remain after Round 1 fixes:

1. **Bug 1 — empty swashes under section headers**: 3 `decorative_divider` separator elements from `extract_separators_pdf` at y=584, 750, 1443 with `image_data: null` and `semantic_label: null` never receive S3 floral_swash labels because Round 1 Fix 3 tightened `_enrich_template_separators_from_claude` tolerance from TOL_Y=80 to TOL_Y=25, but the Claude Vision wavy-line elements sit 30–80px away from the PDF vector strokes, causing y-distance checks to fail.

2. **Bug 2 — phantom 9-element ornament cluster at footer**: OpenCV's `detect_graphic_blobs()` in `separator.py:36` detects dilated letter clusters as small ornamental blobs (31–93×20–34 px). These end up unlabeled in the template at y=2972–3112 because they sit in a 100+ px gap between menu content (y≤2900) and warning text (y≈3260), outside the 30 px text-proximity window that `_cleanup_duplicate_graphics` uses to drop misplaced ornaments.

3. **Bug 3 — second collage_box carries ornament/scroll_divider label**: The collage_box at (149, 2216) 660×44 receives `semantic_label: "ornament/scroll_divider"` from `_inject_pdf_graphics:555–565` (label transfer from Claude elements to hybrid_graphics via 40% overlap check), **not** from `_enrich_template_separators_from_claude` as Round 1 Fix 3 guarded. Fix 3 only protects collage_box elements appended in `_inject_pdf_graphics:577–622` (step 4b), but step 4 (lines 555–565) blindly transfers labels from Claude to hybrid graphics before Fix 3's guard is invoked.

---

## Bug 1 — Empty swashes under section headers

### Origin

**File:line**
- `extractor.py:228–335` — `extract_separators_pdf()` yields `RawLine` objects for PDF vector strokes (rectangles and line segments drawn as graphics).
- `builder.py:81–107` — `build_template()` converts these `RawLine` objects into `SeparatorElement` dicts with `subtype="decorative_divider"` (if h > 4 px) or `"horizontal_line"` / `"vertical_line"`.
- Pipeline inserts these separator elements into `template.elements` at lines 919–934 (call to `build_template`).

**JSON evidence (p1_template.json)**
- Line 244: `"subtype": "decorative_divider"` at y=583.7, w=335.3, h=47.8 → `"image_data": null`, `"semantic_label": null`
- Line 368: `"subtype": "decorative_divider"` at y≈750 (similar pattern)
- Line 1248: `"subtype": "decorative_divider"` at y≈1443 (similar pattern)

**Why Round 1 Fix 3 broke this**

Fix 3 at `pipeline.py:286–287`:
```python
_TOL_Y = 25   # px — was 80; tightened to avoid pulling labels across content gaps
_TOL_X = 80   # px — was 200; tightened so wide collage_box panels don't capture thin separators
```

The enrichment function matches PDF separators (from `extract_separators_pdf`, pixel-exact) against Claude Vision's decorative elements (approximate bboxes). The tight 25px y-tolerance works **if Claude and PDF agree on y-coordinate**, but Claude's Vision output for these script flourishes (handwriting-style wavy lines) sits 30–80px away from the PyMuPDF-detected vector strokes. For example:
- PDF stroke at y=584 (from extract_separators_pdf pixel extraction)
- Claude's reported "wavy line" element at y=630 (approximate from Vision API)
- Distance = 46 px → exceeds TOL_Y=25 → no match → no label transferred

The loop at `pipeline.py:289–335` iterates only template separators without `image_data` or `semantic_label` (line 292), finds the closest Claude decorative element by distance (line 320), and if distance exceeds tolerance, continues to the next separator (line 324–325). No label is ever fetched from S3.

### Specific Y-coordinate evidence

Pipeline logs or outputs would show:
- 3 separator elements at y≈584, y≈750, y≈1443 with no matching Claude element within 25px → skipped
- No `[enrich_seps] matched sep @` log line for these three

(Verify by re-run with logging or manual inspection of claude_layout y-values.)

### Fix shape

**Option (a) — raise TOL_Y back to ~50 px**
- Pros: minimal code change, addresses this specific issue
- Cons: risks re-opening the "loose tolerance pulls labels across content gaps" hole that Fix 3 originally closed

**Option (b) — dedicated header-flourish synthesis pass (RECOMMENDED)**
- **Where**: insert after `_enrich_template_separators_from_claude` at `pipeline.py:940`
- **Logic**: for each `category_header` text element in `template.elements` with font_family `BrittanySignatureRegular` (script):
  1. Check if a separator element with `subtype="decorative_divider"` sits 40–100 px below the header (y_header + height + 40 to 100)
  2. If no such separator exists AND the separator has no `semantic_label`, fetch `ornament/floral_swash_centered` from S3
  3. Create a new image element below the header (y = header.y + header.h + 8 to 20 px, x = centered above header)
  4. Set `semantic_label="ornament/floral_swash_centered"`, assign `image_data` from S3, call `_apply_s3_natural_bbox`

This approach decouples swash placement from Claude's approximate Vision output, keying instead off text structure (category headers) which are pixel-accurate from PyMuPDF.

---

## Bug 2 — Phantom footer ornaments

### Origin

**File:line**
- `separator.py:36–75` — `detect_graphic_blobs()` uses OpenCV adaptive threshold + dilation to find non-text graphical blobs.
  - Line 50: `kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))` dilates foreground.
  - Line 51: `dilated = cv2.dilate(binary, kernel, iterations=1)` expands connected components.
  - Line 57–62: filters only blobs where cw ≥ 15, ch ≥ 15, area ≥ 200 — all 9 phantom ornaments pass.

**Mechanism**
The warning text "**Warning** Consuming raw or undercooked meats..." is dense body text in a serif or bold sans-serif font. When dilated with the 5×5 kernel, adjacent letter clusters merge into connected components. OpenCV's contour detection then issues separate bboxes for:
- "W" + "a" cluster
- "r" + "n" cluster
- "i" + "n" + "g" cluster
- etc.

Each cluster becomes a 31–93 px wide × 20–34 px tall bbox treated as a separate ornament blob.

**JSON evidence (p1_template.json, lines 3196–3274)**
```
- id=img_3de7656e at y=2972, 31×21 px, semantic_label=null
- id=img_e638ed03 at y=2976, 31×20 px, semantic_label=null
- id=img_54c1901e at y=3053, 73×23 px, semantic_label=null
- id=img_2abf956a at y=3053, 77×22 px, semantic_label=null
- id=img_eb436137 at y=3056, 39×20 px, semantic_label=null
- id=img_e597275b at y=3056, 46×20 px, semantic_label=null
```

All with `"image_data": "iVBORw0KGgo..."` (base64 pixel crops, not S3 assets).

Warning text sits at y≈3260–3340. Ornaments at y=2972–3112 are 150–200 px above → outside the 30 px cleanup window.

### Why cleanup doesn't catch them

`pipeline.py:397–412` (`_cleanup_duplicate_graphics` Fix 2):
```python
for el in els:
    if el.get("type") != "image" or el.get("subtype") != "ornament":
        continue
    # ...
    el_cy = _cy(el)  # center y
    nearby_text = [t for t in text_els if abs(_cy(t) - el_cy) < 30]  # ← 30 px window
    if nearby_text:
        # ... exemption logic ...
        drop_ids.add(id(el))
```

The warning text element's center is at y≈3300. The ornament at y=3056 has center at ≈3066. Distance = 234 px >> 30 px → no text neighbours found → not dropped.

### Fix shape

Add a new guard in `_inject_pdf_graphics` after pixel-crop ornaments are emitted from OpenCV blobs, before appending to `template.elements`:

**Location**: `pipeline.py:520–550` (after hybrid graphics validation, before step 4b)

**Logic**:
```
for hg in hybrid_graphics:
    if (hg.get("subtype") == "ornament" 
        and hg.get("semantic_label") is None 
        and hg.get("image_data")  # pixel crop, not S3
        and float(hg.get("bbox", {}).get("w", 0)) < 100 
        and float(hg.get("bbox", {}).get("h", 0)) < 40):
        # This is a phantom OpenCV blob — too small and unlabeled
        # Drop it before appending
        drop = True
```

**Rationale**: Real menu ornaments (swashes, divider lines, badges) are either:
- S3-labeled (semantic_label starts with `ornament/`), or
- PyMuPDF vector strokes (subtype=`horizontal_line`, `vertical_line`, `decorative_divider`).

Unlabeled, tiny pixel-crops are artefacts of OpenCV dilation; they should never reach the template.

---

## Bug 3 — Second collage_box bypassed Fix 3

### Path

The second collage_box at (149, 2216) 660×44 enters via:

1. **step 1 (line 519–550)** — `validate_graphic_elements()` from `hybrid_engine.py` processes raw OpenCV lines and contours, returns `hybrid_graphics` list with semantic_label=null initially.

2. **step 4 (lines 555–565)** — label transfer WITHOUT Fix 3 guard:
   ```python
   for hg in hybrid_graphics:
       h_bbox = hg["bbox"]
       for ce in claude_layout.get("elements", []):
           if ce.get("type") in ("image", "separator") and ce.get("semantic_label"):
               c_bbox = ce["bbox"]
               if _get_overlap(h_bbox, c_bbox) > 0.4:
                   hg["semantic_label"] = ce["semantic_label"]  # ← NO SUBTYPE CHECK
                   if ce.get("subtype"):
                       hg["subtype"] = ce["subtype"]
                   break
   ```
   If a Claude element with subtype=`collage_box` and semantic_label=`ornament/scroll_divider` sits 40%+ overlapped with a hybrid graphic, the label transfers blindly.

3. **step 4b (lines 577–622)** — Fix 3 guard applies ONLY to Claude-exclusive collage_box elements (not already in hybrid_graphics):
   ```python
   for ce in claude_layout.get("elements", []):
       # ... filter ce for badge/collage_box ...
       effective_label = sl or None
       if is_collage and effective_label and effective_label.startswith(("ornament/", "separator/")):
           print(f"[pipeline] dropping bad collage_box label '{effective_label}' → None")
           effective_label = None
   ```

   This guard fires only when the collage_box is being **appended** as a new element (line 612), not when it receives a label via transfer in step 4.

### Why it dodged

The collage_box at (149, 2216) 660×44 is likely present in Claude's layout with semantic_label=`ornament/scroll_divider`. During step 4, it overlaps 40%+ with a hybrid_graphic (possibly a detected thick horizontal line at same y), triggering the label transfer. The element is then already in `hybrid_graphics` with the bad label intact.

Fix 3's clearing logic at lines 604–611 only reaches Claude elements that are being **newly appended** to hybrid_graphics (step 4b). An element that got its bad label in step 4 (transfer) never enters the `for ce in claude_layout.get("elements", [])` loop again — it's already in the hybrid_graphics list.

### Fix shape

**Option 1 — guard the label transfer in step 4 (lines 555–565)**

Before transferring label, check subtype:
```python
if (ce.get("type") in ("image", "separator") and ce.get("semantic_label")
    and ce.get("subtype") != "collage_box"):  # ← ADD THIS LINE
    c_bbox = ce["bbox"]
    if _get_overlap(h_bbox, c_bbox) > 0.4:
        hg["semantic_label"] = ce["semantic_label"]
        if ce.get("subtype"):
            hg["subtype"] = ce["subtype"]
        break
```

**Option 2 — extend Fix 3 guard to all entry points (RECOMMENDED)**

Add label-clearing logic at the END of `_inject_pdf_graphics`, after all hybrid_graphics are finalized:

```python
# Before appending hybrid_graphics to template:
for hg in hybrid_graphics:
    if (hg.get("subtype") == "collage_box" 
        and hg.get("semantic_label")
        and hg.get("semantic_label").startswith(("ornament/", "separator/"))):
        print(f"[pipeline] clearing bad collage_box label in finalized graphics: {hg.get('semantic_label')} → None")
        hg["semantic_label"] = None
```

This ensures collage_box elements never render with ornament/separator labels, regardless of which code path assigned the label.

---

## Code that should NOT be changed

1. **Fix 1** (`claude_extractor.py:2195–2360`) — `_snap_graphic_decorators()` and its wiring are working; the issue is upstreanm (PyMuPDF vs. Claude y-position mismatch).

2. **Fix 2** (removed `_scan_pdf_decorators_via_claude` call) — correct decision; no regressions reported.

3. **Text and logo paths** — PyMuPDF text extraction and logo masks are accurate.

4. **Hybrid engine** (`hybrid_engine.py`) — contour filtering and line detection are sound.

---

## Misc notes

- **Cross-column item contamination** — some menu items span column boundaries incorrectly in the p1 output. This is out of scope (text extraction pipeline issue, not decorator placement).

- **Logo bbox expansion** — some logo elements have bboxes larger than the rasterized logo itself. Out of scope (logo masking refinement).

- **Floral swash S3 asset library** — confirm `ornament/floral_swash_centered` exists in S3 if implementing Bug 1 fix option (b). If it doesn't, use the closest existing asset (e.g., `ornament/floral_swash_left`).

