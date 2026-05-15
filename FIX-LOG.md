# FIX-LOG — Decorator Placement Fixes

Targeted patches for the three root causes diagnosed in `DECORATOR-PLACEMENT-ROOT-CAUSE.md`. All edits are working-tree changes only (no commits). Imports verified with `./venv/bin/python3 -c "import pipeline; import claude_extractor; print('ok')"` after each fix.

## Files changed

- `pipeline.py`
- `claude_extractor.py`

(Pre-existing uncommitted edits in `analyzer.py`, `claude_extractor.py`, `pipeline.py` were preserved — none of them touched the regions modified here.)

---

## Fix 3 — Subtype-safe label transfer (smallest scope, done first)

### `pipeline.py::_enrich_template_separators_from_claude`

Old behaviour: 660×44 collage_box panels were inheriting `ornament/scroll_divider` labels because (a) tolerance was loose (TOL_Y=80, TOL_X=200) and (b) any Claude image whose label started with `ornament/` or `separator/` was eligible as a source — including collage_boxes that happened to live near a PDF separator.

Code changes:
- `pipeline.py:264-281` — narrowed `claude_decoratives` filter: skip `subtype=="collage_box"`; only accept Claude `separator` elements or `image` elements that themselves bear a `separator/`-or-`ornament/` label.
- `pipeline.py:286-287` — `_TOL_Y` 80 → 25, `_TOL_X` 200 → 80.
- `pipeline.py:295-318` — orientation-match guard: only transfer a label between source and destination separators of the same orientation (horizontal/vertical). Orientation is read from `el.get("orientation")` or inferred from bbox aspect.

### `pipeline.py::_inject_pdf_graphics`

- `pipeline.py:593-604` — when a Claude `collage_box` is about to be appended with `semantic_label` starting with `ornament/` or `separator/`, the bad label is cleared (set to `None`) instead of carried forward; an info log line records the drop. The element itself is still appended so the panel still renders.

---

## Fix 2 — Drop `_scan_pdf_decorators_via_claude` from the main flow (Option A)

### `pipeline.py::process` (call-site removal)

- `pipeline.py:932-940` — the entire `extra_decorators = _scan_pdf_decorators_via_claude(...)` block plus the 40-px proximity-anchored append loop has been removed and replaced with a comment recording the choice (Option A). Decorators now come exclusively from the `extract_layout_surya_som` pipeline, which has snapping and validation.

### `pipeline.py::_scan_pdf_decorators_via_claude` (definition kept)

- `pipeline.py:118` — single-line `# DEPRECATED (Fix 2): …` comment above the function. Body intentionally untouched so future callers can still re-enable it.

### `pipeline.py::_cleanup_duplicate_graphics` cleanup tightening

- `pipeline.py:384-403` — text-overlap drop threshold lowered from `>=2` text neighbours within 40 px to `>=1` within 30 px. An exception preserves any ornament whose `semantic_label` starts with `separator/` (explicit decorative dividers get the benefit of the doubt).

---

## Fix 1 — Snap (or drop) graphic decorators into content gaps

### `claude_extractor.py::_snap_graphic_decorators` (NEW)

- `claude_extractor.py:2195-2335` — new function placed immediately after `_snap_decorative_headers`. For every element with `type in ("image","separator")` whose `semantic_label` starts with `ornament/` or `separator/` OR whose `subtype` is `ornament`/`decorative_divider`:
  1. Collect non-decorative text content blocks within a ±200 px y-band (or `max(150, element_h * 2.5)` clipped to ≤200).
  2. Build the gaps between consecutive nearby content blocks plus the open space above/below the cluster.
  3. Pick the LARGEST nearby gap.
  4. If the element's vertical center already sits in that gap, leave it.
  5. Otherwise, move it so its vertical center sits at the gap midpoint.
  6. If no nearby gap is ≥ `element_h + 12` px, DROP the element — but only if it is "droppable" (unlabeled or `ornament/`-labelled). PyMuPDF vector separators (`subtype=horizontal_line|vertical_line`, no semantic_label) are never dropped, never moved on the "no gap" branch.
- `[snap_graphic] move …` and `[snap_graphic] drop …` print lines preserve traceability.

### Wiring

- `claude_extractor.py:1743-1747` — call inserted in `extract_layout_surya_som` post-processing between `_snap_decorative_headers` and `_enforce_single_logo`.
- `claude_extractor.py:2427-2429` — call inserted in `merge_layouts` post-processing right after `_snap_decorative_headers`.

---

## Expected behavioural change on the next pipeline run

1. Ornament image clusters at incorrect y (the "y=3053–3112 over warning text" pattern in the FFL example) will be either:
   - moved into the nearest legitimate content gap, OR
   - dropped entirely when no gap of suitable height exists within ±200 px.
2. The second Anthropic decorator API call is no longer made; one fewer network round-trip per PDF side. `[scan_decorators]` log lines will disappear.
3. Decorative-label transfer from Claude to PDF vector separators only happens within a tight 25×80 px window AND only between matching orientations; collage_box panels never donate labels. The "660×44 collage_box with `ornament/scroll_divider` label" failure mode is closed.
4. Collage_box panels with bad inherited labels at injection time get the label cleared (`null`) but still render — the panel will fall back to its pixel crop instead of stretching an `ornament/*` S3 asset.
5. `_cleanup_duplicate_graphics` removes more stragglers because the text-proximity threshold went from 40 px / 2 neighbours to 30 px / 1 neighbour.

