# R7-C — Big gray brand badges placement

## Summary
The "As Seen On Food Network" + "Diners' Choice" big gray circular badges land at y=1859 / y=2095 (Claude vision estimate), overlapping right-column menu items. Source has them at y≈2300–2700 (bottom-right brand zone). The OpenCV template-matching path doesn't help — local_assets contains only the *small colored* versions of these badges, not the big gray variants. Recommended fix is **Option D: hybrid y-snap** — keep Claude's label, but for known-brand badges that Claude places in the menu-text zone, snap their y-coordinate down to the lower-right brand zone.

## local_assets/ inventory (badges)
| File | Size | Style |
|---|---|---|
| food_network.png | 4.8 KB | small colored (logo, ~100 px) |
| opentable_diners_choice.png | 6.6 KB | small colored |
| yelp.png | small colored |
| tripadvisor.png | small colored |
| youtube.png | small colored |
| michelin.png | small colored |
| zagat.png | small colored |
| best_of.png | small colored |

No PNGs match the big gray circle style.

## Why Claude's y-coords are wrong
Claude detects the badges visually but its bbox estimates anchor to the first visual gap it perceives in the right column (around y≈1859, just below the menu items' continuous run). The actual placement is in white-space reserved below all content. Pure-vision bbox estimation without pixel-anchor lookup is approximate; for badges that sit in unusual locations, the estimate is off by 400–800 px.

## Recommended fix — Option D (hybrid y-snap)

### Implementation in `pipeline.py:_inject_pdf_graphics`
Add after the badge-injection block (~line 786), before S3 resolution:

```python
# R7-C: Known brand badges (Food Network, Diners' Choice) get misplaced by Claude.
# Snap their y-coordinate to the lower-right brand zone when Claude places them
# inside the menu-text band. Only fires when current y is significantly above
# the expected zone — protects against false snapping on menus that legitimately
# have these badges higher up.
_BRAND_BADGE_LOWER_ZONE = {
    "badge/food_network":              (0.65, 0.85),
    "badge/opentable_diners_choice":   (0.72, 0.92),
}
for el in graphic_els:
    if el.get("subtype") != "badge":
        continue
    sl = el.get("semantic_label", "")
    if sl not in _BRAND_BADGE_LOWER_ZONE:
        continue
    bd = el.get("bbox") or {}
    current_y = float(bd.get("y", 0))
    zone_min, zone_max = _BRAND_BADGE_LOWER_ZONE[sl]
    target_y_min = canvas_h * zone_min
    if current_y < target_y_min and (target_y_min - current_y) > canvas_h * 0.15:
        bd["y"] = target_y_min
        print(f"[pipeline] R7-C badge snap: {sl} y {current_y:.0f} → {target_y_min:.0f}")
```

## Larger size for big brand badges
In `_apply_s3_natural_bbox` (~line 75), add a size_hint check. When the badge label is in the brand-badge set AND the post-snap y is in the lower zone (y > canvas_h * 0.65), use 200 px target height instead of 130 px:
```python
if el_type == "image" and el_sub == "badge":
    is_brand_badge = (
        sl in ("badge/food_network", "badge/opentable_diners_choice")
        and cy > canvas_h * 0.6
    )
    target_h = 200.0 if is_brand_badge else (130.0 if aspect < 1.5 else 90.0)
```

## Risk + edge cases
- Other menus may not have these badges → snap doesn't fire (only triggers on the two specific labels)
- A variant menu legitimately places a Food Network badge mid-page → the `> 0.15` displacement threshold protects against snapping unless the displacement is severe
- Bar & Patio / AMI Brunch likely don't have these badges → no effect

## Future improvement
Add proper big-gray-circle PNG templates to `local_assets/` so OpenCV `match_badges` can pixel-anchor them. Then the snap heuristic becomes a fallback rather than primary. Not in this round.
