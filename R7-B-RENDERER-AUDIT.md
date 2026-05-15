# Renderer Audit — Post-Round-6

## Summary

`static/renderer.html` (1053 lines) is a Canvas 2D renderer that converts template JSON to pixel output. It correctly implements the R6-1 @font-face injection for custom fonts (BrittanySignatureRegular, Montserrat variants, etc.), properly loads all element types (text, logo, image, separator), and applies z-order rendering in two passes (text/lines → images/logos). However, five bugs prevent 100% pixel-accurate rendering of the real menu template: multi-line text wrapping is not implemented (causing overflow on long item descriptions and center-aligned category headers); the fillText `maxWidth` parameter is passed but has no effect without word-breaking logic; separators with missing image_data are silently rendered as centered single-pixel lines rather than styled rules; collage_box image stretching uses raw `drawImage` without aspect-ratio preservation; and font fallback detection (document.fonts.check) may return false for newly-injected @font-face rules due to timing, causing brief fallback rendering before the font promise resolves.

## Confirmed Bugs

### Bug 1: Multi-line text truncation/overflow on long item_description
**File:line**: `static/renderer.html:397–398`  
**Issue**: `ctx.fillText(content, tx, ty, w)` draws the entire `el.content` on a single line at `ty`. The `w` parameter is passed as `maxWidth` but has no effect without word-breaking logic. When text is wider than bbox.w (e.g., a 40-character item description in a 300px box), the text overflows right or gets clipped, rendering pixel-mismatched to the source PDF where the text naturally wrapped to 2–3 lines.

**Manifestation**: Center-aligned category headers (46 in the real template) and long item descriptions that span the full column width render on a single line, extending past the bbox boundary or being clipped by canvas bounds. The vertical centering via `ty = y + fontSize * 0.95` assumes single-line height.

**Impact**: Text elements fail visual fidelity on ~95/151 multi-line candidates.

**Suggested fix**: 
```javascript
// Replace ctx.fillText with a word-wrapping loop:
function drawTextWithWrap(ctx, text, x, y, maxW, lineHeight) {
  const words = text.split(' ');
  let line = '';
  for (const word of words) {
    const test = line + (line ? ' ' : '') + word;
    if (ctx.measureText(test).width > maxW && line) {
      ctx.fillText(line, x, y);
      y += lineHeight;
      line = word;
    } else {
      line = test;
    }
  }
  if (line) ctx.fillText(line, x, y);
}
// Call: drawTextWithWrap(ctx, content, tx, ty - (fontSize * 0.95), w, fontSize * 1.2);
```

---

### Bug 2: Font fallback race condition on initial render
**File:line**: `static/renderer.html:374` + `926–954`  
**Issue**: `document.fonts.check(`12px "${raw}"`)` is called immediately after `registerTemplateFonts(template)` awaits `document.fonts.ready` (lines 973, 1090). However, the promise chain is:
1. `injectFontFaces(fonts)` appends `<style>` and returns `document.fonts.ready`
2. `registerTemplateFonts` awaits the legacy FontFace API promises + `cssReady` + `document.fonts.ready` again
3. Rendering begins via `render()` call

The @font-face rules are in the DOM, but `document.fonts.check()` may return false in a tight loop if the font's data URL is still parsing. When false, the code falls back to FONT_CSS generic families, silently rendering BrittanySignatureRegular as "Great Vibes" or Montserrat variants as Arial.

**Manifestation**: Restaurant name (BrittanySignatureRegular, decorated script) renders in a serif/cursive fallback instead of the exact embedded font on the first render of a multi-page template.

**Suggested fix**:
```javascript
// In drawTextElement, after document.fonts.ready promise:
const raw = s.font_family || 'sans-serif';
let fontFamilyCSS;
try {
  // Force a re-check with increased timeout for data: URLs
  await Promise.race([
    document.fonts.load(`12px "${raw}"`),
    new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), 500))
  ]);
  // Now check should pass
  if (document.fonts.check(`12px "${raw}"`)) {
    // use raw family
  }
} catch {
  // fallback
}
```
Or simpler: ensure `registerTemplateFonts` is awaited BEFORE `render()` is called (already correct at load-time, but auto-load path should confirm).