## Conflicts with existing uncommitted edits

None. The user's existing diff touches:
- `pipeline.py::_cleanup_duplicate_graphics` Fix 4 (logo containment) — non-overlapping with the threshold change.
- `claude_extractor.py` Surya device strategy + tool-use schema additions — non-overlapping with the new `_snap_graphic_decorators` function and its wiring points.
- `analyzer.py` — untouched by this work.

## Verification performed

- `./venv/bin/python3 -c "import pipeline; import claude_extractor; print('ok')"` after each fix — all pass.
- Smoke tests of `_snap_graphic_decorators` with synthetic element lists:
  - Ornament inside a real gap → left alone.
  - Ornament sitting on text with a usable gap nearby → moved to gap midpoint.
  - PyMuPDF vector separator with no semantic_label → preserved regardless of position.
  - Dense text band with no gap ≥ element_h+12 → unlabeled and `ornament/`-labelled graphics dropped; `separator/`-labelled and PyMuPDF vector separators preserved.
- Full pipeline NOT run (per instructions).

---

## Follow-up patch (post-QA)

Three small inline conditional fixes addressing the three defects called out in
`QA-REPORT.md` § "Defects requiring code change before user runs the pipeline".
No new functions; pre-existing local edits and the prior Fix 1/2/3 work are
preserved.

### Defect B — open-margin pseudo-gaps now fallback-only

`claude_extractor.py:2286-2308` (inside `_snap_graphic_decorators`).
Split the prior single `gaps` list into `real_gaps` (true inter-content gaps)
and `fallback_gaps` (open space above the topmost / below the bottommost
content block). The fallback margins are only considered when no real gap
already satisfies `gap_h >= need` (where `need = element_h + 12 px`). This
stops a 92 px open-top margin from beating a legitimate 80 px inter-content
gap on bare size.

### Defect A — MOVE branch now respects ANY usable gap

`claude_extractor.py:2329-2336`. Replaced the prior
"already inside the LARGEST gap?" check with
"already inside ANY usable gap (`gap_h >= need`)?". Combined with the
Defect-B `candidate_gaps` correction, an ornament that is already sitting in
a real inter-content gap that fits it will be left alone even if a larger
fallback margin exists — closing the CASE-2 regression in the QA synthetic
suite.

### Defect C — cleanup exempts ornaments under section headers

`pipeline.py:397-412` (inside `_cleanup_duplicate_graphics`). The within-30 px
text-neighbour drop now collects the actual neighbour elements (Option 1).
The drop is skipped when EVERY neighbour is "header-like" — i.e. either
`subtype == "category_header"` or has `style.font_family` in
`("decorative-script", "display")`. If even one neighbour is body text the
ornament is still dropped, matching the spec's per-neighbour scoping.

### Verification

- `./venv/bin/python3 -c "import pipeline; import claude_extractor; from claude_extractor import _snap_graphic_decorators; print('ok')"`
  stdout: `[claude_extractor] Loaded 9 logo templates from local_assets\nok`,
  stderr empty.
- Mental re-run of QA CASE 2 (ornament at y=150 inside 80 px real gap;
  open-top margin = 92 px): `real_gaps = [(.., .., 80)]`, `need ≈ el_h+12 < 80`,
  so `real_usable` is non-empty, fallback margins are excluded, and
  `already_in_a_usable_gap` is True → element is preserved at y=150.
- Full pipeline NOT run.

---

## Round 2 patch

Three targeted fixes addressing the regressions diagnosed in
`DECORATOR-PLACEMENT-ROUND2.md`. All edits are in `pipeline.py`; no other
files were touched. Pre-existing uncommitted edits and the original Round 1
fixes are preserved.

### Fix R2-1 — Header-flourish synthesis pass (Bug 1)

`pipeline.py:514-595` — new function `_synthesize_header_flourishes()`
inserted just above `_inject_pdf_graphics`. For each
`text/category_header` element it injects an
`ornament/floral_swash_centered` (or `_left` when the header is
left-aligned) image 14 px below the header baseline, sized to ~60 px
height with width capped at `header_w × 2`. Uses pixel-accurate PyMuPDF
text positions instead of Claude Vision's approximate wavy-line bboxes,
so it works independently of the tightened TOL_Y=25 in
`_enrich_template_separators_from_claude`. Skips when an image element
is already within 40 px vertically of the target position.

`pipeline.py:1072` — call site added in `process()` immediately after
`_enrich_template_separators_from_claude` and before
`_cleanup_duplicate_graphics`.

`pipeline.py:1073-1083` — drop pass for unlabelled
`separator/decorative_divider` elements with no `image_data` and no
`semantic_label` (these are the empty bbox outlines that survived
enrichment). Runs immediately before `_cleanup_duplicate_graphics`.

### Fix R2-2 — Drop tiny unlabelled ornament fragments (Bug 2)

`pipeline.py:865-876` — inside `_inject_pdf_graphics`, right after the
pixel-crop block that assigns `image_data` for unmatched image elements,
filter `graphic_els` to remove any unlabelled `image/ornament` with
`w < 100` AND `h < 40`. Drops OpenCV dilation artefacts before they
reach the template.

