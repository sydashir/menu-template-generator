# QA Report — Round 3 menu_data fixes

Reviewer: independent QA pass. Source edits NOT made (suggestions only).
Files audited: `analyzer.py`, `pipeline.py` (vs `HEAD`).

## Verdict per fix

- **R3-1 (Claude restaurant_name fallback): PASS.**
  `pipeline.py:1047-1077` is inside the `if ext in SUPPORTED_PDF:` branch
  (opened at `pipeline.py:1016`) and runs immediately after `menu_data =
  build_menu_data(...)` at `pipeline.py:1039-1045`, before
  `build_template(...)` at `pipeline.py:1079`. It guards on `claude_layout is
  not None`, rejects Claude's pick if it matches a detected category header
  (case-insensitive), and overrides on three documented scenarios: analyzer
  empty, analyzer == category, analyzer in the generic-title whitelist. The
  `MenuData` Pydantic model permits mutation (the existing image branch
  mutates the same field elsewhere). One latent risk noted below ("Brunch"
  edge case).

- **R3-2 (Wine sub-categories — ALL CAPS 14-34pt rule): PARTIAL.**
  The rule (`analyzer.py:212-224`) fires correctly for `SPARKLING WINE`,
  `FRANCE`, `CALIFORNIA`, `ROSÉ`, `ADDITIONAL WHITES`, `RHONE`, AND for
  `224 Pinot Noir, Pike Road, OR` / `164- LAURANT PERRIER SPLIT` (both
  correctly excluded via `ALL_CAPS_NUMERIC_PREFIX` and price-tail). The
  `BORDEAUX, FRANCE (Left Bank)` case is FAIL — confirmed by synthetic
  test, root-cause analysis below. Also flags a serious regression: the
  rule false-positives on ordinary ALL CAPS bold item names (`FILET MIGNON`,
  `BAKED BRIE EN CROUTE` without inline price) — see "Regression risks".

- **R3-3 (Orphan item_name drop): PASS for the stated goal, BUT with a
  silent-failure regression for menus without detected categories.**
  `analyzer.py:302-306` swaps the old `MenuCategory(name="General", ...)`
  fallback for `continue`. Synthetic test confirms "Dinner Menu" is dropped
  while legitimate items under `Starters` are preserved. The regression
  risk: every item is silently dropped when no `category_header` was
  detected on the page at all — see "Regression risks".

- **R3-4 (Wine vocab blacklist in `_is_address`): PASS for the stated
  goal, MINOR regression on `PO Box` form.**
  `analyzer.py:71-97` correctly rejects all five wine-list cases in the
  test set and accepts canonical street addresses. One existing test case
  (`PO Box 123, Anytown, CA 90210`) now returns `False` — the tight regex
  requires a street keyword, and the loose `ADDRESS_RE` also requires a
  street keyword (`\b(street|st\.?|...)\b`), so `PO Box` lines without an
  explicit street keyword fail both branches. This is not a regression
  from this round (the loose regex was already missing `PO Box`), but the
  task spec calls it `True`. Reported as a pre-existing bug, NOT a R3-4
  defect — though worth tracking.

## The Bordeaux case — one-line patch

**File: `analyzer.py:212-213`**

Current code (the strip pattern preserves interior letters of parens,
including the lowercase "eft" / "ank" in "Left Bank", breaking
`isupper()`):

```python
stripped_alpha = re.sub(r"[\s$/.,\-\d&()]", "", text)
is_all_caps = len(stripped_alpha) >= 3 and stripped_alpha.isupper()
```

Replace with:

```python
# Strip parenthesized annotations first, then non-alpha
no_parens = re.sub(r"\([^)]*\)", "", text)
stripped_alpha = re.sub(r"[^A-Za-zÀ-ÿ]", "", no_parens)  # keep only letters (incl. accented)
is_all_caps = len(stripped_alpha) >= 3 and stripped_alpha.isupper()
```

Verified independently in the REPL:
- Current → `stripped='BORDEAUXFRANCELeftBank'`, `isupper()=False` → falls through to `item_name`.
- Fixed → `stripped='BORDEAUXFRANCE'`, `isupper()=True`, len=14 → returns `category_header`.

## Regression risks

