# Menu Renderer Diagnostic Report

## Summary

The HTML Canvas renderer (static/renderer.html) and the Python extraction pipeline both have critical gaps that prevent complete menu rendering. The renderer fails to apply embedded fonts to canvas text because the Canvas 2D API does not support FontFace-registered fonts—only system fonts and pre-linked CSS fonts. The pipeline undersizes synthetic decorative swashes (60px max), merges multiple logos into a single element, and produces some empty image elements with no visual content. These issues combine to render the menu visually incomplete: section headers appear in fallback serif instead of elegant script fonts, ornamental flourishes are too small, sub-logos at page bottom are lost, and decorative panels are missing or undersized.

---

## Renderer Bugs

### R6-R-1: Canvas Text Cannot Use FontFace-Registered Fonts

- **File:line** — renderer.html:374–390, 884–909
- **What the code does**
  - `registerTemplateFonts()` (line 884) decodes base64 TTF data and creates FontFace objects.
  - FontFace objects are added to `document.fonts` via `.add()` (line 900).
  - `document.fonts.ready` is awaited to ensure registration (line 908).
  - In `drawTextElement()`, the code checks `document.fonts.check()` (line 374) to verify the font is registered.
  - If registered, a font-family stack is constructed using the raw font name (line 378–381).
  - The canvas context applies this stack via `ctx.font = ... fontFamilyCSS` (line 390).

- **Why it breaks the visible output**
  - The HTML Canvas 2D rendering context (`CanvasRenderingContext2D`) **does not query `document.fonts`** when rasterizing text.
  - Canvas only uses:
    1. System installed fonts (by name)
    2. Web fonts pre-loaded via CSS `<link>` tags (e.g., Google Fonts on line 10)
  - FontFace objects registered via the FontFace API work for DOM rendering (`document.body` text) but **not** for canvas `ctx.fillText()`.
  - When a custom font like `BrittanySignatureRegular` is specified in `ctx.font`, the canvas rasterizer doesn't find it in system fonts and falls back to the fallback chain (e.g., "Great Vibes" or generic serif).
  - **Result:** Section headers ("Sharable", "Entrées") render in plain italic serif or a generic cursive, not the elegant script font embedded in the JSON.

- **Suggested fix**
  - The renderer must inject embedded fonts as CSS `@font-face` rules into the document, not via the FontFace API alone.
  - In `registerTemplateFonts()`, after decoding the base64 TTF, dynamically create a `<style>` tag with `@font-face` rules:
    ```javascript
    const styleEl = document.createElement('style');
    styleEl.textContent = `@font-face {
      font-family: "${f.family}";
      src: url(data:application/octet-stream;base64,${f.data_base64}) format('truetype');
      font-weight: ${f.weight || 'normal'};
      font-style: ${f.style || 'normal'};
    }`;
    document.head.appendChild(styleEl);
    ```
  - Then, when setting `ctx.font`, the canvas *should* have access to the font (browser support varies; Safari and Chrome support data: URIs in canvas).
  - **Alternative:** Use a canvas font library like OpenType.js to render text as paths/shapes, bypassing the system font lookup entirely. This guarantees pixel-accurate rendering but is more complex.

---

### R6-R-2: Empty Image Elements Render as Silent No-ops

- **File:line** — renderer.html:538–574
- **What the code does**
  - `drawImageElement()` iterates over image elements with `type === 'image'`.
  - If `image_data` is present (truthy), it decodes the base64 PNG, caches it, and draws it via `ctx.drawImage()`.
  - If `image_data` is falsy/null, the code reaches the catch block or skips the draw silently (line 566).

- **Why it breaks the visible output**
  - The JSON contains an empty collage_box element (id=img_3be48d48, 660×44 px) with `image_data: null` and no `semantic_label`.
  - This element renders as empty space on the canvas—no error, no placeholder, just an empty bbox.
  - The user sees a blank stripe or gap instead of a meaningful image or error indication.
  - **Result:** Decorative panel expected at that position is missing or invisible.

