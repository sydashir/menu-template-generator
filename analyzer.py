import re
from typing import List, Tuple, Optional

from models import RawBlock, SemanticType, MenuCategory, MenuItem, MenuData

# R19.4: footer detection — URLs / "est. YYYY" / city-list strings are not items.
_URL_RE = re.compile(r"(https?://|www\.)\S+|\b\w+\.(com|net|org|us|co|io)\b", re.I)
_ESTABLISHED_RE = re.compile(r"^est(\.|ablished)?\s+\d{4}\s*$", re.I)
_CITY_LIST_RE = re.compile(r"^[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*(?:\s*[~\-•·]\s*[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*){1,}\s*$")

# R20.3: As-Seen-On panel captions — "As seen on:", "Featured on:", and quoted
# show names like "SUMMER RUSH" / "FOY RUSH". These are panel labels, not menu
# items.
_ASO_HEADER_RE = re.compile(r"^(as\s+seen\s+on|featured\s+on)\s*[:\-]?\s*$", re.I)
_QUOTED_SHOWNAME_RE = re.compile(r"^[\"“‘'][\w\s&\-,.]{2,40}[\"”’']\s*$")


def _is_footer(text: str) -> bool:
    t = text.strip()
    if _URL_RE.search(t) or _ESTABLISHED_RE.match(t) or _CITY_LIST_RE.match(t):
        return True
    # R20.3: panel captions are not items.
    if _ASO_HEADER_RE.match(t) or _QUOTED_SHOWNAME_RE.match(t):
        return True
    return False


PRICE_RE = re.compile(r"^\$?\s?\d{1,4}(?:[./]\d{1,4})?(?:\.\d{2})?$")
PRICE_TAIL_RE = re.compile(r"\s+(\$?\s?\d{1,4}(?:[./]\d{1,4})?(?:\.\d{2})?)$")
PHONE_RE = re.compile(r"[\+\(]?[1-9][0-9 \.\-\(\)]{7,}[0-9]")
# Use word-boundary regex — old substring set matched "ave" inside "have",
# "rd" inside "undercooked", flagging warnings & footers as addresses.
# Also require a number nearby so non-address text mentioning "street" doesn't trip.
ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+\w+.*\b(street|st\.?|avenue|ave\.?|blvd\.?|boulevard|"
    r"road|rd\.?|suite|ste\.?|drive|dr\.?|lane|ln\.?|way|court|ct\.?|"
    r"place|pl\.?|highway|hwy\.?|parkway|pkwy\.?)\b",
    re.IGNORECASE,
)
# Wine-vintage exclusion: wine list entries ("607 Château Beau-Site, St. Estèphe,
# 2017") contain ", 20YY" or ", 19YY" plus saint-abbreviations. Real addresses
# never end in a wine year. If we see one, it's a wine, not an address.
_WINE_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
# Saint-abbreviation exclusion: "St. Estèphe" / "St. Georges-St. Émilion" /
# "St. Tropez" — followed by a capitalized non-state word. State postal codes
# are 2 uppercase letters so we keep those (e.g. "123 Main St. Boston, MA").
_SAINT_FALSE_POS_RE = re.compile(
    r"\b(?:st|saint)\.?\s+[A-Z][a-zé-]+\b",
    re.IGNORECASE,
)


