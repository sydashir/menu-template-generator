## Verdict per fix

- **R2-1: PASS** — `pipeline.py:514-594` defines `_synthesize_header_flourishes()`. It is invoked from `process()` at `pipeline.py:1072`, sitting between `_enrich_template_separators_from_claude` (line 1067) and `_cleanup_duplicate_graphics` (line 1086), exactly as required. The unlabelled-`decorative_divider` drop at `pipeline.py:1076-1084` runs immediately before the cleanup. The drop predicate is specific to `subtype == "decorative_divider"` so `horizontal_line` / `vertical_line` vector dividers are untouched (`pipeline.py:1079-1083`). Per-header dedup (line 547-555) tests `abs(cy - target) < 40` AND `abs(cx - target) < hw`. Centered vs. left swash slug selection (line 559-560) reads `style.text_align` exactly as spec'd. Size: `target_h=60` capped by `target_w <= hw*2.0` (line 574-577). Fallback when S3 returns nothing: `if not png: continue` (line 562-563). Call site is inside the `if claude_layout is not None:` block of the PDF-hybrid branch (line 1064-1065), so the synth does NOT fire on the image-vision branch (which begins at line 1099 `elif claude_layout is not None:`).

- **R2-2: PASS (with caveat)** — Tiny-ornament guard appears in both required locations: inside `_inject_pdf_graphics` at `pipeline.py:865-877` (rebuilds `graphic_els` with the predicate negated) and inside `_cleanup_duplicate_graphics` at `pipeline.py:414-428`. Predicate is exactly `type=image AND subtype=ornament AND no semantic_label AND w<100 AND h<40` in both spots. Synth flourishes carry `semantic_label="ornament/floral_swash_*"` so they bypass `not el.get("semantic_label")` → safe. S3-labelled small badges have `subtype=="badge"`, not `"ornament"` (see `pipeline.py:713`) → safe. Caveat: the `_inject_pdf_graphics` filter rebuilds the list object, which is fine here because `graphic_els` is re-bound only locally; nothing else holds a reference.

- **R2-3: PASS** — Final scrub at `pipeline.py:900-913`. Runs AFTER `_enforce_single_logo` (line 898) and BEFORE `build_template_from_claude` (line 915), so all hybrid manipulation is done. Predicate is `subtype=="collage_box"` AND `semantic_label` starts with `ornament/` or `separator/`. Clears `semantic_label` and conditionally clears `image_data` (only when there was a bad `sl` and image_data was present). Collage boxes with `None`/empty label or `badge/*` label are not disturbed (predicate filter at line 905). The earlier guard at the injection point (`pipeline.py:707-710`) prevents the bad label from ever being set on a freshly-injected collage_box, so this final scrub is the safety net for the older overlap-based label-transfer paths (e.g. `_enrich_template_separators_from_claude` doesn't touch collage_box, but other earlier passes could).

## Regression risks

- **Synth flourish dropped by `_cleanup_duplicate_graphics` Fix 2 when an item_name sits within 30 px below the swash.** The synth bbox top is `header_y + header_h + 14` and mid-y is `top + ~21`. If header_h is small (e.g. 50 px) and the first item_name follows tightly, the body-text neighbour test (`pipeline.py:398-412`) will see a non-header neighbour and drop the swash. In the real `p1_template.json` the spacing is comfortable (header h=98.5, item starts at y=641 vs synth mid ~575 → ~66 px), so this is not triggering today. Verify: re-run on the FFL PDF and confirm synth flourish IDs (`img_synth_swash_*`) appear in the final p1/p2 JSONs.

- **Synth flourish horizontal centering for left-aligned headers may overlap adjacent columns.** Slug for `text_align == "left"` is `floral_swash_left`, but bbox center is still computed as `flourish_cx = hx + hw / 2` (`pipeline.py:545`). For a left-aligned header at the column edge, the swash will be centered on the header text rather than left-aligned with it; visually it can creep right of the header. Verify by inspecting render output near a left-aligned header.

- **`_synthesize_header_flourishes` will re-run on every PDF page and append duplicates if `process()` is somehow re-entered with the same template object.** Today this is not the case — `process` builds a fresh template per side — but the dedup guard uses only existing image elements at function entry (line 533), not the appended new ones, so an in-function double-add for two headers very close together is not possible (no two headers share the same x/y).

- **Tiny-ornament guard could mis-drop a legitimate small badge re-labelled with subtype=ornament.** Today badges are assigned `subtype=="badge"` (`pipeline.py:713`) so the guard's `subtype=="ornament"` filter protects them. If a future code path mislabels a small badge as `subtype=ornament` with no `semantic_label`, it would be silently dropped. Low risk; no remediation needed now.

- **Decorative_divider drop relies on `image_data is None AND semantic_label is None`.** A `decorative_divider` that did receive a `style.color`/`stroke_width` but no image_data is still dropped (e.g. real `horizontal_line` data structures use `subtype="horizontal_line"`, so they're not affected). The risk is purely against `decorative_divider` rows that were intended to render as styled rectangles — verify renderer never produces those on its own.

- **R2-3 nullifies `image_data` only when `sl` was non-empty at scrub time.** Because R2-3 first nulls `semantic_label` then checks `sl` (the captured pre-clear value) — correct logic. No defect.

## Defects requiring code change before re-running the pipeline

_None._ The three Round 2 fixes match the spec, the new function passes the synthetic test, and no regression in the real `p1_template.json` data was identified.

## Synthetic test result

- import check stdout:
  ```
  [claude_extractor] Loaded 9 logo templates from local_assets
  ok
  ```

- synth flourish test stdout:
  ```
  [claude_extractor] Loaded 9 logo templates from local_assets
  [pipeline] synth header flourish under 'Sharable' at y=505
  [pipeline] synth header flourish under 'Entrees' at y=647
  After synth: 4 elements
    synth: sl=ornament/floral_swash_left bbox=(155,505) 280x42 has_img=True
    synth: sl=ornament/floral_swash_centered bbox=(860,647) 360x54 has_img=True
  ok
  ```
  Two image elements added with correct slug per text_align, `has_img=True` (S3 fetch + base64 succeeded via the local cached PNGs), bbox widths within the `header_w × 2` cap, heights ≤ 60 px.

## Overall: GREEN
