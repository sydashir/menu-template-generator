# R7-D — Image-path backports

## Summary
The image-only branch (JPG/PNG menus) has received no backports from R3-R6. It uses `build_menu_data_from_claude` and skips the entire PDF post-processing stack. Per round-by-round audit:
- **Need to backport:** R5-A (3-column schema enum widen), R6-2 (defensive collage_box filter)
- **Already applies (no action):** R4-2, R5-B, R6-1, R6-4
- **Not needed:** R3-1, R3-3, R3-4, R4-1, R6-3 (Claude handles classification natively on the image path)

## Concrete changes needed

### R5-A — Widen `column` enum (3-column support)

**File:** `claude_extractor.py:239` (`_TOOL_SCHEMA.elements...text.column`)
```python
"column": {"type": "integer", "enum": [0, 1]}
```
→
```python
"column": {"type": "integer", "enum": [0, 1, 2]}
```

**File:** `claude_extractor.py:950` (`_HYBRID_TOOL_SCHEMA.ocr_labels...column`)
Same change.

**File:** `claude_extractor.py:945` (`_HYBRID_TOOL_SCHEMA.decorative_elements...column`)
Same change.

**Prompt update** in `_HYBRID_SYSTEM_PROMPT` and `_TOOL_SYSTEM_PROMPT`: change
> "column: 0 for left or single column, 1 for right column"

to
> "column: 0 for leftmost column, 1 for middle column on 3-col menus or right column on 2-col menus, 2 for rightmost column on 3-col menus only. On 2-column menus only use 0 and 1."

### R6-2 — Defensive empty collage_box filter on image branch

**File:** `pipeline.py` in the image-branch after `build_template_from_claude` call (~line 1496):
```python
# R6-2 (image-path defensive): filter empty collage_box
template.elements = [
    el for el in template.elements
    if not (
        el.get("type") == "image"
        and el.get("subtype") == "collage_box"
        and not el.get("image_data")
        and not el.get("semantic_label")
    )
]
```

## Not needed
| Fix | Reason |
|---|---|
| R3-1 (Claude name fallback) | Image path uses Claude's `menu_data.restaurant_name` directly; no analyzer to fall back from |
| R3-3 (orphan item drop) | `build_menu_data_from_claude` accepts Claude's structured categories; no orphan blocks |
| R3-4 (wine vocab blacklist) | `_is_address` is in analyzer.py; image path skips analyzer |
| R4-1 (Claude-validated reclassification) | Image path trusts Claude entirely; no analyzer output to reclassify |
| R6-3 (synth flourishes) | PDF-specific (depends on PyMuPDF pixel-accurate text positions). Could be refactored later but not blocking |

## Already-applied
| Fix | Where it lives | Confirmation |
|---|---|---|
| R4-2 cross-page name propagation | `pipeline.py:1555-1577` (outside if/elif/else) | Runs for all branches |
| R5-B `_is_generic_name` | `pipeline.py:54-76` | Used by R3-1 and R4-2; both reach all branches |
| R6-1 renderer fonts | `static/renderer.html` | Renderer-side; affects all branches equally |
| R6-4 multi-logo clustering | `claude_extractor.py:_enforce_single_logo` called at `pipeline.py:1490` (image branch) | Both branches call it |

## Risk assessment
- Widening `column` enum: zero risk; downstream code already uses `int`. Just opens a third option.
- R6-2 filter on image branch: zero risk; only drops elements that are both empty AND have no label, which means they have no value to render.