- **R3-2 false-positives on ALL CAPS bold item names.** The new rule at
  `analyzer.py:216-224` has no positional / column-width / `has_inline_price-anywhere`
  guard. Any bold ALL-CAPS item line whose font_size is 14-34pt (i.e. the
  vast majority of bold-uppercase menu items) and that lacks a trailing
  price token is now classified as `category_header`. Synthetic test
  result:
  - `'FILET MIGNON'` (14pt, bold) → `category_header` (BAD — wanted `item_name`)
  - `'PAN SEARED HALIBUT'` (16pt, bold) → `category_header` (BAD)
  - `'NEW YORK STRIP STEAK'` (14pt, bold) → `category_header` (BAD)
  - `'BAKED BRIE EN CROUTE 18'` (14pt, bold) → `item_name` (OK — saved by `PRICE_TAIL_RE`)

  In real menus item prices are often in a separate text block (right-
  aligned column), so `PRICE_TAIL_RE` does not save those. **Suggested
  mitigation:** add a lower-bound floor on font ratio (e.g. require
  `font_ratio >= 0.35` AND `font_size >= 18`) OR require the block's
  width to be small (single-word/short headers don't span the column).
  Without this, any wine-style fix will regress non-wine menus.

- **R3-3 silently drops all items when no category is detected.** The
  spec acknowledges this. It happens when (a) all headers use a decorative
  script font in the header zone — covered by the script-font rule, OK —
  OR (b) the analyzer fails to fire either the 0.85 promotion or the
  script-font rule (e.g., an OCR/image-only page that mis-extracts the
  header) — in that case the entire page now drops to zero items in
  menu_data, instead of one "General" bucket. **Suggested safety valve:**
  if the loop reaches the end and `categories` is still empty AND we saw
  ≥1 `item_name` block during iteration, do a single second pass that
  re-injects them into an auto-created "General" — but only as last
  resort, after the loop completes. Track count of suppressed orphans and
  log at WARNING level.

- **R3-1 — "Brunch" overwrite risk: LOW but not zero.** The predicate at
  `pipeline.py:1063-1074` only accepts a Claude pick when the analyzer's
  value is empty / a category / in the generic-title whitelist. If Claude
  Vision misidentifies a section title as the restaurant_name (e.g.
  "Brunch" the word) and the analyzer's pick is also a generic title like
  "Patio & Bar Menu", the predicate will accept the Claude value — but
  the secondary guard `claude_rn_lc not in header_names` filters it if
  "Brunch" got classified as a category_header on the page. The risk is
  when Claude says "Brunch" AND no category_header equal to "brunch" was
  detected. The current whitelist doesn't include "brunch" as a Claude
  blacklist, so this could land. **Suggested mitigation:** apply the same
  generic-title set as a blacklist for `claude_rn_lc` too — symmetric
  filter on both sides.

- **R3-4 — `PO Box 123, Anytown, CA 90210` returns False.** Already
  flagged above. The tight regex requires a street keyword; the loose
  `ADDRESS_RE` (`analyzer.py:12-17`) also requires one. PO Box / Rural
  Route / Apt-only addresses fail. Not a regression from R3-4 specifically
  but listed for completeness.

- **R3-4 — substring leak in `_looks_like_wine_entry`.** The helper does
  `w in lower` (raw substring) for every token in `_WINE_VOCAB`. Words
  like `"rose"` will substring-match `"rosewood drive"`, `"sonoma"` may
  match a person's last name, etc. The `w in lower.split()` branch
  protects most cases but the OR with `w in lower` defeats it. **Suggested
  mitigation:** drop the `w in lower` arm; rely only on `w in lower.split()`
  with punctuation-stripped tokens. Or use `\b` regex.

## Defects requiring code change before re-running the pipeline

- **(blocker)** R3-2 ALL-CAPS-bold-item-name regression — needs a
  positional / size / width guard so that ordinary item names like
  `FILET MIGNON` (14pt, bold, no inline price) aren't promoted to
  `category_header`. See suggestion in "Regression risks".
- **(must-fix)** The Bordeaux case — apply the one-line patch above
  at `analyzer.py:212-213`.
- **(should-fix)** R3-3 safety valve so menus without any detected
  category don't end up with zero items in menu_data.
- **(nice-to-have)** R3-1 add generic-title blacklist for Claude's pick
  (symmetric filter).
- **(nice-to-have)** R3-4 tighten `_looks_like_wine_entry` substring
  match (drop the raw-substring branch).

## Test results

### R3-4 (`_is_address`):

```
PASS: '224 Pinot Noir, Pike Road, OR' -> False (want False)
PASS: '605A Château Les Pagodes de Cos, St. Estèphe, 2014' -> False (want False)
PASS: '5325 Marina Dr ~ Holmes Beach' -> True (want True)
PASS: '123 Main St, Boston, MA 02101' -> True (want True)
FAIL: 'PO Box 123, Anytown, CA 90210' -> False (want True)
PASS: '250 Domaine De La Solitude, Rhone Valley FR' -> False (want False)
PASS: '100 Chardonnay, The Calling, Sonoma Coast CA' -> False (want False)
```

6/7 PASS. The PO-Box miss is a pre-existing gap in the loose `ADDRESS_RE`,
not a R3-4 regression.

### R3-2 (`_classify` wine sub-categories):

```
PASS: 'SPARKLING WINE'              -> category_header    (want category_header)
PASS: 'FRANCE'                      -> category_header    (want category_header)
PASS: 'CALIFORNIA'                  -> category_header    (want category_header)
PASS: 'ROSÉ'                        -> category_header    (want category_header)
PASS: 'ADDITIONAL WHITES'           -> category_header    (want category_header)
PASS: 'RHONE'                       -> category_header    (want category_header)
FAIL: 'BORDEAUX, FRANCE (Left Bank)' -> item_name         (want category_header)
PASS: '224 Pinot Noir, Pike Road, OR' -> item_description (want item_name|item_description)
PASS: '164- LAURANT PERRIER SPLIT'  -> item_name          (want item_name|item_description)
PASS: 'CHARCUTERIE & CHEESE BOARD'  -> category_header    (want item_name|category_header)
```

9/10 PASS. BORDEAUX fix is documented above. Additional regression test
(ALL CAPS bold item names, 14-16pt, NO inline price):

```
'BAKED BRIE EN CROUTE'    -> category_header    (BAD — wanted item_name)
'FILET MIGNON'            -> category_header    (BAD)
'PAN SEARED HALIBUT'      -> category_header    (BAD)
'NEW YORK STRIP STEAK'    -> category_header    (BAD)
'BAKED BRIE EN CROUTE 18' -> item_name          (OK — saved by PRICE_TAIL_RE)
```

### R3-3 (orphan `item_name` drop in `build_menu_data`):

```
  item_name          'Dinner Menu'
  category_header    'Starters'
  item_name          'BAKED BRIE EN CROUTE 18'
Categories: [('Starters', ['BAKED BRIE EN CROUTE'])]
```

`'Dinner Menu'` correctly absent from menu_data. `'Starters'` retains its
one item. PASS.

### R3-1 (wire-up audit, no synthetic test):

- Inside `if ext in SUPPORTED_PDF:` branch (opens `pipeline.py:1016`).
- Runs after `menu_data = build_menu_data(...)` (`pipeline.py:1039-1045`),
  before `build_template(...)` (`pipeline.py:1079`).
- `should_use_claude` predicate at `pipeline.py:1063-1074` covers the
  four MENUDATA-ROUND3.md scenarios: empty analyzer pick, analyzer
  picked a category, analyzer picked a generic title word, analyzer
  picked a section title (covered via the whitelist + `current_lc in
  header_names` check).
- Mutates `menu_data.restaurant_name` at `pipeline.py:1077` — Pydantic
  models in this codebase allow attribute mutation (verified by the
  pattern at the image-branch fallback which mutates the same field).
- One latent risk: no symmetric blacklist for `claude_rn_lc` (see
  Regression risks above).

PASS.

## Overall: **YELLOW**

R3-1, R3-3, R3-4 all pass their stated goals. R3-2 partially works —
the seven simple ALL-CAPS sub-category cases pass, but (a) the
implementer's known-defect `BORDEAUX, FRANCE (Left Bank)` is unfixed
(one-line patch supplied above), and (b) the rule has a serious false-
positive footprint on ordinary ALL-CAPS bold item names without inline
price. Both need addressing before re-running the pipeline on the
existing PDF corpus, or page-2 wine sections will look right while
page-1 entrées get re-shuffled into spurious category headers.
