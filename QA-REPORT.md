# QA Review — Decorator Placement Fixes

Scope of review: only the three fixes claimed in `FIX-LOG.md`. Pre-existing
uncommitted edits in `analyzer.py`, Surya device strategy in
`claude_extractor.py`, and tool-use schema additions are NOT reviewed.

## Verdict per fix

- **Fix 1: PARTIAL** — `_snap_graphic_decorators` (`claude_extractor.py:2195-2339`)
  implements gap snapping for graphic decorators and is wired into both
  `extract_layout_surya_som` (`claude_extractor.py:1744`) and `merge_layouts`
  (`claude_extractor.py:2429`). The drop guard correctly preserves PyMuPDF vector
  separators (`claude_extractor.py:2249-2250`), unlabeled elements with no
  available gap are dropped, and the target predicate correctly matches the spec
  (image/separator with `ornament/`/`separator/` label or `ornament`/
  `decorative_divider` subtype). However, the MOVE branch (lines 2329-2337) is
  NOT gated by `_droppable`, so any "target" element — including one that was
  spatially correct to begin with — can be moved if a LARGER gap exists nearby.
  Combined with the open-top/open-bot pseudo-gaps appended at lines 2293-2302,
  this means an ornament correctly placed in a real 80 px inter-content gap can
  be yanked into a 92 px open margin above the cluster simply because it is
  marginally larger. Synthetic test (CASE 2) confirms this: ornament at y=150
  in a real 80 px gap was moved to y=46 because the open-top margin was 92 px.
  This contradicts the FIX-LOG smoke-test claim "Ornament inside a real gap →
  left alone" and the spec bullet "If the element's vertical center already
  sits in that gap, leave it" — the "that gap" in the implementation is the
  largest, not the containing gap.