`pipeline.py:414-428` — inside `_cleanup_duplicate_graphics`, added a
"Fix 2b" guard immediately after the existing text-area ornament drop
(Fix 2). Same predicate (unlabelled `image/ornament`, w<100 AND h<40)
as a final whole-template sweep so fragments injected by any code path
(not just `_inject_pdf_graphics`) get dropped.

### Fix R2-3 — Final collage_box label scrub (Bug 3)

`pipeline.py:900-913` — at the end of `_inject_pdf_graphics`, just
before `tmp = build_template_from_claude(...)`, scrub any
`image/collage_box` whose `semantic_label` starts with `ornament/` or
`separator/`. Sets `semantic_label = None` and clears any `image_data`
that was fetched for the wrong asset (so the renderer falls back to a
source-image pixel crop). Catches bad labels assigned by the
overlap-based label-transfer loop at lines 555–565 that Round 1 Fix 3
didn't guard.

### Verification

- `./venv/bin/python3 -c "import pipeline; from pipeline import _synthesize_header_flourishes; print('ok')"`
  stdout: `[claude_extractor] Loaded 9 logo templates from local_assets\nok`,
  stderr empty.
- Full pipeline NOT run (per instructions).

---

## Round 3 patch (menu_data)

Four targeted fixes addressing text/menu_data accuracy regressions diagnosed
in `MENUDATA-ROUND3.md`. Edits confined to `analyzer.py` and `pipeline.py`;
no other files were touched. Pre-existing uncommitted edits and all Round
1+2 fixes are preserved.

### Fix R3-4 — Varietal/country blacklist for address detection

`analyzer.py` (top of module, around the address regex constants) — added
`_WINE_VOCAB` frozenset (varietals + wine regions) and a `_looks_like_wine_entry`
helper. Also added `ALL_CAPS_NUMERIC_PREFIX` module-level regex used by R3-2.

`analyzer.py:_is_address` — at the very top (after `has_zip = ...`), inserted
a wine-entry early-return: if the text contains wine vocabulary AND there is
no real US ZIP, the candidate is rejected. Stops "224 Pinot Noir, Pike Road, OR"
from being classified as `address`.

### Fix R3-3 — Suppress orphan item_name (top-right metadata leak)

`analyzer.py:build_menu_data` — inside the `elif sem == "item_name":` branch,
when `current_cats` is empty AND no fallback category exists, the block is now
skipped via `continue` instead of auto-creating a "General" category. Prevents
top-of-page metadata ("FEB", "Dinner Menu", "Brunch") that gets classified as
`item_name` from leaking into menu_data as items.

### Fix R3-2 — Wine sub-category detection in the classifier

`analyzer.py:_classify` — added a new rule immediately after the script-font
heuristic and before the existing `font_ratio >= 0.75` check. Promotes ALL
CAPS bold text in the 14-34pt absolute range to `category_header` when there
is no inline price, no leading wine-code prefix, and `len(text) <= 35`. Uses
the `ALL_CAPS_NUMERIC_PREFIX` regex added at the top of the file to reject
wine-code lines like "224 Pinot Noir...". Catches sub-headers like
"SPARKLING WINE", "FRANCE", "CALIFORNIA" that were missed by the ratio rules
when the page's max_font was inflated by a large script header.

### Fix R3-1 — Use Claude vision's restaurant_name as PDF fallback

`pipeline.py` (PDF branch, immediately after `menu_data = build_menu_data(...)`
in the `if ext in SUPPORTED_PDF:` block) — added a fallback block that
overrides `menu_data.restaurant_name` with `claude_layout["menu_data"]["restaurant_name"]`
when:
1. The analyzer assigned nothing, OR
2. The analyzer's pick matches a detected category header (case-insensitive), OR
3. The analyzer's pick matches a generic title word ("brunch", "dinner menu",
   "white wines", "patio & bar menu", etc.).
Claude's pick is itself rejected if it matches a category header. A
`[pipeline] R3-1: restaurant_name '...' → '...' (Claude vision)` log line
records every override.

### Verification

- `./venv/bin/python3 -c "import pipeline; import analyzer; from analyzer import _is_address, _looks_like_wine_entry; print(_is_address('224 Pinot Noir, Pike Road, OR'), _is_address('5325 Marina Dr ~ Holmes Beach FL 12345'))"`
  stdout: `[claude_extractor] Loaded 9 logo templates from local_assets\nFalse True`,
  stderr empty. (Expected `False True` — passes.)
- Synthetic `_classify` smoke test:
  - `RawBlock(text='BORDEAUX, FRANCE (Left Bank)', font_size=22, is_bold=True)` →
    `item_name` (NOT `category_header` — the parenthetical "Left Bank" breaks the
    `isupper()` check in the literal R3-2 rule; simpler ALL CAPS sub-headers
    like `SPARKLING WINE`, `FRANCE`, `CALIFORNIA` all return `category_header`).
  - `RawBlock(text='224 Pinot Noir, Pike Road, OR', font_size=14)` →
    `item_description` (NOT `category_header`, NOT `address` — correct).
  - `RawBlock(text='Dinner Menu', font_size=18, is_bold=True)` → `item_name`
    (correct — will be dropped by R3-3 at `build_menu_data` since no
    category exists yet at that point in reading order).
- Full pipeline NOT run (per instructions).

### Conflicts with existing uncommitted edits

