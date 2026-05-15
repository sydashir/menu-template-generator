# Root-Cause Analysis: Decorator Placement Bug

## Summary

Ornament and separator graphics are landing in wrong (x,y) positions—rendering over menu text content instead of in gaps between sections—due to three interacting failures: (1) Claude Vision's decorative_elements field provides **approximate y-coordinates** (off by 50–150px) that are NOT corrected during image processing; (2) the `_scan_pdf_decorators_via_claude()` function issues a second exhaustive decorator scan that re-detects graphics **after cleanup has already run**, injecting fresh misplaced ornaments anchored by loose 40px proximity checks rather than semantic validation; (3) collage_box subtype elements receive ornament semantic_labels due to label mismatch handling, causing 660×44 strip panels to be treated as generic ornaments and displayed via S3 asset stretching instead of their correct logical role.

**Top 3 Contributing Causes (by severity):**
1. **No y-correction applied to Claude Vision's decorative_elements** — approximate positions are passed directly to template without snapping to actual gaps.
2. **`_scan_pdf_decorators_via_claude()` re-injects unvalidated ornaments post-cleanup** — defeats the text-overlap filter with a weak 40px "anchoring" rule.
3. **Subtype/semantic_label mismatch in collage_box handling** — strips meant to be panels get ornament labels and S3 stretching, landing in wrong spatial context.

---

## Root Cause 1: Claude Vision Decorative Elements Use Approximate Y-Coordinates Never Snapped to Content Gaps

- **File:line** — `claude_extractor.py:1414–1750` (extract_layout_surya_som), specifically post-processing at lines 1638–1667.
- **What the code does** — Claude Vision returns `decorative_elements` (section headers, separators) with approximate bbox coordinates marked as "acceptable for section headers" (line 1421 docstring). The code scales these coordinates if the image was downsampled for API transmission (lines 1640–1644) but then passes them directly into the template element list without ANY spatial validation against the actual gap positions detected by Surya OCR.
- **Why it produces bad placement** — The system has Surya OCR blocks with **pixel-accurate coordinates** (line 1749: "Surya's OCR blocks are pixel-accurate"). The pipeline even implements `_snap_decorative_headers()` (line 2096) to fix Claude's imprecision by snapping decorative headers to 6px above the content block below them. However, this snapping applies ONLY to decorative text headers (`type="text", subtype="category_header"`), NOT to `type="image"` ornaments or separators added by Claude in the `graphic_elements` field. The graphic_elements array entries (lines 1712–1728) are copied directly with their approximate y-coordinates intact, bypassing the snapping logic entirely.

```python
# Line 1638-1667: decorative_elements added to elements list WITHOUT snapping
for dec in data.get("decorative_elements", []):
    # ... scaling ...
    el_dict = {
        "type": el_type,  # "separator" or "text"
        "bbox": {"x": float(bd.get("x", 0)), "y": float(bd.get("y", 0)),  # ← APPROXIMATE Y
                 "w": max(1.0, float(bd.get("w", 100))), "h": elem_h},
        # ...
    }
    elements.append(el_dict)  # ← NO snapping applied to this

# Line 2096: _snap_decorative_headers() later tries to fix text headers ONLY
for i, el in enumerate(result):
    if el.get("type") != "text":  # ← SKIPS image/separator types
        continue
    is_decorative = el.get("style", {}).get("font_family") in ("decorative-script", "display")
    is_header = el.get("subtype") == "category_header"
    if not (is_decorative or is_header):
        continue
```

- **Evidence in the JSON output** — In `outputs/AMI FFL DINNER MENU Combined (4)_v2/AMI FFL DINNER MENU Combined (4)_p1_template.json`:
  - Ornament cluster at **y=3053–3112** (6 elements) with `semantic_label: null` — these sit directly on top of warning text at **y=3312–3340** ("**Warning** Consuming raw or undercooked meats...").
  - Ornament at **y=2972–2993** — in the dense text band; nearby content includes "As seen on:" at y=2994.
  - These ornaments have no `semantic_label`, indicating they were NOT matched to S3 assets and are pure pixel crops of detected PDF or image regions—placed based on Claude's approximate coordinates.
- **Severity** — **High** — affects any ornament/separator detected by Claude in image mode (not just text headers).
- **Suggested fix shape** — Apply `_snap_decorative_headers()` logic (or a variant) to all decorator graphics (`type="image"`) returned by Claude, not just text headers. Or, for `graphic_elements` with no semantic_label, validate they sit in content gaps >40px before adding to template.

---

## Root Cause 2: `_scan_pdf_decorators_via_claude()` Re-Injects Misplaced Ornaments Post-Cleanup with Weak Deduplication

