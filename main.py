import logging
import shutil
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

load_dotenv()

from pipeline import process

app = FastAPI(
    title="Menu Template Generator",
    description="Converts restaurant menu PDFs and images into structured canvas templates.",
    version="1.0.0",
)

OUTPUT_DIR = Path("outputs")


@app.post("/process")
async def process_menu(file: UploadFile = File(...)):
    """
    Upload a menu PDF or image (JPG/PNG).
    Returns paths to the generated menu_data.json and template.json files.
    """
    allowed = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
    ext = Path(file.filename).suffix.lower()

    if ext not in allowed:
        raise HTTPException(
            status_code=400,
                 detail=f"Unsupported file type '{ext}'. Upload PDF, JPG, PNG, or WEBP. "
                   "PSD files must be exported to PNG first.",
        )

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        original_stem = Path(file.filename).stem
        results = await run_in_threadpool(
            process, tmp_path, str(OUTPUT_DIR / original_stem), original_stem
        )
    except ValueError as e:
        logger.error("validation error processing %r: %s", file.filename, e)
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception("unexpected error processing %r: %s: %s", file.filename, type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Processing failed. Check server logs.")
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return JSONResponse(content={"file": file.filename, "results": results})


@app.get("/health")
def health():
    return {"status": "ok"}
