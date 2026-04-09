import re
from typing import List, Tuple, Optional

from models import RawBlock, SemanticType, MenuCategory, MenuItem, MenuData

PRICE_RE = re.compile(r"^\$?\s?\d{1,4}(?:[./]\d{1,4})?(?:\.\d{2})?$")
PRICE_TAIL_RE = re.compile(r"\s+(\$?\s?\d{1,4}(?:[./]\d{1,4})?(?:\.\d{2})?)$")
PHONE_RE = re.compile(r"[\+\(]?[1-9][0-9 \.\-\(\)]{7,}[0-9]")
ADDRESS_KEYWORDS = {"street", "ave", "blvd", "rd", "suite", "ste", "drive"}


def detect_columns(blocks: List[RawBlock], canvas_w: float) -> List[int]:
    """Assign column index using the two densest x-coordinate clusters."""
    if not blocks:
        return []

    import numpy as np

    x_lefts = np.array([b.x for b in blocks])
    hist, edges = np.histogram(x_lefts, bins=100, range=(0, canvas_w))
    smoothed = np.convolve(hist.astype(float), np.ones(5) / 5, mode="same")

    # Pick the 10 densest bins and sort their x positions
    top_idx = np.argsort(smoothed)[-10:]
    top_xs = np.sort(edges[top_idx])

    if len(top_xs) < 2:
        return [0] * len(blocks)

    # Find the largest gap between dense-bin positions
    gaps = np.diff(top_xs)
    max_gap = float(np.max(gaps))

    # If no significant gap, treat as single column
    if max_gap < canvas_w * 0.20:
        return [0] * len(blocks)

    split_pos = top_xs[int(np.argmax(gaps))]
    col_split = split_pos + max_gap / 2

    return [0 if b.x < col_split else 1 for b in blocks]


def classify_blocks(
    blocks: List[RawBlock], canvas_h: float = 0
) -> List[Tuple[RawBlock, SemanticType]]:
    if not blocks:
        return []

    max_font = max(b.font_size for b in blocks)
    # Header zone: top 12% of canvas — restaurant name lives here
    header_zone = canvas_h * 0.12 if canvas_h > 0 else 0

    # Process in reading order so we can track context
    order = sorted(range(len(blocks)), key=lambda i: (blocks[i].y, blocks[i].x))
    types: List[SemanticType] = ["other_text"] * len(blocks)
    restaurant_assigned = False

    for idx in order:
        b = blocks[idx]
        sem = _classify(b, max_font, header_zone, restaurant_assigned)
        if sem == "restaurant_name":
            restaurant_assigned = True
        types[idx] = sem

    return list(zip(blocks, types))


def _classify(b: RawBlock, max_font: float, header_zone: float, restaurant_assigned: bool) -> SemanticType:
    text = b.text.strip()

    if len(text) <= 2:
        return "other_text"

    if PRICE_RE.match(text):
        return "item_price"

    if PHONE_RE.fullmatch(text):
        return "phone"

    lower = text.lower()
    if any(kw in lower for kw in ADDRESS_KEYWORDS):
        return "address"

    font_ratio = b.font_size / max(max_font, 1)

    # Top area: first meaningful text is the restaurant/menu name
    if header_zone and b.y <= header_zone and not restaurant_assigned and len(text) > 1:
        return "restaurant_name"

    # Large font below header = section/category header
    # Threshold at 0.75 separates headers (typically >80% of max)
    # from items that happen to use larger-than-normal font (~50-60%)
    if font_ratio >= 0.75 and len(text) > 1:
        return "category_header"

    # ALL CAPS bold = menu item (with possible inline price)
    stripped = re.sub(r"[\s$/.,\-\d]", "", text)
    is_upper_content = len(stripped) > 0 and stripped.isupper()

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

    for (block, sem), col in zip(classified, col_assignments):
        text = block.text.strip()

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
                # Reuse any existing "General" category to avoid duplicates
                cat = next((c for c in categories if c.name == "General"), None)
                if cat is None:
                    cat = MenuCategory(name="General", column=col)
                    categories.append(cat)
                current_cats[col] = cat
            name, price = _split_name_price(text)
            cat.items.append(MenuItem(name=name, price=price))
        elif sem == "item_description":
            cat = current_cats.get(col) or next(iter(current_cats.values()), None)
            if cat and cat.items:
                last = cat.items[-1]
                cat.items[-1] = MenuItem(name=last.name, description=text, price=last.price)
        elif sem == "item_price":
            cat = current_cats.get(col) or next(iter(current_cats.values()), None)
            if cat and cat.items:
                last = cat.items[-1]
                cat.items[-1] = MenuItem(name=last.name, description=last.description, price=text)

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


def _split_name_price(text: str) -> Tuple[str, Optional[str]]:
    """Split 'ITEM NAME     21' or 'ITEM NAME     21/18' into (name, price)."""
    m = PRICE_TAIL_RE.search(text)
    if m:
        return text[: m.start()].strip(), m.group(1).strip()
    return text, None
