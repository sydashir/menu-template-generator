"""
Idempotent seeder: reads outputs/**/*_template.json + *_menu_data.json
and upserts each pair into MongoDB.

Usage:
    MONGODB_URI="mongodb+srv://..." python seed_mongo.py
"""

import asyncio
import json
import re
from pathlib import Path

from database import close_client, upsert_menu

OUTPUTS_DIR = Path("outputs")


def detect_file_type(source_file: str) -> str:
    p = Path(source_file)
    if p.suffix.lower() == ".pdf":
        return "pdf"
    return "image"


def parse_side_page(stem: str) -> tuple[str, int]:
    side = "front"
    page = 1

    if re.search(r"[_\-]?(back)[_\-]?", stem, re.IGNORECASE):
        side = "back"

    m = re.search(r"[_\-]p(?:age)?(\d+)$", stem, re.IGNORECASE)
    if m:
        page = int(m.group(1))

    return side, page


async def seed():
    if not OUTPUTS_DIR.exists():
        print(f"[seed] outputs dir not found: {OUTPUTS_DIR.resolve()}")
        return

    dirs = [d for d in OUTPUTS_DIR.iterdir() if d.is_dir()]
    if not dirs:
        print("[seed] No subdirectories found in outputs/")
        return

    seeded = 0
    skipped = 0

    for menu_dir in sorted(dirs):
        template_files = list(menu_dir.glob("*_template.json"))
        menu_data_files = list(menu_dir.glob("*_menu_data.json"))

        if not template_files or not menu_data_files:
            print(f"[seed] skip {menu_dir.name!r}: missing template or menu_data")
            skipped += 1
            continue

        template_map = {f.name.replace("_template.json", ""): f for f in template_files}
        data_map = {f.name.replace("_menu_data.json", ""): f for f in menu_data_files}

        for stem in sorted(set(template_map) & set(data_map)):
            tf = template_map[stem]
            df = data_map[stem]

            try:
                template = json.loads(tf.read_text(encoding="utf-8"))
                menu_data = json.loads(df.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[seed] error reading {stem}: {e}")
                skipped += 1
                continue

            source_file = menu_data.get("source_file", stem)
            file_type = detect_file_type(source_file)
            side, page = parse_side_page(stem)

            inserted_id = await upsert_menu(
                name=stem,
                source_file=source_file,
                file_type=file_type,
                side=side,
                page=page,
                menu_data=menu_data,
                template=template,
            )
            print(f"[seed] upserted {stem!r} → {inserted_id}")
            seeded += 1

    print(f"\n[seed] done: {seeded} upserted, {skipped} skipped")
    close_client()


if __name__ == "__main__":
    asyncio.run(seed())