---

### Bug 3: Single-line separator fallback for missing image_data + no semantic_label
**File:line**: `static/renderer.html:465–494` (`_drawSeparatorLine`)  
**Issue**: When `el.image_data` is falsy, `_drawSeparatorLine()` is called. The function draws a centered single-pixel rule:
```javascript
ctx.moveTo(x, y + h / 2);
ctx.lineTo(x + w, y + h / 2);
```
For a 660px-wide × 25px-tall ornament separator in the real template, this renders as a thin horizontal line at `y=370` (the center of the bbox). The source PDF shows a decorative ornament image at the same position — the bbox was correct, but no image_data was embedded (likely cleared by R2 fixes or missing from extraction). The fallback should either skip the element or draw a styled rule matching `style.stroke_width`, not a bare 1.5px dashed line.

**Manifestation**: Decorative ornaments that lost image_data (e.g., from label-clearing in R2-3) render as thin gray dashes instead of the floral/scroll patterns from the source menu. The menu's visual hierarchy flattens.

**Suggested fix**:
```javascript
// In _drawSeparatorLine, add an early check:
if (!el.image_data && el.subtype === 'decorative_divider' && !el.semantic_label) {
  // Skip rendering entirely — this is an empty placeholder
  return;
}
// Or render a styled rule:
const thickness = safeNum(style.stroke_width, 2);
ctx.lineWidth = thickness * state.scale;
ctx.setLineDash([10 * state.scale, 5 * state.scale]);
```

---

### Bug 4: collage_box image stretching (no aspect-ratio preservation)
**File:line**: `static/renderer.html:538–574` (`drawImageElement`)  
**Issue**: `drawImageElement` uses the same "contain" logic as logo/separator (lines 560–565):
```javascript
const scale = Math.min(w / img.width, h / img.height);
const drawW = img.width * scale;
const drawH = img.height * scale;
```
This is correct for logos and badges (portrait images). However, the real template has a **collage_box** at `bbox={x: 35.4, y: 2142.7, w: 478.3, h: 283.3}` with a landscape image_data. The logic scales the 660×480px source PNG down to fit within 478×283, which is correct **in theory**. But the code applies **centering** via `drawX = x + (w - drawW) / 2`, which is wrong for a collage_box that should fill the bbox tightly (or letter-box, not center-box).

**Manifestation**: A full-width collage panel (e.g., "Featured Cocktails" promo image) appears smaller and centered within its allocated space instead of filling the bounding box. The page layout looks broken around that panel.

**Suggested fix**:
```javascript
// In drawImageElement, differentiate by subtype:
if (el.subtype === 'collage_box') {
  // Scale to fill bbox (stretch, or fit with letter-boxing)
  const scaleX = w / img.width;
  const scaleY = h / img.height;
  // For collage: fill width, letter-box height
  const scale = Math.max(scaleX, scaleY);  // fill
  const drawW = img.width * scale;
  const drawH = img.height * scale;
  const drawX = x + (w - drawW) / 2;  // center horizontally
  const drawY = y + (h - drawH) / 2;  // center vertically
  ctx.drawImage(img, drawX, drawY, drawW, drawH);
} else {
  // badge/ornament: maintain aspect ratio, center
  const scale = Math.min(w / img.width, h / img.height);
  // ... existing code
}
```

---

### Bug 5: fillText maxWidth parameter has no effect without word-wrapping
**File:line**: `static/renderer.html:398`  
**Issue**: `ctx.fillText(content, tx, ty, w)` passes `w` as the 4th parameter (maxWidth). Per Canvas 2D spec, maxWidth causes the browser to **horizontally compress** the text if it overflows, not to wrap it. On a 40-character item description, the text gets squeezed horizontally to fit 300px, distorting the font metrics. This is invisible in the renderer (the text fits), but it's pixel-mismatched: the source PDF wrapped the text across lines at the natural word boundaries, not squeezed it.

