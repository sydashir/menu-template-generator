"""
render_snapshot.py — headless-render outputs/*_template.json files through
static/renderer.html and save a PNG snapshot of each so visual issues can be
diff'd without opening a real browser.

Usage:
  ./venv/bin/python3 render_snapshot.py outputs/SOME_template.json
  ./venv/bin/python3 render_snapshot.py --all
  ./venv/bin/python3 render_snapshot.py --stems "AMI BRUNCH 2022,bar & Patio"
"""
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).parent
RENDERER = REPO / "static" / "renderer.html"
OUT = REPO / "outputs"
SNAPS = REPO / "snapshots"
SNAPS.mkdir(parents=True, exist_ok=True)


async def render_one(json_path: Path, snap_path: Path) -> dict:
    from playwright.async_api import async_playwright
    info = {"json": str(json_path), "png": str(snap_path), "ok": False, "errors": []}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1600, "height": 2400})
        page = await ctx.new_page()

        # Capture console errors
        def _on_console(msg):
            if msg.type == "error":
                info["errors"].append(msg.text)
        page.on("console", _on_console)
        page.on("pageerror", lambda e: info["errors"].append(str(e)))

        await page.goto(f"file://{RENDERER.resolve()}", wait_until="domcontentloaded")

        # Find the menu_data path alongside the template
        menu_data_path = Path(str(json_path).replace("_template.json", "_menu_data.json"))

        # Attach files via the file inputs the renderer exposes
        await page.set_input_files("#templateFiles", str(json_path))
        if menu_data_path.is_file():
            await page.set_input_files("#menuDataFile", str(menu_data_path))

        # Untick bounding-box overlay so the snapshot is the clean render
        try:
            await page.uncheck("#showBBoxes")
        except Exception:
            pass

        # Wait for the canvas to be resized to match template dimensions —
        # that's the signal that a template was loaded and render() ran.
        try:
            await page.wait_for_function(
                """() => {
                    const c = document.querySelector('canvas');
                    return c && c.width >= 500 && c.height >= 500;
                }""",
                timeout=15000,
            )
        except Exception:
            info["errors"].append("canvas never resized to template dimensions")
        # Allow text + image draws to settle (S3 PNGs decode async)
        await page.wait_for_timeout(2500)

        canvas_dims = await page.evaluate("""() => {
            const c = document.querySelector('canvas');
            if (!c) return null;
            const r = c.getBoundingClientRect();
            return { width: c.width, height: c.height, css_w: r.width, css_h: r.height, top: r.top, left: r.left };
        }""")
        if canvas_dims:
            info["canvas"] = canvas_dims
            await page.set_viewport_size({
                "width": max(600, int(canvas_dims["css_w"]) + 40),
                "height": max(600, int(canvas_dims["css_h"]) + 40),
            })

        # Screenshot the canvas (not the whole page chrome)
        canvas = await page.query_selector("canvas")
        if canvas:
            await canvas.screenshot(path=str(snap_path), type="png")
            info["ok"] = True
        else:
            info["errors"].append("no canvas element found")

        await browser.close()
    return info


def stems_in_outputs() -> list[str]:
    stems = set()
    for p in OUT.glob("*_template.json"):
        stems.add(p.stem.replace("_template", ""))
    return sorted(stems)


async def main(args):
    targets: list[Path] = []
    if not args or args == ["--all"]:
        for stem in stems_in_outputs():
            targets.append(OUT / f"{stem}_template.json")
    elif args[0] == "--stems":
        wanted = {s.strip() for s in args[1].split(",")}
        for stem in stems_in_outputs():
            if any(w in stem for w in wanted):
                targets.append(OUT / f"{stem}_template.json")
    else:
        for a in args:
            p = Path(a)
            if not p.is_absolute():
                p = REPO / a
            targets.append(p)

    results = []
    for t in targets:
        snap = SNAPS / (t.stem + ".png")
        print(f"render → {snap.name}")
        info = await render_one(t, snap)
        results.append(info)
        if info["errors"]:
            for e in info["errors"][:3]:
                print(f"  console: {e}")
    print("\nResults:")
    for r in results:
        flag = "OK" if r["ok"] else "FAIL"
        c = r.get("canvas", {})
        canvas_str = f" canvas={c.get('width')}×{c.get('height')}" if c else ""
        print(f"  [{flag}] {Path(r['json']).name}{canvas_str}  errors={len(r['errors'])}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