- **Suggested fix**
  - In the pipeline, filter out image elements where `image_data` is null before writing the JSON. In `claude_extractor.py` or `pipeline.py`, add a validation step:
    ```python
    elements = [e for e in elements if not (e.get('type') == 'image' and not e.get('image_data'))]
    ```
  - Alternatively, in the renderer, skip rendering images with no `image_data`:
    ```javascript
    if (!imageData) {
      if (els.showBBoxes.checked) {
        ctx.strokeStyle = '#ff0000';  // red bbox for debug
        ctx.lineWidth = 2;
        ctx.strokeRect(x, y, w, h);
      }
      return;
    }
    ```
  - This makes empty elements visible during debugging and prevents them from rendering silently.

---

## Pipeline Bugs

### R6-D-1: Synthetic Swashes Hardcoded to 60px (Too Small)

- **File:line** — pipeline.py:557–638, specifically line 617
- **What the code does**
  - `_synthesize_header_flourishes()` injects decorative swash image elements below category headers.
  - For each header, it resolves the S3 asset slug (e.g., `ornament/floral_swash_centered`) and fetches the PNG.
  - It computes the target height: `target_h = 60.0` (hardcoded, line 617).
  - It scales width by aspect ratio, then caps width to `hw * 2.0` (max 2× header width, line 620).
  - The flourish is embedded as base64 PNG in the JSON with `semantic_label` and full `image_data`.

- **Why the resulting JSON is incomplete**
  - A 60px-tall ornamental swash is too small to create visual impact in a menu layout.
  - The PDF source likely has elaborate 100–150px flourishes with intricate detail.
  - The JSON swash is undersized relative to the original, so the rendered menu looks sparse and less ornate.
  - **Result:** Swashes appear as thin, delicate lines instead of large flowing ornaments.

- **Suggested fix**
  - Increase the hardcoded `target_h` from 60 to 100–120 pixels:
    ```python
    target_h = 100.0  # increased from 60.0
    target_w = target_h * aspect
    target_w = min(target_w, hw * 3.0)  # increase cap to 3× header width
    target_h = target_w / aspect
    ```
  - Consider scaling based on header width/height for proportional flourishes:
    ```python
    target_h = min(150.0, hw * 0.5)  # use 50% of header width as flourish height
    ```

---

### R6-D-2: `_enforce_single_logo` Merges Distant Logos into One

- **File:line** — claude_extractor.py:697–786, specifically lines 726–728 and 778–785
- **What the code does**
  - `_enforce_single_logo()` is called to consolidate multiple detected logo elements into a single merged logo.
  - It selects an anchor logo by priority: top_center > bottom_center > other (line 722–723).
  - It computes a proximity threshold: `proximity_threshold = anchor_size * 2.0` (line 728), where `anchor_size` is the max dimension of the anchor logo.
  - All logos within this radius are merged into a single union bounding box (lines 739–751).
  - Logos outside the threshold are reclassified as `type: "image", subtype: "badge"` (lines 778–785).

- **Why the resulting JSON is incomplete**
  - The menu has 2–3 logos in different positions (top center, bottom left, bottom center).
  - Claude's vision detection fragments each logo into 2–3 separate detections (e.g., emblem + frame + text label).
  - `_enforce_single_logo` uses a linear proximity distance of `anchor_size * 2.0`.
  - If the top logo is 200px wide, the threshold is 400px. A bottom logo 300px below the top might fall within this 400px radius in Euclidean distance, causing unwanted merging.
  - **Result:** Multiple logos are merged into one with a huge union bbox, losing the semantic distinction between top and bottom logos.

- **Suggested fix**
  - Add a **vertical separation check** to prevent merging logos in clearly different positions:
    ```python
    anchor_bbox = anchor_logo.get("bbox", {})
    anchor_bottom = anchor_bbox.get("y", 0) + anchor_bbox.get("h", 0)
    
    for i, el in logos:
        if i == anchor_idx:
            continue
        bd = el.get("bbox", {})
        el_top = bd.get("y", 0)
        
        # Don't merge if vertically separated by more than 2× anchor height
        vert_gap = max(0, el_top - anchor_bottom) if el_top > anchor_bottom else 0
        if vert_gap > anchor_size:
            continue  # Skip merging
        
        # Otherwise apply the proximity check...
    ```
  - Or, allow up to N=3 logos by rank-scoring and keeping only the top 3 by priority:
    ```python
    if len(logos) <= 3:
        return elements  # Keep all 3 logos
    ```