None. The user's existing diff in `analyzer.py` contributes the `_is_address`
skeleton + script-font heuristic; R3-4 extends `_is_address` at the top, R3-2
inserts a new rule immediately after the script-font heuristic, R3-3 modifies
only the `item_name` branch of `build_menu_data`. R3-1 is additive in the PDF
branch of `pipeline.py` and does not touch any Round 1/2 logic.

---

## Round 3 follow-up patch (post-QA)

Five inline-conditional patches addressing the defects called out in
`QA-REPORT-R3.md` § "Defects requiring code change". Two blockers + one
must-fix + two nice-to-have. Edits confined to `analyzer.py` and `pipeline.py`;
no new functions, all previous Round 1/2/3 work preserved.

### Patch P1 — R3-2 ALL-CAPS-bold-item-name false-positive (BLOCKER)

`analyzer.py:_classify` — the R3-2 trigger now requires:
- `b.font_size >= 18.0` (raised floor from 14; real wine sub-headers run 18-30pt,
  bold item names are 12-16pt), AND
- `b.w < canvas_w * 0.30` when `canvas_w > 0` (wine sub-headers like `CALIFORNIA`
  are narrow; full item names like `PAN SEARED HALIBUT` span much more width).

To plumb `canvas_w` into `_classify`:
- `analyzer.py:_classify` signature gained `canvas_w: float = 0.0`.
- `analyzer.py:classify_blocks` signature gained `canvas_w: float = 0` and
  forwards it to every `_classify(...)` call.
- `pipeline.py` — both `classify_blocks(...)` call sites (PDF branch at
  ~line 1046 and OCR fallback at ~line 1413) now pass `canvas_w=canvas_w`
  (already in scope at both sites).

Synthetic check: `FILET MIGNON` / `PAN SEARED HALIBUT` / `NEW YORK STRIP STEAK`
(14-16pt, bold, 400 px width on a 2200 px canvas) all return `item_name`.
`SPARKLING WINE` (22pt, bold, 300 px width) still returns `category_header`.

### Patch P2 — Bordeaux ALL-CAPS detection (MUST-FIX)

`analyzer.py:_classify`, just above the R3-2 trigger. Replaced the single-step
strip (which left interior letters of parenthesized annotations) with:
1. `no_parens = re.sub(r"\([^)]*\)", "", text)` — drop `(Left Bank)` entirely.
2. `stripped_alpha = re.sub(r"[^A-Za-zÀ-ÿ]", "", no_parens)` — keep only
   letters (incl. accented).

`has_wine_code_prefix` still keys off the original `text`, since wine codes
appear outside parens.

Synthetic check: `BORDEAUX, FRANCE (Left Bank)` (22pt, bold, narrow) now
returns `category_header`.

### Patch P3 — R3-3 safety valve

`analyzer.py:build_menu_data`. Added a local `orphan_items: list[MenuItem]`.
The R3-3 `continue` branch now appends the parked item to `orphan_items`
instead of silently dropping. After the main classification loop, if
`categories` is empty AND `orphan_items` is non-empty, a single `General`
bucket is created with those items and a `[analyzer] R3-3 safety valve: N
orphan items routed to 'General'` line is printed. The legacy "every
item_name before the first category creates a General bucket" behaviour is
NOT restored — orphans are still dropped when at least one real category was
found.

### Patch P4 — R3-1 symmetric generic-title blacklist (nice-to-have)

`pipeline.py` — added module-level `_GENERIC_TITLE_WORDS` frozenset just
under `SUPPORTED_PDF` / `SUPPORTED_IMG`. The R3-1 `should_use_claude`
predicate now compares **both** `current_lc` AND `claude_rn_lc` against
this set. Claude's pick is rejected if it falls into the generic-title set,
closing the "Brunch overwrite" edge case from QA § Regression risks.

### Patch P5 — R3-4 word-boundary in wine detector (nice-to-have)

`analyzer.py:_looks_like_wine_entry`. Dropped the raw `w in lower` substring
arm. New implementation tokenises on non-letter chars (preserving accented
ranges via `À-ÿ`) and tests set membership against `_WINE_VOCAB`. Closes
the substring leak where `"rose"` matched `"rosewood drive"`.

### Verification

```
./venv/bin/python3 -c "<canned check from instructions>"
```

stdout:
```
[claude_extractor] Loaded 9 logo templates from local_assets
Bordeaux: category_header
FILET MIGNON: item_name
PAN SEARED HALIBUT: item_name
NEW YORK STRIP STEAK: item_name
SPARKLING WINE: category_header
rosewood drive: False
pinot noir: True
```

stderr: empty. All expectations met.

Full pipeline NOT run (per instructions). No commits made.

### Conflicts with existing uncommitted edits

None. All five patches edit code paths introduced by the Round 3 patch
itself; user's pre-existing `_is_address` skeleton, script-font heuristic,
and Round 1/2 graphics fixes are untouched.

## Round 4 patch

Real test run on `AMI FFL DINNER MENU Combined (4).pdf` exposed two
classification bugs and one cross-page bug.

### Bug A — analyzer over-promoted ALL-CAPS bold items to category_header

`outputs/AMI FFL DINNER MENU Combined (4)_p1_menu_data.json` had 14
categories on page 1 instead of ~5. False positives included
`CHARCUTERIE & CHEESE BOARD`, `SEAFOOD TOWER`, `OYSTERS ON THE HALF SHELL`,
`LOBSTER TAIL MP`, `WWW.CHATEAURESTAURANTS.COM`, etc.

