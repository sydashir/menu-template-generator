import logging
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

logger = logging.getLogger(__name__)

DEPLOY_MODE = os.getenv("DEPLOY_MODE", "").lower() == "prod"
OUTPUT_DIR = Path("outputs")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from database import get_client
    get_client()
    yield
    from database import close_client
    close_client()


app = FastAPI(
    title="Menu Template Generator",
    description="Converts restaurant menu PDFs and images into structured canvas templates.",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="static", html=True), name="static")


# ── Read-only menu endpoints ───────────────────────────────────────────────────

@app.get("/menus")
async def get_menus():
    from database import list_menus
    return await list_menus()


@app.get("/menus/{menu_id}/data")
async def get_menu_data(menu_id: str):
    from database import get_menu_data as _get
    data = await _get(menu_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Menu not found")
    return data


@app.get("/menus/{menu_id}/template")
async def get_template(menu_id: str):
    from database import get_template as _get
    tmpl = await _get(menu_id)
    if tmpl is None:
        raise HTTPException(status_code=404, detail="Menu not found")
    return tmpl


@app.get("/menus/{menu_id}/download/data")
async def download_data(menu_id: str):
    from database import get_menu_data as _get
    data = await _get(menu_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Menu not found")
    return JSONResponse(
        content=data,
        headers={"Content-Disposition": f'attachment; filename="{menu_id}_menu_data.json"'},
    )


@app.get("/menus/{menu_id}/download/template")
async def download_template(menu_id: str):
    from database import get_template as _get
    tmpl = await _get(menu_id)
    if tmpl is None:
        raise HTTPException(status_code=404, detail="Menu not found")
    return JSONResponse(
        content=tmpl,
        headers={"Content-Disposition": f'attachment; filename="{menu_id}_template.json"'},
    )


# ── Processing endpoint (disabled in prod) ────────────────────────────────────

@app.post("/process")
async def process_menu(file: UploadFile = File(...)):
    import json
    # Deferred import keeps startup fast (surya model loads on first request)
    from pipeline import process

    allowed = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
    ext = Path(file.filename).suffix.lower()

    if ext not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Upload PDF, JPG, PNG, or WEBP.",
        )

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    original_stem = Path(file.filename).stem
    out_dir = OUTPUT_DIR / original_stem

    try:
        results = await run_in_threadpool(
            process, tmp_path, str(out_dir), original_stem
        )
    except ValueError as e:
        logger.error("validation error processing %r: %s", file.filename, e)
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("unexpected error processing %r: %s: %s", file.filename, type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Processing failed. Check server logs.")
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    # Save results to MongoDB
    menu_id = None
    template_file = out_dir / f"{original_stem}_template.json"
    data_file = out_dir / f"{original_stem}_menu_data.json"

    if template_file.exists() and data_file.exists():
        from database import upsert_menu
        try:
            template = json.loads(template_file.read_text(encoding="utf-8"))
            menu_data = json.loads(data_file.read_text(encoding="utf-8"))
            file_type = "pdf" if ext == ".pdf" else "image"
            menu_id = await upsert_menu(
                name=original_stem,
                source_file=file.filename,
                file_type=file_type,
                side="front",
                page=1,
                menu_data=menu_data,
                template=template,
            )
        except Exception as e:
            logger.error("MongoDB upsert failed for %r: %s", original_stem, e)

    return JSONResponse(content={
        "file": file.filename,
        "name": original_stem,
        "id": menu_id,
        "results": results,
    })


@app.get("/health")
def health():
    return {"status": "ok"}