---

### R6-D-3: Reclassified Logos Lose Semantic Context

- **File:line** — claude_extractor.py:778–785
- **What the code does**
  - When a logo is reclassified as an image element (line 779–785), it's given `type: "image"` and `subtype: "badge"`.
  - The `semantic_label` is preserved if it exists on the original logo element (line 783–784).

- **Why the resulting JSON is incomplete**
  - Most logos detected by Claude don't have a `semantic_label` field—that field is typically only populated for S3 assets and awards/badges.
  - When a distant logo is reclassified without a semantic label, the renderer treats it as a generic image/badge, and it may be rendered at the wrong size, position, or priority.
  - **Result:** Sub-logos at the page bottom ("ON THE LAKE" and "ANNA MARIA" locations) are either missing or rendered incorrectly because they were reclassified as badge images with no semantic context.

- **Suggested fix**
  - Preserve more metadata when reclassifying distant logos:
    ```python
    badge_el: dict = {
        "type": "image",
        "subtype": "badge",
        "bbox": bd,
        "semantic_label": el.get("semantic_label") or f"logo_{el.get('id', 'unknown')}",
        "position_hint": el.get("position_hint", "unknown"),  # preserve hint
    }
    ```
  - Add a flag to indicate the element was originally a logo:
    ```python
    badge_el["original_type"] = "logo"
    ```

---

## Code Paths That Are Correct (Don't Touch)

1. **renderer.html:496–536** — `drawLogoElement()` correctly decodes base64 `image_data` and renders with `ctx.drawImage()`. The code is sound; the input JSON has valid image data. If the logo doesn't appear, the issue is upstream (font, filter, or async loading).

2. **renderer.html:538–574** — `drawImageElement()` correctly handles `image_data` for all image subtypes (badge, ornament, collage_box). The caching and aspect-ratio scaling are correct.

3. **renderer.html:423–463** — `drawSeparatorElement()` correctly renders both plain lines and image-based separators (wavy lines, ornaments). The aspect-ratio contain logic is correct.

4. **renderer.html:470–472** — Border-subtype separators are correctly rendered as `ctx.strokeRect()`.

5. **renderer.html:605–616** — Two-pass rendering (text + lines first, then images/logos) is correct and prevents z-order issues.

6. **pipeline.py:567–606** — Asset resolution (`resolve_asset()`) and PNG decoding are correct.

7. **pipeline.py:884–909** — Font registration via FontFace API works correctly for DOM elements; it's only the canvas context that can't access FontFace objects.

8. **claude_extractor.py:888–909** — `registerTemplateFonts()` correctly decodes base64 and loads fonts into `document.fonts`.

---

## Verification Checklist

- [x] Does renderer.html register `@font-face` rules from the `fonts` array? **No.** It uses the FontFace API, which doesn't affect canvas. CSS `@font-face` rules need to be injected into the document via `<style>` tags.

- [x] Does renderer.html draw a `<canvas>` `drawImage` for elements where `type === "logo"` with `image_data`? **Yes**, line 516–523. The code is correct.

- [x] Does renderer.html draw image elements (`type === "image"` with `image_data`) at their bbox? **Yes**, line 558–565. The code is correct.

- [x] Does renderer.html handle `subtype === "border"` separators by drawing a rectangle outline? **Yes**, line 470–472. The code is correct.

- [x] Does the pipeline's `_enforce_single_logo` merge all logo regions into ONE logo, killing sub-logos? **Yes**, line 739–751. It uses a proximity threshold without vertical separation checks, causing logos in different regions to merge.

- [x] Does any code filter out collage_box elements with `image_data: null`? **No.** Empty collage_box elements are included in the JSON and render silently.

- [x] What controls the synth swash size in `_synthesize_header_flourishes`? **Line 617:** `target_h = 60.0` (hardcoded constant). This is too small.