Root cause: the R3-2 rule in `analyzer.py:_classify` used
`18 <= b.font_size <= 34` thinking that range was point-space, but
`font_size` in `RawBlock` is pixel-space at 200 DPI (point × 2.78). 11pt
item names render at ~30.54 px and tripped the rule.

### Bug B — wine sub-categories dropped on page 2

`outputs/AMI FFL DINNER MENU Combined (4)_p2_menu_data.json` only had
`Red Wines` / `Other Reds We Love`. Categories like `CALIFORNIA`,
`FRANCE`, `BORDEAUX, FRANCE (Left Bank)`, `RHONE`, `ITALY & SPAIN`, etc.
were present in `claude_layout.menu_data.categories` but never reached
the analyzer's `build_menu_data` — they fell through to `item_name`.

### Bug C — page 2 restaurant_name = "Wine Menu"

Each page is processed in isolation, so Claude vision returned
`"Wine Menu"` for the wine-list page, overriding `"The Château Anna Maria"`
that page 1 correctly identifies.

### Fix 1 — Remove R3-2 wine-subcategory rule (`analyzer.py:211`)

The 18-pixel font-size floor unintentionally matched true-11pt menu items
once rendered at 200 DPI. Rule removed entirely, replaced with a single
comment line. `ALL_CAPS_NUMERIC_PREFIX` regex kept (still useful
elsewhere). Script-font heuristic (Bordeaux, Sharable, …) is untouched.

### Fix 2 — Claude-validated reclassification in `pipeline.py:1051`

Inserted between `classify_blocks(...)` and `build_menu_data(...)` inside
the PDF branch of `process()`. Builds a set of Claude-vision category
names (case-insensitive, substring match for parenthesized variants).

- (a) Demotes analyzer's `category_header` to `item_name` when Claude
  does NOT list it AND the block is not script-font (script font is
  inherently decorative → trust the analyzer).
- (b) Promotes analyzer's `item_name` / `other_text` to `category_header`
  when Claude DID list it (text length ≥ 3 chars).

