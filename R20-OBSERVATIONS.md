# R20 Loop Observations

One block per regen+compare cycle. Honest visual inspection.

## Iter 3 baseline (before R20 changes) — 2026-05-16 ~06:19
Average weighted: 83.2%. AMI BRUNCH 90, FFL p1 75, FFL p2 76, bar p1 87, bar p2 87.

Largest visual gap: AMI FFL p1 As-Seen-On panel renders as overlapping colored shapes (food_network @ 86,2224 / hulu @ 34,2328 / youtube @ 371,2194). Per-panel resolver from R19.5 was being overwritten by `_apply_s3_natural_bbox` (S3 normalize pass).

## Iter 4 — R20.1 + R20.2 + R20.3 — (regen pending at write)

R20.1 plan:
- Move panel-resolver to AFTER `_apply_s3_natural_bbox` so it has the last word on bbox.
- Add `has_aso_text` trigger so R16 inject fires even when Claude misses inline brand badges (vision is non-deterministic — one run gives food_network/hulu/youtube, next gives nothing).

R20.2:
- `_split_name_price` now keeps trailing 4-digit vintages (1900-2099) IN the wine name instead of peeling them off as bogus prices on Bordeaux/Burgundy entries.

R20.3:
- `_is_footer` now also catches "As seen on:", "Featured on", and quoted ALL-CAPS show names ("SUMMER RUSH", "FOY RUSH"). These were appearing as ghost items in p1.

Will populate observations after view-reading the regenerated compare.