# Tight address pattern: NUMBER then 1-4 plain words then a street keyword,
# WITHOUT a comma between them. Catches "5325 Marina Dr" and "123 Main St" but
# rejects "224 Pinot Noir, Pike Road" where the number is the wine code and the
# street word comes after a comma.
_TIGHT_ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+(?:[A-Za-z][\w.'-]*\s+){0,4}"
    r"(street|st\.?|avenue|ave\.?|blvd\.?|boulevard|"
    r"road|rd\.?|suite|ste\.?|drive|dr\.?|lane|ln\.?|way|court|ct\.?|"
    r"place|pl\.?|highway|hwy\.?|parkway|pkwy\.?)\b",
    re.IGNORECASE,
)
_US_ZIP_RE = re.compile(r"\b[A-Z]{2}\s*\d{5}\b")

# R3-4: wine varietal + country/region words. If the candidate "address" contains
# any of these, it's a wine-list entry, not an address.
_WINE_VOCAB = frozenset({
    # Varietals
    "pinot", "noir", "cabernet", "sauvignon", "chardonnay", "merlot",
    "syrah", "shiraz", "riesling", "zinfandel", "malbec", "tempranillo",
    "sangiovese", "nebbiolo", "barbera", "viognier", "grenache", "mourvedre",
    "champagne", "prosecco", "cava", "rose", "rosé", "brut",
    "chablis", "burgundy", "bordeaux", "rioja", "chianti", "barolo",
    "amarone", "barbaresco", "beaujolais",
    # Regions / countries that show up as wine origins
    "france", "italy", "spain", "germany", "argentina", "australia",
    "chile", "portugal", "napa", "sonoma", "tuscany", "rhone",
    "alsace", "loire", "willamette", "marlborough",
})


def _looks_like_wine_entry(text: str) -> bool:
    """True if the text contains wine vocabulary that disqualifies it from being an address."""
    import re as _re
    # Tokenize on non-letter chars to get clean word boundaries (R3-4 follow-up:
    # the prior raw-substring branch matched "rose" inside "rosewood drive").
    tokens = {t for t in _re.split(r"[^A-Za-zÀ-ÿ]+", text.lower()) if t}
    return bool(tokens & _WINE_VOCAB)

# R3-2: wine-code prefix at the start of a line ("224 ", "164-"). When a line
# begins with a 2-4 digit wine code, it's not a category header.
ALL_CAPS_NUMERIC_PREFIX = re.compile(r"^\d{2,4}\b")


def _is_address(text: str) -> bool:
    """Address regex + wine/saint exclusions."""
    has_zip = bool(_US_ZIP_RE.search(text))

    # R3-4: wine-list entries like "224 Pinot Noir, Pike Road, OR" trip the tight
    # address regex. If the line contains a varietal or wine-region word AND no
    # real ZIP code, it's a wine entry.
    if _looks_like_wine_entry(text) and not has_zip:
        return False

    # Vintage years (1900-2099) → wine list entry, not an address (unless real zip).
    if _WINE_YEAR_RE.search(text) and not has_zip:
        return False

    # Tight match (no comma between number and street keyword) is a strong signal.
    if _TIGHT_ADDRESS_RE.search(text):
        # Saint-abbreviation false positive: tight pattern still allows "224 Pinot Noir, Pike Road"
        # if it matched a different keyword. But require either has_zip OR no saint-pattern.
        if _SAINT_FALSE_POS_RE.search(text) and not has_zip:
            return False
        return True

    # Loose match with zip = also an address ("PO Box 123, Anytown, CA 90210").
    if has_zip and ADDRESS_RE.search(text):
        return True

    return False


def detect_columns(blocks: List[RawBlock], canvas_w: float) -> List[int]:
    """Assign column index using x-position clustering. Supports 2 OR 3 columns.

    R5-A: previous version split at the single largest x-histogram gap, which
    collapsed 3-column menus (e.g. Sharable / Entrées / Sides) into 2 columns.
    Now: identify the densest x-bins, look at gaps between them; if two big
    gaps exist that are far enough apart, treat the page as 3 columns.
    Otherwise fall back to the original 2-column split.
    """
    if not blocks:
        return []

    import numpy as np

    x_lefts = np.array([b.x for b in blocks])
    hist, edges = np.histogram(x_lefts, bins=100, range=(0, canvas_w))
    smoothed = np.convolve(hist.astype(float), np.ones(5) / 5, mode="same")

    # Densest bin x-positions (top 15 — gives us room to see 3 clusters)
    top_idx = np.argsort(smoothed)[-15:]
    top_xs = np.sort(edges[top_idx])

    if len(top_xs) < 2:
        return [0] * len(blocks)

    # Gaps between consecutive dense-bin x-positions
    gaps = np.diff(top_xs)
    threshold = canvas_w * 0.12   # gap must be >= 12% of canvas to count

    big_gap_indices = [i for i, g in enumerate(gaps) if g > threshold]

    if not big_gap_indices:
        return [0] * len(blocks)

    # Try 3 columns when there are at least 2 big gaps
    if len(big_gap_indices) >= 2:
        # Take the 2 largest gaps, sort them in x-order to identify column boundaries
        two_biggest = sorted(big_gap_indices, key=lambda i: -gaps[i])[:2]
        # R7-extra: require the two competing gaps to be similar in magnitude
        # (within 50% of each other). For a genuine 3-col menu the two inter-col
        # gaps are roughly the same width; on a 2-col menu with description-
        # indented sub-clusters the second-largest gap is much smaller than the
        # main inter-col gap and we should NOT treat the page as 3-col.
        g_big = gaps[two_biggest[0]]
        g_second = gaps[two_biggest[1]]
        if g_second < g_big * 0.5:
            # Not a real 3-col layout; fall through to 2-col split.
            pass
        else:
            two_biggest_sorted = sorted(two_biggest)
            gi1, gi2 = two_biggest_sorted
            split_1 = top_xs[gi1] + gaps[gi1] / 2
            split_2 = top_xs[gi2] + gaps[gi2] / 2
            # Require the two splits to be far enough apart that we really have 3 columns
            if (split_2 - split_1) > canvas_w * 0.20:
                # Sanity: each zone must contain at least 3 blocks. Without this,
                # a single outlier x-position can mint a phantom 3rd column.
                z0 = sum(1 for b in blocks if b.x < split_1)
                z1 = sum(1 for b in blocks if split_1 <= b.x < split_2)
                z2 = sum(1 for b in blocks if b.x >= split_2)
                if min(z0, z1, z2) >= 3:
                    def _assign3(x: float) -> int:
                        if x < split_1:
                            return 0
                        if x < split_2:
                            return 1
                        return 2
                    return [_assign3(b.x) for b in blocks]

    # Fall back to 2 columns (largest gap)
    split_idx = int(np.argmax(gaps))
    col_split = top_xs[split_idx] + gaps[split_idx] / 2
    return [0 if b.x < col_split else 1 for b in blocks]


def classify_blocks(
    blocks: List[RawBlock], canvas_h: float = 0, canvas_w: float = 0
) -> List[Tuple[RawBlock, SemanticType]]:
    if not blocks:
        return []

    # R19.6: script/signature fonts (e.g. BrittanySignatureRegular logo text)
    # are typically much bigger than body type and skew max_font upward,
    # which pushes wine-section headers ("SPARKLING WINE" at 36pt vs Brittany
    # logo at 67pt → ratio 0.55) just below the 0.55/0.75 category thresholds.
    # Exclude these from the max_font baseline.
    def _is_script_family(blk: RawBlock) -> bool:
        raw = (getattr(blk, "font_family_raw", "") or "").lower()
        if blk.font_family == "decorative-script":
            return True
        return any(k in raw for k in ("signature", "script", "vibes", "brittany", "calligraph"))

    body_blocks = [b for b in blocks if not _is_script_family(b)]
    if body_blocks:
        max_font = max(b.font_size for b in body_blocks)
    else:
        max_font = max(b.font_size for b in blocks)
    # Header zone: top 12% of canvas — restaurant name lives here
    header_zone = canvas_h * 0.12 if canvas_h > 0 else 0

    # Process in reading order so we can track context
    order = sorted(range(len(blocks)), key=lambda i: (blocks[i].y, blocks[i].x))
    types: List[SemanticType] = ["other_text"] * len(blocks)
    restaurant_assigned = False

    for idx in order:
        b = blocks[idx]
        sem = _classify(b, max_font, header_zone, restaurant_assigned, canvas_w=canvas_w)
        if sem == "restaurant_name":
            restaurant_assigned = True
        types[idx] = sem

    return list(zip(blocks, types))


def _classify(b: RawBlock, max_font: float, header_zone: float, restaurant_assigned: bool, canvas_w: float = 0.0) -> SemanticType:
    text = b.text.strip()

    # R20.4: check PRICE_RE BEFORE the short-text guard. Wine lists have
    # plenty of legitimate 2-digit prices ('57', '70', '40') that the prior
    # `len <= 2` short-circuit was demoting to other_text, leaving 48% of
    # wines with null price after cross-column pairing.
    if PRICE_RE.match(text):
        # R19.6: wine vintages (4-digit years 1900-2099) are NOT prices.
        if text.isdigit() and len(text) == 4 and 1900 <= int(text) <= 2099:
            return "other_text"
        return "item_price"

    if len(text) <= 2:
        return "other_text"

    # R19.4: catch footer/URL/established/city-list lines before any
    # bold-upper heuristic claims them as items.
    if _is_footer(text):
        return "other_text"

    if PHONE_RE.fullmatch(text):
        return "phone"

    if _is_address(text):
        return "address"

    # R19.6: wine-entry promotion. A line that starts with a 2-4 digit code
    # ("224 Pinot Noir, Pike Road, OR") AND contains wine vocabulary is an
    # item_name, not an item_description or other_text. Run BEFORE the script-
    # font / bold-upper paths so it wins on plain Roman body type.
    import re as _re
    if _re.match(r"^\d{2,4}\s+", text) and _looks_like_wine_entry(text):
        return "item_name"

    font_ratio = b.font_size / max(max_font, 1)
    raw_font = (getattr(b, "font_family_raw", "") or "").lower()
    is_script_font = (
        b.font_family == "decorative-script"
        or any(k in raw_font for k in ("signature", "script", "vibes", "brittany", "calligraph"))
    )

    # Top area: only promote text to restaurant_name if it looks like a name —
    # at least 3 chars, contains a letter, and uses near-max font.
    # Otherwise it's likely a section header ("Brunch", "Dinner Menu") next
    # to a real logo graphic — the logo carries the brand, not this text.
    if (
        header_zone
        and b.y <= header_zone
        and not restaurant_assigned
        and len(text) >= 3
        and any(c.isalpha() for c in text)
        and font_ratio >= 0.85
    ):
        return "restaurant_name"

    # Script-font heuristic: short text in a script/signature font, below the
    # header zone, with no inline price, is overwhelmingly a section header
    # ("Sharable", "Starters", "Entrées", "Sides"). This catches the cases where
    # font_ratio falls below 0.75 because the restaurant logo uses an even bigger
    # script font and skews the max.
    word_count = len(text.split())
    if (
        is_script_font
        and (not header_zone or b.y > header_zone)
        and word_count <= 4
        and not PRICE_TAIL_RE.search(text)
        and text not in ("&",)
    ):
        return "category_header"

    # Removed R3-2: false-positive on item names — see Round 4 diagnosis. Using Claude-validated reclassification in pipeline.py instead.

    # ALL CAPS bold = menu item (with possible inline price)
    stripped = re.sub(r"[\s$/.,\-\d]", "", text)
    is_upper_content = len(stripped) > 0 and stripped.isupper()

    # Large font below header = section/category header.
    # Threshold at 0.75 separates headers (typically >80% of max)
    # from items that happen to use larger-than-normal font (~50-60%).
    # R19.8: R19.6 excluded script fonts from max_font baseline, which dropped
    # the denominator and pushed descriptions like "bone in wings, celery, ranch"
    # above 0.75 → false-promoted to category_header. Require uppercase content
    # AND no inline price tail before granting header status. Real headers
    # ("STARTERS", "SPARKLING WINE", "BREAKFAST") have all-caps content; food
    # descriptions and item-with-price lines do not.
    if (
        font_ratio >= 0.75
        and len(text) > 1
        and is_upper_content
        and not PRICE_TAIL_RE.search(text)
    ):
        return "category_header"

    # Secondary header check: medium font (55-75%) + bold + ALL CAPS + no inline price.
    # Catches category headers that use a smaller font than the restaurant name
    # (e.g. name=36pt, headers=22pt → ratio=0.61, missed by the 0.75 threshold above).
    # Guards: no price tail (avoids "SALMON 28"), text > 3 chars (avoids initials).
    if (0.55 <= font_ratio < 0.75
            and b.is_bold
            and is_upper_content
            and len(text) > 3
            and not PRICE_TAIL_RE.search(text)):
        return "category_header"

    if b.is_bold and is_upper_content:
        return "item_name"

    if b.is_bold:
        return "item_name"

    # Text ending with a price is an item line regardless of caps or bold
    if PRICE_TAIL_RE.search(text):
        return "item_name"

    # OCR blocks have no bold info — ALL CAPS alone signals an item name
    if is_upper_content:
        return "item_name"

    if len(text) > 20:
        return "item_description"

    return "other_text"


def build_menu_data(
    classified: List[Tuple[RawBlock, SemanticType]],
    col_assignments: List[int],
    source_file: str,
    side: str = "full",
    num_separators: int = 0,
) -> MenuData:
    restaurant_name: Optional[str] = None
    tagline: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    categories: List[MenuCategory] = []
    # Track current category per column so items go to the right section
    current_cats: dict[int, MenuCategory] = {}
    # R3-3 safety valve: park orphan item_name blocks here until end-of-loop.
    # Restored to a "General" bucket ONLY if no categories were detected at all.
    orphan_items: list[MenuItem] = []

    # R19.6: cross-column wine-list price pairing. In a 3-col wine list the
    # price sits in the rightmost column on the same row as the item_name in
    # column 0 or 1. The per-column loop below sees a column-N price and
    # attaches it to whatever the last item in column N was — which is
    # usually wrong for wine lists.
    # Pre-pass: for each item_price block in column N, find the nearest
    # item_name block in columns < N with |Δy| <= 10 px. If found, attach
    # the price directly to that name (as a sidecar attribute we read below)
    # and clear the sem so the per-column loop skips it.
    classified = list(classified)  # local mutable copy
    # Build a quick index of item_name blocks for proximity lookup.
    name_idx = [
        (i, b, col_assignments[i])
        for i, (b, s) in enumerate(classified)
        if s == "item_name"
    ]
    paired_prices: dict[int, str] = {}  # item_name index -> price string
    handled_price_idx: set[int] = set()
    for i, (b, s) in enumerate(classified):
        if s != "item_price":
            continue
        my_col = col_assignments[i] if i < len(col_assignments) else 0
        if my_col == 0:
            continue  # left column already aligns naturally
        my_y = b.y + b.h / 2
        # Find nearest item_name in a STRICTLY LOWER column index with |Δy|<=10
        best = None
        for j, nb, ncol in name_idx:
            if ncol >= my_col:
                continue
            if j in paired_prices:
                continue
            ny = nb.y + nb.h / 2
            dy = abs(ny - my_y)
            if dy > 10:
                continue
            if best is None or dy < best[0]:
                best = (dy, j)
        if best is not None:
            paired_prices[best[1]] = b.text.strip()
            handled_price_idx.add(i)

    for i, ((block, sem), col) in enumerate(zip(classified, col_assignments)):
        text = block.text.strip()

        # R19.6: skip prices already paired to a left-column item_name above.
        if sem == "item_price" and i in handled_price_idx:
            continue

        if sem == "restaurant_name" and not restaurant_name:
            restaurant_name = text
        elif sem == "tagline" and not tagline:
            tagline = text
        elif sem == "address":
            address = text
        elif sem == "phone":
            phone = text
        elif sem == "category_header":
            cat = MenuCategory(name=text, column=col)
            categories.append(cat)
            current_cats[col] = cat
        elif sem == "item_name":
            cat = current_cats.get(col)
            if cat is None:
                # Fallback: nearest column that has a category
                cat = next(iter(current_cats.values()), None)
            if cat is None:
                # R3-3: park as orphan. Restored to "General" at end ONLY if no
                # categories were ever detected on this side (safety valve below).
                name, price = _split_name_price(text)
                # R19.6: pick up paired wine-list price too.
                if price is None and i in paired_prices:
                    price = paired_prices[i]
                orphan_items.append(MenuItem(name=name, price=price))
                continue
            name, price = _split_name_price(text)
            # R19.6: pick up paired wine-list price too.
            if price is None and i in paired_prices:
                price = paired_prices[i]
            cat.items.append(MenuItem(name=name, price=price))
        elif sem == "item_description":
            cat = current_cats.get(col) or next(iter(current_cats.values()), None)
            if cat and cat.items:
                last = cat.items[-1]
                # R19.4: APPEND multi-line PyMuPDF descriptions instead of
                # overwriting. The prior code dropped all but the last line
                # ("home fries, choice of toast" ← lost the breakfast preamble).
                combined = (last.description + " " + text).strip() if last.description else text
                cat.items[-1] = MenuItem(name=last.name, description=combined, price=last.price)
        elif sem == "item_price":
            cat = current_cats.get(col) or next(iter(current_cats.values()), None)
            if cat and cat.items:
                last = cat.items[-1]
                cat.items[-1] = MenuItem(name=last.name, description=last.description, price=text)

    # R3-3 safety valve: if no categories were detected AT ALL, restore the
    # orphans into a "General" bucket so we don't lose every item.
    if not categories and orphan_items:
        general = MenuCategory(name="General", column=0, items=orphan_items)
        categories.append(general)
        print(f"[analyzer] R3-3 safety valve: {len(orphan_items)} orphan items routed to 'General' (no category headers detected)")

    num_cols = max(col_assignments, default=0) + 1
    return MenuData(
        source_file=source_file,
        side=side,
        restaurant_name=restaurant_name,
        tagline=tagline,
        address=address,
        phone=phone,
        categories=categories,
        num_separators=num_separators,
        num_columns=num_cols,
        layout_notes=f"{num_cols}-column layout, {len(categories)} sections detected.",
    )


_WINE_CODE_PREFIX_RE = re.compile(r"^\d{2,4}[A-Z]?\s+")


def _split_name_price(text: str) -> Tuple[str, Optional[str]]:
    """Split 'ITEM NAME     21' or 'ITEM NAME     21/18' into (name, price).

    R20.2: When the trailing number is a 4-digit wine vintage (1900-2099) and
    the line looks like a wine entry (vocab match OR wine-code prefix like
    '605A '), do NOT treat it as a price. Vintages glued by R8.1 span-merge
    would otherwise be peeled off as bogus prices on every Bordeaux/Burgundy
    entry ('Château Les Pagodes de Cos, St. Estèphe, 2014' → price='2014').
    """
    m = PRICE_TAIL_RE.search(text)
    if m:
        candidate = m.group(1).strip()
        digits = candidate.lstrip("$").strip()
        is_vintage = (digits.isdigit() and len(digits) == 4
                      and 1900 <= int(digits) <= 2099)
        looks_wine = _looks_like_wine_entry(text) or bool(_WINE_CODE_PREFIX_RE.match(text))
        if is_vintage and looks_wine:
            return text, None
        return text[: m.start()].strip(), candidate
    return text, None