**Manifestation**: Long item descriptions render in a compressed, hard-to-read font rather than wrapped to 2–3 lines as in the source.

**Suggested fix**: Implement word-wrapping as in Bug 1 fix, and **don't pass maxWidth** to ctx.fillText.

---

## Verified-correct paths

1. **@font-face injection (R6-1)**: `injectFontFaces()` (lines 889–915) correctly builds data: URLs and appends `<style>` to `document.head`. The promise chain in `registerTemplateFonts` (lines 926–954) properly awaits `document.fonts.ready` and the FontFace API. Font registration is called before `render()` at load time (line 973) and auto-load time (line 1090).

2. **Logo rendering with image_data**: `drawLogoElement()` (lines 496–536) correctly loads base64 PNG, decodes via Image.onload, applies contain-scaling, and centers within the bbox. Fallback to placeholder works.

3. **Two-pass z-order**: Lines 606–616 correctly separate text/lines (Pass 1) from images/logos (Pass 2). Decorators render on top as expected.

4. **Separator image_data handling**: When `el.image_data` is present, `drawSeparatorElement()` (lines 423–463) correctly loads and scales the PNG using contain logic.

5. **Background color**: `clearCanvas()` (lines 341–345) fills the entire canvas with white before rendering. This matches `canvas.background_color: "#ffffff"`.

6. **BBox positioning**: All draw functions correctly scale x/y/w/h by `state.scale` and use `safeNum()` to prevent NaN propagation.

7. **Text alignment**: `ctx.textAlign` (line 388) is correctly set to `left|center|right` and tx is adjusted accordingly (lines 392–394).

---

## Edge cases worth handling

1. **Font family mismatch**: If a text element has `font_family: "Podkova-Medium"` but no matching `@font-face` is registered (missing from `template.fonts`), `document.fonts.check()` returns false and the code falls back to `FONT_CSS[raw]` or the generic fallback. This is safe (renders in a serif fallback), but means menu-specific fonts fail silently. **Suggested**: Log a warning when a font is not found in `template.fonts`.

2. **Image data corruption**: If `el.image_data` is a malformed base64 string, `Image.onerror` fires and the element is skipped (logo/image) or falls back to a line (separator). **Safe path**, but corrupted images render as nothing.

3. **Missing bbox on an element**: The code checks `el.bbox ||` {}` and defaults x/y/w/h to 0/0/1/1 via `safeNum(..., 0)` and `Math.max(1, ...)`. Elements render at canvas origin but don't crash.

4. **Element type not in [text, logo, image, separator]**: Lines 608–616 only draw known types. Unknown elements are silently skipped. This is correct.

5. **Canvas dimensions mismatch**: If `template.canvas.width` is missing, line 598 defaults to 1200px. The renderer adapts, but the output may not match the intended aspect ratio.

6. **Center-aligned text without word-wrapping**: On a 300px-wide center-aligned category header with 20 characters, the text overflows right without wrapping. The tx anchor is at `x + w/2`, but the text extends past x+w.

---

## Implementation priority

- **Critical (blocks pixel fidelity)**: Bug 1 (multi-line text wrapping) — 46 center-aligned elements fail without it.
- **High (visual regression)**: Bug 3 (separator fallback) — ornaments render as thin lines instead of decorations.
- **Medium (layout breakage)**: Bug 4 (collage_box scaling) — full-width panels render centered/small.
- **Medium (font rendering)**: Bug 2 (font fallback race) — first render may use fallback font for 1–2 frames.
- **Low (UX polish)**: Bug 5 (fillText maxWidth) — text is squeezed instead of wrapped, but readable.