- **File:line** — `pipeline.py:118–241` (_scan_pdf_decorators_via_claude) and `pipeline.py:899–929` (call site and dedup logic).
- **What the code does** — After the main extraction pipeline and the `_cleanup_duplicate_graphics()` pass (which removes ornaments overlapping 2+ text elements), the code issues a **second dedicated API call to Claude** to exhaustively find all remaining graphics. It then attempts to inject these new decorators only if they sit within **40px of an existing non-text graphic** (separator/image/logo).

```python
# Line 899-904: second decorator scan AFTER cleanup
extra_decorators = _scan_pdf_decorators_via_claude(
    side_img, template.elements, canvas_w, canvas_h
)

# Line 918-929: weak dedup — only checks proximity to GRAPHIC elements
for dec in extra_decorators:
    # ...
    near_graphic = any(
        abs(cx - gc_x) < 40 and abs(cy - gc_y) < 40
        for gc_x, gc_y in _existing_graphic_centers  # ONLY separators, images, logos
    )
    if near_graphic:
        template.elements.append(dec)  # ← APPENDED without further validation
```

- **Why it produces bad placement** — The 40px proximity check is ANCHORED to existing graphics only, NOT to text content. If a prior extraction pass placed even a single ornament (or separator) at y=3000, the second scan can add 6 more ornaments at y=3050 (within 40px) without checking whether they sit on text. The `_cleanup_duplicate_graphics()` logic (line 362–375) filters ornaments that sit within 40px of 2+ text elements, but that logic runs BEFORE the second scan, so the new decorators bypass the text-overlap check entirely.

- **Evidence in the JSON output** — In `outputs/AMI FFL DINNER MENU Combined (4)_v2/AMI FFL DINNER MENU Combined (4)_p1_template.json`:
  - Total of **13 ornament image elements** with no semantic_label across y=545 to y=3112.
  - Six ornaments clustered at **y=3053–3112** (per-pixel y-ranges: 3053, 3053, 3056, 3056, 3056, 3079) — nearly identical y-positions suggest they were detected in the SECOND scan as distinct small blobs/pixels of a single visual feature (e.g., text warning strikethrough or pixel noise) and each treated as a separate ornament.
  - The warning text "**Warning** Consuming raw or undercooked meats..." is at y=3312, but these 6 ornaments are at y=3053–3112, visually rendering **directly above/over** that warning text in the rendered canvas.
  - Comment in code (line 324–326): "ornament/floral_swash_* images land inside dense text content because the 'already covered' guard in _inject_pdf_graphics only checks OpenCV blobs, not text."

- **Severity** — **High** — the second scan is **unconditional** and runs for every PDF page, reintroducing misplaced ornaments that cleanup has just removed.
- **Suggested fix shape** — Either (a) remove the `_scan_pdf_decorators_via_claude()` call entirely and rely on the main extract_layout_surya_som pipeline (which has snapping logic), OR (b) apply the same text-overlap validation after the second scan: drop any decorator whose vertical center sits within 40px of 2+ text elements, OR (c) change the "near_graphic" check to also require NO text elements within 40px.

---

## Root Cause 3: Collage_Box Elements Mislabeled as Ornaments; Subtype/Label Mismatch Causes S3 Stretching

- **File:line** — `pipeline.py:461–576` (_inject_pdf_graphics), specifically lines 540–576 where Claude-detected collage_box images are added.
- **What the code does** — The function adds Claude-detected images with `subtype="collage_box"` if they haven't already been covered by an OpenCV detection. These elements are meant to represent "As seen on" panels or logo collections. However, when the `semantic_label` is set to an ornament slug (e.g., `ornament/scroll_divider`), the downstream `_apply_s3_natural_bbox()` function (line 41–110) treats the bbox resize differently for ornament vs. collage_box, and the element is rendered as a small stretched S3 asset rather than a panel.

```python
# Line 567-576: collage_box added from Claude, may get ornament semantic_label
hybrid_graphics.append({
    "type": "image",
    "subtype": "collage_box" if is_collage else "badge",  # ← SUBTYPE is collage_box
    "semantic_label": sl or None,  # ← BUT semantic_label may be ornament/scroll_divider
    "bbox": {...},
})

# Line 91-100 in _apply_s3_natural_bbox():
elif sl.startswith("ornament/"):
    target_h = 70.0
    target_w = target_h * aspect
    # ← ornament logic applied to a collage_box element
```

