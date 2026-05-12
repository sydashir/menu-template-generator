#!/usr/bin/env python3
"""
upload_assets.py — Download canonical brand/ornament assets and upload them to S3.

Sources all assets from publicly available press-kit URLs and Creative Commons
ornament libraries, then uploads to the configured S3 bucket under:
  s3://{S3_BUCKET}/{S3_ASSET_PREFIX}/badge/<name>.png
  s3://{S3_BUCKET}/{S3_ASSET_PREFIX}/ornament/<name>.png
  s3://{S3_BUCKET}/{S3_ASSET_PREFIX}/separator/<name>.png

Usage:
    python upload_assets.py [--dry-run] [--label badge/food_network]

Options:
    --dry-run     Fetch and save locally but skip S3 upload
    --label SLUG  Upload only the specified canonical label
    --list        List what's currently in S3 and exit
"""

import argparse
import io
import logging
import os
import sys
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv(override=True)

# ── Asset manifest ──────────────────────────────────────────────────────────
# Each entry: (canonical_label, source_url_or_None, local_gen_fn)
# If source URL is None, local_gen_fn(path) is called to synthesise the file.
# We use Pillow to generate clean ornament/separator PNGs programmatically for
# elements where we can't source a public-domain image — this gives us
# vector-sharp, background-free dividers regardless of what's in the source menu.

def _make_wavy_line(out: Path, w: int = 800, h: int = 30, color=(40, 30, 20)):
    """Generate a wavy calligraphic line PNG (transparent background)."""
    from PIL import Image, ImageDraw
    import math
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    amplitude = h * 0.28
    frequency = 2 * math.pi / (w / 6)
    center_y = h / 2
    pts = []
    for x in range(w):
        y = center_y + amplitude * math.sin(frequency * x)
        pts.append((x, y))
    # Draw with anti-aliasing via multiple shifted passes
    for dx, dy, alpha in [(0, 0, 255), (-1, -1, 80), (1, 1, 80)]:
        for i in range(len(pts) - 1):
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            draw.line([(x0 + dx, y0 + dy), (x1 + dx, y1 + dy)],
                      fill=(*color, alpha), width=2)
    img.save(out, "PNG")


def _make_double_line(out: Path, w: int = 800, h: int = 18, color=(40, 30, 20)):
    """Generate a clean double-rule PNG (transparent background)."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.line([(0, 4), (w, 4)], fill=(*color, 255), width=2)
    draw.line([(0, h - 4), (w, h - 4)], fill=(*color, 255), width=2)
    img.save(out, "PNG")


def _make_diamond_rule(out: Path, w: int = 800, h: int = 24, color=(40, 30, 20)):
    """Generate a centered diamond ornament rule PNG."""
    from PIL import Image, ImageDraw
    import math
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cy = h // 2
    # Left and right lines
    mid = w // 2
    diamond_hw = 10  # half-width of center diamond
    draw.line([(0, cy), (mid - diamond_hw - 2, cy)], fill=(*color, 255), width=2)
    draw.line([(mid + diamond_hw + 2, cy), (w, cy)], fill=(*color, 255), width=2)
    # Center diamond
    draw.polygon([
        (mid, cy - 8), (mid + diamond_hw, cy),
        (mid, cy + 8), (mid - diamond_hw, cy),
    ], fill=(*color, 255))
    img.save(out, "PNG")


def _make_floral_swash(out: Path, w: int = 400, h: int = 60, color=(40, 30, 20)):
    """
    Generate a simple symmetric calligraphic swash using Bezier curves.
    This gives a clean, transparent-background ornament for section headers.
    """
    from PIL import Image, ImageDraw
    import math

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = w // 2
    cy = h // 2

    # Draw a symmetric curl pattern: two S-curves mirrored around center
    def bezier_pts(p0, p1, p2, p3, steps=60):
        pts = []
        for i in range(steps + 1):
            t = i / steps
            x = (1-t)**3*p0[0] + 3*(1-t)**2*t*p1[0] + 3*(1-t)*t**2*p2[0] + t**3*p3[0]
            y = (1-t)**3*p0[1] + 3*(1-t)**2*t*p1[1] + 3*(1-t)*t**2*p2[1] + t**3*p3[1]
            pts.append((x, y))
        return pts

    # Left S-curl
    lpts = bezier_pts(
        (cx - 10, cy), (cx - 60, cy - 25), (cx - 120, cy + 20), (cx - 180, cy - 5)
    )
    # Right S-curl (mirror)
    rpts = bezier_pts(
        (cx + 10, cy), (cx + 60, cy - 25), (cx + 120, cy + 20), (cx + 180, cy - 5)
    )

    # Center diamond
    draw.polygon([(cx, cy - 8), (cx + 8, cy), (cx, cy + 8), (cx - 8, cy)], fill=(*color, 255))

    for pts in [lpts, rpts]:
        for i in range(len(pts) - 1):
            alpha = int(255 * (1 - abs(i / len(pts) - 0.5) * 0.6))
            draw.line([pts[i], pts[i + 1]], fill=(*color, alpha), width=2)

    img.save(out, "PNG")


def _make_scroll_divider(out: Path, w: int = 600, h: int = 40, color=(40, 30, 20)):
    """Generate a scroll-style divider."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cy = h // 2
    cx = w // 2
    # Center line with end scrolls
    draw.line([(20, cy), (w - 20, cy)], fill=(*color, 220), width=2)
    # Left scroll circle
    draw.ellipse([(5, cy - 8), (21, cy + 8)], outline=(*color, 200), width=2)
    # Right scroll circle
    draw.ellipse([(w - 21, cy - 8), (w - 5, cy + 8)], outline=(*color, 200), width=2)
    img.save(out, "PNG")