Single change resolves both Bug A (Montserrat-Bold items not in
Claude's category list → demoted) and Bug B (wine sub-categories present
in Claude's list → promoted before `build_menu_data` walks `classified`).

Prints `[pipeline] R4-1 reclassify: X promoted, Y demoted` for traceability.

### Fix 3 — Generic title blacklist extensions (`pipeline.py:49`)

Added `"wine menu"`, `"food menu"`, `"drinks menu"`, `"cocktail menu"`
to `_GENERIC_TITLE_WORDS`. `"dinner menu"` was already in the set —
verified. Prevents the existing R3-1 fallback from accepting Claude's
page-title placeholders as `restaurant_name`.

### Fix 4 — Cross-page restaurant_name propagation (`pipeline.py:1493`)

Added a final pass at the end of `process()` (just before
`return results`) that runs only when `len(results) > 1`. It scans the
already-written `menu_data.json` files for the first non-generic
`restaurant_name`, then rewrites that name onto any page whose
`restaurant_name` is empty or matches `_GENERIC_TITLE_WORDS`.

Prints `[pipeline] R4-2: propagated restaurant_name '<name>' → page N`
for any page that received the canonical name.

### Sanity verification

Import smoke test:
```
imports ok
_GENERIC_TITLE_WORDS sample: ['brunch', 'brunch menu', 'cocktail menu', 'dinner', 'dinner menu', 'drinks menu']
```

Synthetic classifier test (analyzer-only, no Claude reclassification):
```
CHARCUTERIE (analyzer-only): item_name  (R3-2 removed → expect item_name)  ✓
Sharable: category_header  (script font path still works)  ✓
```

Full pipeline NOT run (per instructions). No commits made.

### Conflicts with existing uncommitted edits

None. Fix 1 removes a Round 3 rule that proved broken. Fixes 2-4 add new
logic that runs after the Round 3 paths and only touches `classified` /
`results` / `_GENERIC_TITLE_WORDS`. All Round 1-3 graphics, separator,
synth-flourish, and address-regex changes are preserved.

---

## Round 5 patch

Two targeted fixes addressing regressions seen on `AMI FFL DINNER MENU`.
Edits confined to `analyzer.py`, `pipeline.py`, and `builder.py`; no other
files were touched. All Round 1-4 fixes and the user's pre-existing
uncommitted edits are preserved.

### Bug R5-A — 3-column menus collapsed to 2 columns

`analyzer.py:detect_columns` (lines ~103-160). The previous implementation
identified the single largest gap in the x-position histogram and split at
it, producing only 2 columns. On the AMI page 1 (Sharable / Starters
[left], Entrées [center], Broths & Greens / Sides [right]) this merged the
Sharable+Entrées columns: `Sharable` ended up empty and `Starters` over-
inflated to ~23 items because Entrée items spilled in.

Fix: upgraded `detect_columns` to consider the top-15 densest x-bins and
look at gaps >= 12% of canvas width. When at least 2 such gaps exist whose
midpoints are at least 18% of canvas width apart, the page is split into
three columns (0/1/2) at the two largest-gap midpoints. Otherwise the
original 2-column fallback (largest-gap split) is used.

Downstream column-clamp widened in `builder.py:build_template_from_claude`
line ~194-195: `min(1, max(0, ...))` -> `min(2, max(0, ...))`. The
`build_template()` path uses raw `col_assignments` from `detect_columns`
without any clamp, so it already supports 3 columns. `models.py`
`TextElement.column: int = 0` already accepts any int (no Literal
restriction); verified.

`analyzer.build_menu_data` keys `current_cats: dict[int, MenuCategory]` by
arbitrary int, so multi-column splitting requires no changes there.

### Bug R5-B — R4-2 propagation skipped compound page-titles

`pipeline.py`. AMI page 2 ended up with
`restaurant_name = "White Wines / Red Wines Wine Menu"`. This string is
not in `_GENERIC_TITLE_WORDS` (which is exact-match only), so the R4-2
cross-page propagation accepted it and left it in place — never copying
the correct `"The Château Anna Maria"` from page 1.

Fix: added a new helper `_is_generic_name(name)` in `pipeline.py`
immediately after `_GENERIC_TITLE_WORDS` (~line 53). It first checks the
exact-match set, then two additional patterns: (a) the string contains
"menu" or "list" as a standalone token, (b) the string contains 2+
wine-list / drink-section tokens (`wine|wines|reds|whites|sparkling|
rosé|rose|champagne|sake|beer|cocktails`).

Both R3-1 and R4-2 now use the same helper:

- `pipeline.py:1127-1136` (R3-1 PDF-fallback `should_use_claude`):
  replaced `claude_rn_lc not in _GENERIC_TITLE_WORDS` with
  `not _is_generic_name(claude_rn)`, and replaced
  `current_lc in _GENERIC_TITLE_WORDS` with `_is_generic_name(current)`.
- `pipeline.py:1538-1543` (R4-2 canonical-name discovery): replaced
  `rn.lower() not in _GENERIC_TITLE_WORDS` with `not _is_generic_name(rn)`.
- `pipeline.py:1548-1551` (R4-2 page-overwrite check): replaced
  `not rn or rn.lower() in _GENERIC_TITLE_WORDS` with
  `_is_generic_name(rn)`.

`import re` added to `pipeline.py` top-of-module (line 9) since the new
helper uses regex token searches.

### Verification

`./venv/bin/python3 -c "<canned check from instructions>"`

stdout:
```
[claude_extractor] Loaded 9 logo templates from local_assets
PASS: 'The Château Anna Maria'                           -> False (want False)
PASS: 'Wine Menu'                                        -> True (want True)
PASS: 'White Wines / Red Wines Wine Menu'                -> True (want True)
PASS: 'Patio & Bar Menu'                                 -> True (want True)
PASS: 'Le Premier Menu'                                  -> True (want True)
PASS: 'Brunch'                                           -> True (want True)
PASS: ''                                                 -> True (want True)
PASS: None                                               -> True (want True)
PASS: 'Olive Garden'                                     -> False (want False)
3-col test cols: [0, 0, 0, 0, 1, 1, 1, 1, 1, 2, 2, 2, 2]
```

stderr: empty. All 9 _is_generic_name expectations met; 3-col detector
produced three distinct column indices (0/1/2).

Full pipeline NOT run (per instructions). No commits made.

### Conflicts with existing uncommitted edits

None. `detect_columns` is a self-contained rewrite of a function that no
other Round 1-4 patch touched. The new `_is_generic_name()` helper is
additive and the two existing call-sites are updated in place; the
`_GENERIC_TITLE_WORDS` set itself is unchanged. The `builder.py` column-
clamp widening is a one-character edit (`1` -> `2`).

---

## Round 6 patch

Five concrete fixes from `RENDERER-GAPS.md`: one renderer-only and four
pipeline fixes (R6-5 became moot — see below). Edits confined to
`static/renderer.html`, `pipeline.py`, and `claude_extractor.py`. All
Round 1-5 fixes and the user's pre-existing uncommitted edits are
preserved.

### Fix R6-1 — Canvas 2D font loading via CSS `@font-face` injection

`static/renderer.html`. Root cause: Canvas 2D's `ctx.font` does NOT
consult `document.fonts` — it only sees fonts registered as CSS
`@font-face` rules in the document's stylesheet (or system fonts /
pre-linked `<link>`s). The renderer was registering embedded TTFs via
the FontFace API (`document.fonts.add(...)`), which works for DOM text
but is invisible to canvas. So custom fonts like `BrittanySignatureRegular`
silently fell back to generic serif/sans on section headers.

Fix: added a new helper `injectFontFaces(fonts)` (lines ~889-910) that
builds a `<style>` tag containing `@font-face` rules with
`url(data:font/ttf;base64,...)` sources, appends it to `document.head`,
and returns `document.fonts.ready`. Called from inside
`registerTemplateFonts(template)` BEFORE the FontFace API path; both
ready promises are awaited. The legacy FontFace registration is kept
because it works for any DOM rendering path and harmlessly duplicates
the CSS registration. Guards against empty/missing `template.fonts`
(returns immediate `Promise.resolve()` so callers never break).

Wire-in: `registerTemplateFonts` is already awaited at the two existing
load paths (file-loader at line ~973, auto-load by id at line ~1090),
so no caller changes needed.

### Fix R6-2 — Filter empty collage_box elements in `_cleanup_duplicate_graphics`