- **Fix 2: PASS (with caveat)** — Call site at the original `pipeline.py:902–929`
  is properly removed and replaced with an explanatory comment block at
  `pipeline.py:935-940`. The function definition is preserved with a single-line
  `# DEPRECATED (Fix 2): …` comment at `pipeline.py:118`, body untouched. The
  `_cleanup_duplicate_graphics` threshold tightening (`pipeline.py:384-403`)
  drops the neighbour count to `>=1` and the y-distance to `<30 px`, with an
  exemption for `semantic_label.startswith("separator/")`
  (`pipeline.py:394-396`). The exemption is correctly scoped (separator/* only,
  not ornament/*) and does not re-open the phantom-ornament hole. Caveat: the
  threshold tightening also drops legitimate `ornament/*`-labelled flourishes
  that sit within 30 px of a section header — see Regression risks #1.

- **Fix 3: PASS** — `_enrich_template_separators_from_claude` at
  `pipeline.py:268-282` correctly filters claude sources by rejecting
  `subtype=="collage_box"` and requiring either `type=="separator"` OR
  (`type=="image"` AND label starts with `separator/`/`ornament/`). Tolerances
  tightened to `_TOL_Y=25`, `_TOL_X=80` (`pipeline.py:286-287`). Orientation
  guard at `pipeline.py:300-317` reads `orientation` from the element or
  infers from aspect, and rejects mismatches. Synthetic test confirmed:
  vertical PDF separator near a horizontal Claude ornament no longer receives a
  label. The collage_box label-clearing in `_inject_pdf_graphics` at
  `pipeline.py:599-602` clears `ornament/*`/`separator/*` labels but keeps the
  element appended with `semantic_label=None`, matching the spec.

## Regression risks identified

1. **`_cleanup_duplicate_graphics` may drop legitimate ornaments under section
   headers.** With the new threshold (`pipeline.py:398-402`), an
   `image/ornament` whose center sits within 30 px of ANY 1 text element is
   dropped. Headers count as text elements, so an `ornament/*`-labelled
   flourish placed directly under a header (typical decorative pattern) gets
   wiped out. The exemption at `pipeline.py:394-396` only protects
   `separator/*` labels, not `ornament/*`. To verify: run any menu where a
   header has a flourish immediately below it and check for missing ornaments.

2. **`_snap_graphic_decorators` MOVE branch can shift any labeled separator
   into a larger nearby gap, including spatially-correct ones.**
   (`claude_extractor.py:2324-2337`) The drop guard at `_droppable` only gates
   the DROP path, not the MOVE path. In practice this does NOT touch PDF vector
   separators (which live in `template.elements`, not `claude_layout` —
   `_snap_graphic_decorators` only operates on the latter), so the most acute
   risk does not materialise. However, Claude-produced separator elements with
   `separator/*` labels that landed at a legitimate y can be relocated to a
   larger open margin above/below the content cluster. Verify by reviewing the
   `[snap_graphic] move` log lines after a pipeline run — any move spanning
   more than ~100 px is suspect.

3. **Open-top/open-bot pseudo-gaps inflate the "largest gap" choice.**
   (`claude_extractor.py:2293-2302`) Adding the open space above the topmost
   block and below the bottommost block as if they were inter-content gaps
   biases ornaments toward page edges. A correctly-placed centered ornament
   between two content rows can be yanked to the top/bottom margin. Synthetic
   CASE 2 reproduces this exactly. To verify: any menu page that opens with a
   small text header followed by content will see a top-margin pull.

4. **`style: None` on a text element raises AttributeError in the new helper.**
   (`claude_extractor.py:2225`) `e.get("style", {}).get(...)` returns `None`,
   not `{}`, when the element has `"style": None`. Same pattern used elsewhere
   in `_snap_decorative_headers` (lines 2118, 2127) and is consistent with the
   codebase, so the risk is low — but synthetic test confirmed it crashes.
   If `_snap_graphic_decorators` is ever called on a list containing text
   elements that explicitly set `style: None` (rare but possible from
   `_dedup_text_elements` output or merged layouts), it will throw.

5. **`_snap_graphic_decorators` is NOT re-run after `_verification_pass`.**
   In `extract_layout_surya_som` at `claude_extractor.py:1753-1760`,
   `_snap_decorative_headers` and `_enforce_single_logo` are re-applied after
   verification but `_snap_graphic_decorators` is not. Any ornament added by
   the verification pass bypasses the snap. Minor — verification pass is rare
   to add graphics, but inconsistent with the design of the original snapping
   logic.

6. **Subtype/label transfer in `_inject_pdf_graphics` step 4 is unprotected.**
   At `pipeline.py:546-556` semantic_labels (and subtype) from Claude images
   are transferred onto hybrid graphics whenever bboxes overlap >40%. This
   path is independent of Fix 3's clearing logic in step 4b and could still
   propagate an `ornament/*` label from a collage_box-like source onto a
   hybrid graphic, although it's a different code path than the one Fix 3
   targeted. Likely out of Fix 3's stated scope but worth checking.

7. **FIX-LOG claim "Conflicts with existing uncommitted edits = None" holds.**
   The uncommitted Surya device strategy in `claude_extractor.py` lives in
   `_load_surya_models` and `extract_blocks_surya` (lines ~1053-1200), wholly
   disjoint from the new `_snap_graphic_decorators` (line ~2195). The
   uncommitted tool-use schema additions live in `_TOOL_SCHEMA` (lines
   ~273-313), also disjoint. The uncommitted `_cleanup_duplicate_graphics`
   Fix 4 (logo containment, `pipeline.py:428-457`) is appended after the
   threshold-tightening edit — they sit at different points in the same
   function and do not conflict. Confirmed.

## Defects requiring code change before user runs the pipeline

- **`claude_extractor.py:2329-2337` — gate the MOVE branch on `_droppable`.**
  As written, the MOVE branch runs on every target element regardless of
  label trust. Required change: wrap the MOVE branch with a check such that
  only `_droppable(el)` elements OR elements whose current cy is NOT within
  ANY existing gap (not just the largest) get moved. Otherwise an ornament
  legitimately placed in a real gap, but smaller than the open-top margin,
  will be uprooted. Conservative alternative: replace the "already inside
  largest gap?" check at line 2324 with "already inside ANY usable gap
  (`gap_h >= need`)?". This preserves elements that already sit inside an
  acceptable gap.

- **`claude_extractor.py:2293-2302` — exclude open-top/open-bot from the
  "largest gap" selection unless no inter-content gap is ≥ `element_h + 12`.**
  Currently the open-top/open-bot pseudo-gaps compete on equal footing with
  real inter-content gaps and can win on size alone. Required change: only
  fall back to them when `max(real_gaps) < need`. Without this, the
  "ornament correctly placed in gap" smoke test (CASE 2) regresses.

- **`pipeline.py:398-402` — broaden the cleanup exemption to also cover
  `ornament/*` labels under headers.** As written, ornaments under section
  headers are dropped. Required change: skip the drop if the nearest text
  neighbour is itself a category_header / decorative-script text, OR
  exempt the entire `ornament/*` palette. Otherwise a common design pattern
  (header + flourish 5-15 px below) is regressed.

## Tests run and results

- **Import check** —
  `./venv/bin/python3 -c "import pipeline; import claude_extractor; from claude_extractor import _snap_graphic_decorators; print('ok')"`
  Stdout:
  ```
  [claude_extractor] Loaded 9 logo templates from local_assets
  ok
  ```
  Stderr: empty. PASS.

- **Synthetic test for `_snap_graphic_decorators`** —
  Inputs: 3 text blocks (y=100, 200, 300 each h=20) plus five graphic
  candidates (ornament on text, ornament in real gap, PyMuPDF vector sep on
  text, oversized unlabeled image, separator with separator/* label on text).

  | Case | Expected | Actual | Result |
  |------|----------|--------|--------|
  | 1 ornament_bad on text | MOVED into a gap | moved y=200→160 | PASS |
  | 2 ornament in real 80px gap | LEFT alone | moved y=150→46 (yanked into open top) | **FAIL** |
  | 3 PyMuPDF vector sep on text | preserved (drop guard) | preserved at y=205 | PASS |
  | 4 big unlabeled (no gap fits) | DROPPED | dropped | PASS |
  | 5 separator/* labeled on text | preserved (not droppable) | moved into a gap | PASS |

  CASE 2 contradicts FIX-LOG smoke test #1 and the spec ("If the element's
  vertical center already sits in that gap, leave it"). See Fix 1 verdict.

- **Synthetic test — ornament in largest gap (no larger open margin)** —
  Confirms when the inter-content gap is genuinely the largest option, the
  ornament is preserved. PASS.

- **Synthetic test — orientation guard in
  `_enrich_template_separators_from_claude`** —
  Vertical PDF separator + horizontal Claude ornament near same xy →
  semantic_label NOT transferred. PASS.

- **Synthetic edge cases for `_snap_graphic_decorators`** —
  Empty content_blocks: PASS (no crash, element preserved).
  Missing bbox on element: PASS (no crash).
  `semantic_label=None`: PASS (no crash).
  `style: None` on text element: **FAIL** with AttributeError. Low real-world
  risk but unguarded path.

## Overall: YELLOW

Fix 2 and Fix 3 land cleanly and address their root causes. Fix 1 implements
gap snapping correctly for the BAD case (ornament on text) but over-corrects
for the GOOD case (ornament already in a real gap) because:
(a) the MOVE branch is not gated by trust, and
(b) open-top/open-bot margins compete with real gaps for "largest" status.

The combination means ornaments that the pipeline got right will be moved
into page margins on the next run — a different failure mode than the
original bug, but still a placement regression. Additionally, the
`_cleanup_duplicate_graphics` threshold tightening will collateral-damage
`ornament/*` flourishes sitting immediately under section headers.

Recommendation: address the three defects in the "Defects" section before
running the pipeline on real menus. None of them require new functions —
they are conditionals in existing logic. ETA < 30 minutes of edit work.