# Asset manifest: (label, url_or_None, generator_fn_or_None, local_filename_hint)
# URLs are tried first; if None or fails, generator is used as fallback.
# For badges, URLs point to Wikimedia Commons (widely available press-use images).
ASSET_MANIFEST = [
    # ── Badges (downloaded from Wikimedia Commons public URLs) ────────────
    (
        "badge/food_network",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/b/be/Food_Network_New_Logo_2023.svg/200px-Food_Network_New_Logo_2023.svg.png",
        None, "food_network.png",
    ),
    (
        "badge/opentable_diners_choice",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/5/5d/OpenTable_Logo.svg/200px-OpenTable_Logo.svg.png",
        None, "opentable_diners_choice.png",
    ),
    (
        "badge/youtube",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/0/09/YouTube_full-color_icon_%282017%29.svg/200px-YouTube_full-color_icon_%282017%29.svg.png",
        None, "youtube.png",
    ),
    (
        "badge/hulu",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e4/Hulu_Logo.svg/200px-Hulu_Logo.svg.png",
        None, "hulu.png",
    ),
    (
        "badge/tripadvisor",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/9/9a/TripAdvisor_owl_logo.svg/200px-TripAdvisor_owl_logo.svg.png",
        None, "tripadvisor.png",
    ),
    (
        "badge/yelp",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ad/Yelp_Logo.svg/200px-Yelp_Logo.svg.png",
        None, "yelp.png",
    ),
    (
        "badge/michelin",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e9/Michelin_logo.svg/200px-Michelin_logo.svg.png",
        None, "michelin.png",
    ),
    (
        "badge/zagat",
        None, None, "zagat.png",  # no reliable public URL — place manually if needed
    ),
    (
        "badge/best_of",
        None, None, "best_of.png",
    ),

    # ── Ornaments (programmatically generated — no copyright concerns) ────
    ("ornament/floral_swash_centered", None, _make_floral_swash,   "floral_swash_centered.png"),
    ("ornament/floral_swash_left",     None, _make_floral_swash,   "floral_swash_left.png"),
    ("ornament/scroll_divider",        None, _make_scroll_divider, "scroll_divider.png"),
    ("ornament/diamond_rule",          None, _make_diamond_rule,   "diamond_rule.png"),

    # ── Separators (programmatically generated) ───────────────────────────
    ("separator/wavy_line",            None, _make_wavy_line,      "wavy_line.png"),
    ("separator/double_line",          None, _make_double_line,    "double_line.png"),
    ("separator/diamond_rule",         None, _make_diamond_rule,   "separator_diamond_rule.png"),
]

LOCAL_ASSETS_DIR = Path(__file__).parent / "local_assets"


def _fetch_or_generate(label: str, url: str | None, gen_fn, hint: str) -> bytes | None:
    """
    Attempt to load from local_assets/ first, then URL, then generator.
    Returns PNG bytes or None.
    """
    # 1. Local override
    local = LOCAL_ASSETS_DIR / hint
    if local.is_file():
        logger.info("  local file: %s", local)
        return local.read_bytes()

    # 2. URL fetch
    if url:
        try:
            logger.info("  fetching: %s", url)
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read()
        except Exception as exc:
            logger.warning("  URL fetch failed (%s): %s", url, exc)

    # 3. Generator
    if gen_fn:
        logger.info("  generating: %s", label)
        tmp = Path("/tmp") / hint
        gen_fn(tmp)
        if tmp.is_file():
            data = tmp.read_bytes()
            tmp.unlink(missing_ok=True)
            return data

    logger.warning("  no source for %s — place file at local_assets/%s", label, hint)
    return None


def main():
    parser = argparse.ArgumentParser(description="Upload menu graphic assets to S3")
    parser.add_argument("--dry-run", action="store_true", help="Skip S3 upload, only generate/check files")
    parser.add_argument("--label", help="Upload only this specific canonical label")
    parser.add_argument("--list", action="store_true", help="List assets in S3 and exit")
    args = parser.parse_args()

    from s3_asset_library import upload_asset, list_assets

    if args.list:
        assets = list_assets()
        if assets:
            print(f"\n{len(assets)} assets in S3:")
            for a in sorted(assets):
                print(f"  {a}")
        else:
            print("No assets found in S3 (bucket may be empty or unreachable)")
        return

    LOCAL_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = [(l, u, g, h) for l, u, g, h in ASSET_MANIFEST
                if args.label is None or l == args.label]

    if args.label and not manifest:
        logger.error("Unknown label: %s", args.label)
        logger.error("Known labels: %s", [l for l, *_ in ASSET_MANIFEST])
        sys.exit(1)

    ok = fail = skip = 0
    for label, url, gen_fn, hint in manifest:
        logger.info("[%s]", label)
        data = _fetch_or_generate(label, url, gen_fn, hint)
        if data is None:
            skip += 1
            continue

        # Save locally for inspection
        out_path = LOCAL_ASSETS_DIR / hint
        out_path.write_bytes(data)
        logger.info("  saved locally: %s (%d bytes)", out_path, len(data))

        if args.dry_run:
            logger.info("  dry-run: skipping S3 upload")
            ok += 1
            continue

        if upload_asset(label, str(out_path)):
            ok += 1
        else:
            fail += 1

    print(f"\nDone: {ok} uploaded, {skip} skipped (no source), {fail} failed")
    print(f"\nFor badge assets (Food Network, OpenTable, etc.) place your licensed PNG files in:")
    print(f"  {LOCAL_ASSETS_DIR}/")
    print("Then re-run: python upload_assets.py --label badge/food_network")


if __name__ == "__main__":
    main()
