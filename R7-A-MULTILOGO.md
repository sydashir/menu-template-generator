# R7-A — Multi-logo support plan

## Summary
Change Claude tool schemas + system prompts to return ALL logo regions (primary + sub-logos) instead of just one. Each logo becomes its own LogoElement in the output. AMI FFL has 3 logos per page; current pipeline captures only 1.

## Schema changes

### `_HYBRID_TOOL_SCHEMA` (claude_extractor.py:~978)
Replace singular `logo_bbox` with plural `logo_bboxes`:
```json
"logo_bboxes": {
    "type": ["array", "null"],
    "description": "All distinct logo regions on the page (primary + sub-logos). Each bbox covers ONE branding element. Max 5.",
    "items": {
        "type": "object",
        "properties": {
            "x": {"type": "number"}, "y": {"type": "number"},
            "w": {"type": "number"}, "h": {"type": "number"}
        },
        "required": ["x", "y", "w", "h"]
    }
}
```

### `_TOOL_SCHEMA` (claude_extractor.py:~188)
The schema already supports multiple `type=logo` elements (anyOf), no schema change needed there — just prompt update.

## Prompt changes

### `_HYBRID_SYSTEM_PROMPT` STEP 5 (~line 909)
Replace:
> "Draw ONE unified bbox covering ONLY the PRIMARY restaurant name/branding block."

With:
> STEP 5 — LOGO BBOXES:
> Identify ALL distinct restaurant logo/branding regions on the page. Each bbox covers ONE location's name, wordmark, or emblem:
>   - PRIMARY: the main restaurant name (usually top-center, largest)
>   - SUB-LOGOS: secondary location names, chain branches, dual-branding (e.g. "ON THE LAKE", "ANNA MARIA")
>   - EXCLUDE: award badges, social icons, "As seen on" panels — those are graphic_elements
> Return up to 3 logo_bboxes per page.

### `_TOOL_SYSTEM_PROMPT` (~line 401)
Add:
> Include ALL logo regions if present. Each distinct location's name/branding becomes a separate type=logo element in the elements array.

## Downstream wiring

### `claude_extractor.py:extract_layout_surya_som` (~line 1690)
Replace the single-logo parsing block:
```python
lb = data.get("logo_bbox")
if isinstance(lb, dict) and lb.get("w", 0) > 0 and lb.get("h", 0) > 0:
    # ... single logo append ...
```
With:
```python
logo_bboxes = data.get("logo_bboxes") or []
# Backward-compat: if old schema returns single logo_bbox, wrap as list
single = data.get("logo_bbox")
if (not logo_bboxes) and isinstance(single, dict) and single.get("w", 0) > 0:
    logo_bboxes = [single]
for idx, lb in enumerate(logo_bboxes[:5]):
    if not isinstance(lb, dict) or lb.get("w", 0) <= 0:
        continue
    if scale_x != 1.0 or scale_y != 1.0:
        lb = {k: v*sx for k,v,sx in [...scale per-axis...]}
    _lb_cx = lb.get("x", 0) + lb.get("w", 0) / 2
    _lb_y = lb.get("y", 0)
    _py = "top" if _lb_y < orig_h*0.4 else ("middle" if _lb_y < orig_h*0.7 else "bottom")
    _px = "left" if _lb_cx < orig_w*0.35 else ("right" if _lb_cx > orig_w*0.65 else "center")
    elements.append({
        "type": "logo",
        "bbox": lb,
        "position_hint": f"{_py}_{_px}",
        "logo_index": idx,
    })
```

### `pipeline.py:_inject_pdf_graphics` (~line 730)
Replace single-logo crop block with iteration:
```python
logo_image_data_by_idx: dict[int, str] = {}
for el in graphic_els:
    if el.get("type") == "logo":
        idx = int(el.get("logo_index", 0))
        bd = el.get("bbox") or {}
        refined = _refine_logo_bbox_by_pixels(side_img, bd, canvas_w, canvas_h)
        if refined and refined["w"] > 20 and refined["h"] > 20:
            bd.update(refined)
        x1, y1 = max(0, int(bd.get("x", 0))), max(0, int(bd.get("y", 0)))
        x2 = min(canvas_w, int(bd.get("x", 0) + bd.get("w", 0)))
        y2 = min(canvas_h, int(bd.get("y", 0) + bd.get("h", 0)))
        if x2 > x1 and y2 > y1:
            crop = side_img.crop((x1, y1, x2, y2))
            buf = io.BytesIO()
            crop.convert("RGB").save(buf, format="PNG")
            logo_image_data_by_idx[idx] = base64.b64encode(buf.getvalue()).decode()
            print(f"[pipeline] PDF logo #{idx} cropped: {x2-x1}×{y2-y1}px")
```

Pass dict to builder: `logo_image_data_dict=logo_image_data_by_idx`.

### `builder.py:build_template_from_claude` (~line 159)
Add param `logo_image_data_dict: Optional[Dict[int, str]] = None`. In the `el_type == "logo"` branch:
```python
idx = int(raw_el.get("logo_index", 0))
img_data = (logo_image_data_dict or {}).get(idx) or logo_image_data
```

### `claude_extractor.py:_enforce_single_logo`
Preserve `logo_index` through clustering. After producing `merged_logo` dict, copy `logo_index = anchor.get("logo_index", 0)`.

## Edge cases

- 10 logos returned → cap at 5 via slice in extractor, then `_enforce_single_logo` clusters to ≤3
- 0 logos returned → empty array; no LogoElement emitted; no crash
- Single-logo menu → old `logo_bbox` field absent → list of length 1 from new field; still works
- Backwards compat: old saved templates with single `logo_bbox` → wrap as 1-item list before parsing

## Compatibility with R6-4 clustering
`_enforce_single_logo` rewritten in R6-4 already supports multi-region clustering by y-band. Add `logo_index` preservation, no other changes.
