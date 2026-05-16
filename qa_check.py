"""
qa_check.py — quick post-pipeline verification helper.
Usage: ./venv/bin/python3 qa_check.py <stem>
       (reads outputs/<stem>_p{1..N}_{menu_data,template}.json)
Or: ./venv/bin/python3 qa_check.py --all
"""
import json
import sys
from pathlib import Path
from collections import Counter

OUT = Path(__file__).parent / "outputs"


def _empty(*paths):
    return any(not p or not Path(p).is_file() for p in paths)


def check(stem: str) -> None:
    print(f"\n{'='*72}\n{stem}\n{'='*72}")
    page = 1
    found = False
    while True:
        md_path = OUT / f"{stem}_p{page}_menu_data.json"
        tmpl_path = OUT / f"{stem}_p{page}_template.json"
        single_md = OUT / f"{stem}_menu_data.json"
        single_tmpl = OUT / f"{stem}_template.json"
        # Some single-page menus may use no _pN suffix
        if not md_path.exists() and page == 1 and single_md.exists():
            md_path = single_md
            tmpl_path = single_tmpl
        if not md_path.exists():
            break
        found = True
        try:
            md = json.loads(md_path.read_text())
            t = json.loads(tmpl_path.read_text())
        except Exception as e:
            print(f"  [P{page}] read error: {e}")
            page += 1
            continue
        print(f"\n  --- Page {page} ---")
        print(f"  restaurant_name: {md.get('restaurant_name')!r}")
        print(f"  num_columns (template meta): {t['metadata'].get('num_columns')}")
        print(f"  num_categories: {len(md.get('categories', []))}")
        cats = md.get("categories", [])
        empty_cats = sum(1 for c in cats if not c.get("items"))
        item_total = sum(len(c.get("items", [])) for c in cats)
        print(f"    empty categories: {empty_cats}/{len(cats)}  total items: {item_total}")
        for c in cats:
            items = c.get("items", [])
            print(f"      '{c['name'][:40]}' col={c['column']} items={len(items)}")
        els = t.get("elements", [])
        types = Counter((e.get("type"), e.get("subtype")) for e in els)
        print(f"  total elements: {len(els)}")
        for k, v in sorted(types.items(), key=lambda x: -x[1])[:10]:
            print(f"    {k}: {v}")
        # Logos
        logos = [e for e in els if e.get("type") == "logo"]
        print(f"  LOGOS: {len(logos)}")
        for l in logos:
            bd = l["bbox"]
            print(f"    bbox=({bd['x']:.0f},{bd['y']:.0f}) {bd['w']:.0f}x{bd['h']:.0f} "
                  f"hint={l.get('position_hint')!r}  has_img={bool(l.get('image_data'))} "
                  f"idx={l.get('logo_index', '-')}")
        # Big brand badges
        badges = [e for e in els if e.get("type") == "image" and e.get("subtype") == "badge"]
        for b in badges:
            sl = b.get("semantic_label") or "?"
            bd = b["bbox"]
            print(f"  BADGE {sl} @ ({bd['x']:.0f},{bd['y']:.0f}) {bd['w']:.0f}x{bd['h']:.0f}")
        # Collage box count
        collages = [e for e in els if e.get("type") == "image" and e.get("subtype") == "collage_box"]
        print(f"  COLLAGE_BOXES: {len(collages)} (with image: {sum(1 for c in collages if c.get('image_data'))})")
        # Empty/junk elements
        empty_logos = sum(1 for l in logos if not l.get("image_data"))
        empty_images = sum(1 for e in els if e.get("type") == "image" and not e.get("image_data") and not e.get("semantic_label"))
        empty_seps = sum(1 for e in els if e.get("type") == "separator"
                          and not e.get("image_data") and not e.get("semantic_label")
                          and e.get("subtype") == "decorative_divider")
        if empty_logos or empty_images or empty_seps:
            print(f"  ⚠️  EMPTY: logos={empty_logos} unlabeled-images={empty_images} bare-decorative_dividers={empty_seps}")
        page += 1
    if not found:
        print(f"  (no outputs found for stem '{stem}')")


def main():
    if "--all" in sys.argv:
        stems = set()
        for p in OUT.glob("*_template.json"):
            name = p.stem
            for suffix in ("_p1_template", "_p2_template", "_template"):
                if name.endswith(suffix):
                    stems.add(name[: -len(suffix)])
                    break
        for stem in sorted(stems):
            check(stem)
    else:
        for stem in sys.argv[1:]:
            check(stem)


if __name__ == "__main__":
    main()
