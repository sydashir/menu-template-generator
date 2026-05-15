"""
render_compare.py — render the source file (PDF page or image) at the same
dimensions as the corresponding pipeline snapshot, then produce a
side-by-side comparison PNG.

Usage:
  ./venv/bin/python3 render_compare.py "<menu stem>" "<source path>"

Outputs:
  source_renders/<stem>.png  — source rendered
  compares/<stem>_compare.png — side-by-side (source | pipeline output)
"""
from __future__ import annotations
import sys
from pathlib import Path
from PIL import Image
import fitz  # pymupdf

REPO = Path(__file__).parent
SNAPS = REPO / "snapshots"
SRC_DIR = REPO / "source_renders"
CMP_DIR = REPO / "compares"
SRC_DIR.mkdir(exist_ok=True)
CMP_DIR.mkdir(exist_ok=True)


def render_source_to_png(source_path: Path, target_w: int, target_h: int) -> Path:
    """Render the source file to a PNG sized to ~target_w × target_h."""
    out = SRC_DIR / f"{source_path.stem}.png"
    if source_path.suffix.lower() == ".pdf":
        doc = fitz.open(str(source_path))
        # Multi-page PDF — render all pages stacked vertically
        pages = []
        for page in doc:
            scale = max(target_w / page.rect.width, target_h / page.rect.height)
            mat = fitz.Matrix(scale, scale)
            pix = page.get_pixmap(matrix=mat)
            pages.append(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))
        if len(pages) == 1:
            pages[0].save(out, "PNG")
        else:
            # Stack vertically
            w = max(p.width for p in pages)
            h = sum(p.height for p in pages)
            combo = Image.new("RGB", (w, h), "white")
            y = 0
            for p in pages:
                combo.paste(p, (0, y))
                y += p.height
            combo.save(out, "PNG")
    else:
        img = Image.open(str(source_path)).convert("RGB")
        # Resize to roughly match target
        scale = target_w / img.width
        new_w, new_h = target_w, int(img.height * scale)
        img.resize((new_w, new_h), Image.LANCZOS).save(out, "PNG")
    return out


def make_compare(stem: str, source_path: Path) -> Path | None:
    """Produce a side-by-side compare PNG for `stem`."""
    snap = SNAPS / f"{stem}_template.png"
    snaps_p1 = SNAPS / f"{stem}_p1_template.png"
    if snap.is_file():
        candidates = [snap]
    elif snaps_p1.is_file():
        # Multi-page — collect all p* snapshots and stack vertically
        candidates = sorted(SNAPS.glob(f"{stem}_p*_template.png"))
    else:
        print(f"NO snapshot for {stem!r}")
        return None

    # Stack snapshots if multiple pages
    if len(candidates) == 1:
        pipeline_img = Image.open(candidates[0]).convert("RGB")
    else:
        imgs = [Image.open(c).convert("RGB") for c in candidates]
        w = max(i.width for i in imgs)
        h = sum(i.height for i in imgs)
        pipeline_img = Image.new("RGB", (w, h), "white")
        y = 0
        for i in imgs:
            pipeline_img.paste(i, (0, y))
            y += i.height

    src_png = render_source_to_png(source_path, pipeline_img.width, pipeline_img.height)
    src_img = Image.open(src_png).convert("RGB")

    # Same height for clean side-by-side
    h = max(src_img.height, pipeline_img.height)
    if src_img.height != h:
        s = h / src_img.height
        src_img = src_img.resize((int(src_img.width * s), h), Image.LANCZOS)
    if pipeline_img.height != h:
        s = h / pipeline_img.height
        pipeline_img = pipeline_img.resize((int(pipeline_img.width * s), h), Image.LANCZOS)

    gap = 30
    combo = Image.new("RGB", (src_img.width + pipeline_img.width + gap, h), "gray")
    combo.paste(src_img, (0, 0))
    combo.paste(pipeline_img, (src_img.width + gap, 0))

    out = CMP_DIR / f"{stem}_compare.png"
    combo.save(out, "PNG")
    return out


def main(args: list[str]) -> None:
    if len(args) < 2:
        print(__doc__)
        sys.exit(1)
    stem = args[0]
    source_path = Path(args[1])
    if not source_path.is_file():
        print(f"source not found: {source_path}")
        sys.exit(1)
    cmp_png = make_compare(stem, source_path)
    if cmp_png:
        print(f"wrote {cmp_png}")


if __name__ == "__main__":
    main(sys.argv[1:])