- **Why it produces bad placement** — The `_enrich_template_separators_from_claude()` function (line 244–312) matches PDF vector separators to Claude-detected decorative elements using a y-tolerance of 80px and x-tolerance of 200px. When a match is found, it copies the semantic_label from the Claude element to the separator, even if the Claude element was originally a `collage_box`. This causes a 660×44 collage_box panel to inherit a `semantic_label="ornament/scroll_divider"`, which then gets processed as an ornament with the 70px target height rule, resulting in incorrect aspect ratio and y-positioning.

```python
# Line 275-276: loose tolerance
_TOL_Y = 80   # Claude's y estimate can be off by this much
_TOL_X = 200  # wide tolerance for x since widths differ

# Line 304-311: label is copied WITHOUT checking if source and dest subtypes agree
label = best["semantic_label"]
s3_bytes = resolve_asset(label)
if not s3_bytes:
    continue
el["semantic_label"] = label  # ← Ornament label → separator element
el["image_data"] = base64.b64encode(s3_bytes).decode()
_apply_s3_natural_bbox(el, s3_bytes, canvas_w, canvas_h)  # ← Applies ornament sizing
```

- **Evidence in the JSON output** — In `outputs/AMI FFL DINNER MENU Combined (4)_v2/AMI FFL DINNER MENU Combined (4)_p1_template.json`:
  - **2 collage_box elements** at y=2216 and y=2414, both with `semantic_label: "ornament/scroll_divider"` and dimensions 660×44.
  - These are NOT ornaments; they are "As seen on" or social-panel strips that should render as full-width panels. Instead, because they carry an ornament label, the renderer applies ornament sizing (70px height, capped width) and produces misaligned strips.
  - Original source: Claude detected these as `subtype: "collage_box"` (correct), but the enrichment pass found nearby separators and mistakenly copied the `ornament/scroll_divider` label, corrupting the element's semantic role.

- **Severity** — **Medium** — affects only elements where a collage_box happens to be within 80px y and 200px x of a PDF separator.
- **Suggested fix shape** — In `_enrich_template_separators_from_claude()`, only copy semantic_label from a Claude decorative element to a PDF separator if the source element's subtype matches the destination (e.g., only separator→separator, ornament→ornament, NOT collage_box→separator). Or, add a validation check: do not apply ornament-specific sizing to elements with `subtype="collage_box"`.

---

## Causes Worth Investigating But Unconfirmed

1. **Hybrid engine text-overlap threshold may be too permissive for separators** — `hybrid_engine.py:74` uses `threshold=0.05` for separators (vs. `threshold=0.15` for contours). This means a separator only needs to NOT overlap >5% of a text element to pass. For thin horizontal separators over dense body text, this could allow some edge cases. Recommend checking actual overlap ratios in failing cases.

2. **Claude's graphic_elements returned with large ambiguous bboxes** — Some ornaments in FFL have widths of 337–467px (lines 3–5 of ornament list), which seem large for decorative elements. These may be pixel crops of thick visual boundaries or watermarks. The `_cleanup_duplicate_graphics()` line 395 caps ornaments at 3% of canvas area, but canvas area estimate (line 385) uses max(x2, y2) which could overestimate. Worth auditing the canvas_area calculation.

3. **Tolerance values hardcoded without adaptive adjustment** — `_enrich_template_separators_from_claude()` uses fixed `_TOL_Y=80` and `_TOL_X=200`, which may be too loose for high-resolution menus (>1500px wide). No scaling relative to canvas dimensions.

---

## Code Paths That Are CORRECT and Should Not Be Touched

1. **PyMuPDF text extraction** (`extractor.py`, `pipeline.py:650–820`) — accurately extracts text bboxes and is the source of truth for content positions. NO bugs here.

2. **Surya OCR block detection** (`claude_extractor.py:1430–1432`) — pixel-accurate text block localization. Foundation for snapping logic.

3. **`_snap_decorative_headers()` for text headers** (`claude_extractor.py:2096–2187`) — correctly snaps cursive section headers to gaps. Logic is sound; issue is it only applies to `type="text"`, not graphics.

4. **`build_template_from_claude()` and template structure** (`builder.py`) — element JSON schema and rendering is correct. Issue is in the input data (positions), not the output format.

5. **Hybrid engine contour filtering** (`hybrid_engine.py:36–67`) — correctly excludes text-overlapping contours from ornament candidates. The issue is that `_scan_pdf_decorators_via_claude()` bypasses this logic with a second scan.

6. **S3 asset resolution and image_data encoding** (`s3_asset_library.py`) — correctly fetches and encodes PNGs. No issues.

7. **Logo extraction and masking** (`claude_extractor.py:2745–2800`, `pipeline.py:401–425`) — correctly handles logo-specific ornament deduplication.