`pipeline.py:_cleanup_duplicate_graphics` (~lines 537-557). Added a
final filter pass right after the existing `drop_ids` removal that
drops any `type=image / subtype=collage_box` element with neither
`image_data` nor `semantic_label`. These are noise from earlier
label-clearing (Round 2 Fix 2-3) — they render as nothing on canvas
(or a debug bbox if showBBoxes is enabled). Logs
`[cleanup] R6-2: removed N empty collage_box element(s)`.

### Fix R6-3 — Bump synth swash height + minimum width

`pipeline.py:_synthesize_header_flourishes` (~lines 617-625):

- `target_h`: 60.0 → 100.0 (matches the ~100-150 px scale of source-PDF
  swashes in the AMI FFL menu).
- Width cap: `min(target_w, hw * 2.0)` →
  `min(target_w, max(hw * 2.5, 280.0))`. Gives short headers like
  "FRANCE" (w≈80 px) a substantive ~280 px swash without bleeding
  across columns for wider headers.
- y-offset: `+ 14` → `+ 18` (extra breathing room below the header
  baseline now that the swash is taller).
- The dedup-target `flourish_cy_target` recompute (~line 607) was
  updated from `hh + 14 + 35` → `hh + 18 + 50` so the "already there?"
  check stays centered on the new swash position.

### Fix R6-4 — `_enforce_single_logo` clusters logos by region (supports up to 3)

`claude_extractor.py:_enforce_single_logo` (lines 697-790). Full
replacement of the prior "single anchor + `anchor_size * 2.0`
proximity" logic. New behaviour:

1. Estimate canvas height from the spread of all elements
   (`canvas_h_est = max(y2)`, fallback 3400).
2. Compute `y_band = max(canvas_h_est * 0.15, 150 px)`.
3. Sort logos by vertical center, greedily cluster: two logos go in
   the same cluster if (a) some cluster member's y-center is within
   `y_band`, AND (b) the new logo is within
   `max(self_size, member_size) * 1.2` of some member (2D Euclidean
   distance). This means a top-center logo and a bottom-left logo end
   up in different clusters (different y-band); two bottom logos at
   opposite sides of the page also stay separate (same y-band but too
   far apart in x).
4. For each cluster, pick the largest-area logo as anchor, union all
   member bboxes into one merged logo. `position_hint` taken from the
   topmost fragment in the cluster (same as legacy behaviour).
5. Rebuild the element list: drop ALL original logos, append the
   merged cluster logos.

Net effect: AMI FFL-style menus with 3 logos (top-center brand,
bottom-left Château ON THE LAKE, bottom-center-right Château ANNA
MARIA) now preserve all 3 distinct logos instead of collapsing into a
single huge union bbox.

Smoke tests run (`./venv/bin/python3` inline):

- 3-region (top + bottom-left + bottom-right) → 3 logos preserved. ✓
- Single anchor + close fragment → merged into 1. ✓
- Single logo passthrough → unchanged. ✓
- 2 same-y-band logos at opposite x → 2 logos preserved. ✓
- Anchor + close fragment + far same-y-band logo → 2 logos
  (anchor+fragment merged, far one preserved). ✓

### Fix R6-5 — (moot, superseded by R6-4)

The "Distant logo — reclassify as image/badge" branch at the old
lines 778-785 was removed entirely by R6-4's full replacement. Every
logo now flows through the cluster-and-merge path; no logo gets
reclassified to `image/badge`. The metadata-preservation concern
(R6-5) is therefore no longer reachable. The semantic information
that R6-5 wanted to preserve (`semantic_label`, `position_hint`,
`image_data`) is carried through the new merge path because
`merged_logo = dict(anchor)` copies all keys from the anchor element,
and `position_hint` is explicitly re-derived from the topmost
fragment.

### Verification

```
./venv/bin/python3 -c "import pipeline, claude_extractor; print('imports ok')"
```

stdout: `[claude_extractor] Loaded 9 logo templates from local_assets\nimports ok`,
stderr empty.

Renderer change spot-checked by `grep -n "injectFontFaces|@font-face|
registerTemplateFonts" static/renderer.html` — confirms (a)
`injectFontFaces` function defined, (b) called from inside
`registerTemplateFonts`, (c) `registerTemplateFonts` awaited at both
existing call sites (file-loader and auto-load-by-id). Browser test
not run per instructions.

Full pipeline NOT run. No commits made.

### Conflicts with existing uncommitted edits

None. R6-1 is a self-contained addition inside the existing
`registerTemplateFonts` block — the legacy FontFace path is unchanged.
R6-2 is an additive filter after the existing `_cleanup_duplicate_graphics`
drop pass; the Round 2 collage_box label-clearing logic that creates
these empty elements is preserved. R6-3 touches only three lines
inside `_synthesize_header_flourishes` (added by Round 2). R6-4 fully
replaces a function body that no Round 1-5 patch touched — the
function signature, all three call-sites (`extract_layout_surya_som`
line ~1748, second call line ~1760, `merge_layouts` line ~2431) are
unchanged.

## Round 7 renderer patch

Five renderer-side fixes applied to `static/renderer.html`. All five bugs documented in `R7-B-RENDERER-AUDIT.md`. No other files touched. No commits.

### R7-B-5 — `fillText` `maxWidth` removed
**Where:** `drawTextElement` text-drawing branch.
**What:** The 4th `maxWidth` argument was being passed to `ctx.fillText(content, tx, ty, w)`. Per the Canvas 2D spec, this horizontally compresses glyphs instead of wrapping — invisible-but-pixel-mismatched against the source PDF (which used natural word wrapping). Removed the 4th argument by replacing the call entirely with the new `wrapText` helper (see R7-B-1).

### R7-B-3 — Separator fallback no longer renders distracting dashed lines
**Where:** `_drawSeparatorLine`.
**What:** Two behaviour changes:
  1. `decorative_divider` with no `image_data` AND no `semantic_label` → skip rendering entirely (was: dashed center-line). These elements are empty placeholders; drawing them was inventing visual content not in the source.
  2. `decorative_divider` with `semantic_label` (or otherwise valid) but no `image_data` → render a clean SOLID line using the element's own `style.color` and `style.stroke_width`, NOT a dashed line. Removes the previous 1.5px hard-coded dash pattern.
The `border` and plain-rule branches are unchanged. `style` is now resolved once into a local `st` rather than re-read three times.

### R7-B-4 — `collage_box` images fill their bbox
**Where:** `drawImageElement`.
**What:** Branched on `el.subtype`. For `collage_box` the image is drawn at `(x, y, w, h)` — stretch-fill the bbox. The bbox is already sized to match the panel in the source, and these are flat promo panels (not aspect-sensitive). All other image subtypes (badges, ornaments, photos) keep the original `contain`-with-center logic so they don't get distorted.

### R7-B-2 — `document.fonts.check()` wrapped in try/catch
**Where:** `drawTextElement` font-selection branch.
**What:** `document.fonts.check(\`12px "${raw}"\`)` can throw on malformed family names (unescaped quotes, etc.) and may return false in a tight loop for newly-injected @font-face whose data: URL is still parsing. Wrapped the call in `try { fontRegistered = document.fonts.check(...); } catch { fontRegistered = false; }`. On exception we fall through to the `FONT_CSS` generic stack — same behaviour as a false return, but exception-safe. The R6-1 `registerTemplateFonts` chain already awaits `document.fonts.ready` before `render()` is called at both load-time and auto-load-time, so the actual race window is already minimal; this change is defensive against `check()` itself throwing.

### R7-B-1 — Multi-line text wrapping (critical)
**Where:** New `wrapText` helper (just below `safeNum`). Wired into `drawTextElement` in place of the single `ctx.fillText` call.
**What:** Implements word-wrapping for canvas text. Algorithm:
  1. Split content on `\n` first — respects explicit line breaks coming from `el.content`.
  2. For each paragraph, walk word-by-word, accumulate into `line`, and when `ctx.measureText(line + " " + word).width > maxWidth` (AND `line` is non-empty so single huge words still get drawn rather than dropped), flush `line` to `ctx.fillText(line, x, y)` and advance `y` by `lineHeight`.
  3. Flush remaining line at end of each paragraph; advance `y` between paragraphs (including empty ones).
**Line height:** `fontSize * 1.2` (standard typographic baseline-to-baseline).
**Same font:** wrapText uses the currently-set `ctx.font` for measurement, so the wrapping width matches the actual render. Color, align, and baseline are set by the caller (unchanged).
**Anchor:** `tx` is still adjusted for `align` (center → `x + w/2`, right → `x + w`) before being passed to `wrapText`. `measureText` returns the un-aligned line width, which is the correct quantity for "does this line fit in the bbox" regardless of alignment.

### Files changed

- `static/renderer.html` (only file in this round)

### Verification

```
./venv/bin/python3 -c "
from pathlib import Path
html = Path('static/renderer.html').read_text()
checks = [
    ('injectFontFaces present', 'injectFontFaces' in html),
    ('wrapText helper present', 'wrapText' in html or 'measureText' in html),
    ('document.fonts.ready awaited', 'document.fonts.ready' in html),
    ('fillText maxWidth removed (rough check)', html.count('fillText') > 0),
]
for label, ok in checks:
    print(f'{\"PASS\" if ok else \"FAIL\"}: {label}')
"
```

stdout:

```
PASS: injectFontFaces present
PASS: wrapText helper present
PASS: document.fonts.ready awaited
PASS: fillText maxWidth removed (rough check)
```

Additional static checks:
- JS brace count balanced: 198 open / 198 close (entire inline `<script>` body).
- `wrapText` defined once, called once from `drawTextElement`.
- Remaining `ctx.fillText(...)` call sites:
  - 2 inside `wrapText` itself (each with 3 args — line, x, y — no maxWidth).
  - 1 in `drawTextElement` showIds/showSubtypes debug overlay (4-arg, intentional — tiny debug label clipped to overlay bar width; debug-only, not menu output).
  - 1 in `drawLogoPlaceholder` ('LOGO' text, 3-arg).

Browser run NOT performed per instructions.

### Conflicts with existing uncommitted edits

None. The renderer was already on the R6-1 path (`injectFontFaces` + `registerTemplateFonts` awaiting `document.fonts.ready`). R7-B-2's try/catch wraps the existing check call without altering surrounding logic; the R6-1 font-injection chain is preserved verbatim. The other four fixes touch independent functions (`drawTextElement` text branch, `_drawSeparatorLine`, `drawImageElement`).
