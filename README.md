# Menu Template Generator

Converts restaurant menu images and PDFs into structured JSON templates.

---

## Setup

Requires **Python 3.12** and an Anthropic API key.

```bash
/usr/local/bin/python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install torch torchvision
```

Create a `.env` file:
```
ANTHROPIC_API_KEY=sk-ant-...
```

---

## Run

### Option 1 — API server

```bash
source venv/bin/activate
uvicorn main:app --reload
```

Go to `did`, hit **POST /process**, upload a menu file.

Or via curl:
```bash
curl -X POST http://localhost:8000/process -F "file=@menu.jpg"
```

### Option 2 — Python directly

```python
from pipeline import process

results = process(
    file_path="menu.jpg",
    output_dir="outputs/my_menu",
    file_stem="my_menu",       # used as filename prefix for outputs
)

# results is a list of dicts (one per page/side):
# {
#   "side": "full",
#   "page": 1,
#   "menu_data": "outputs/my_menu/my_menu_menu_data.json",
#   "template": "outputs/my_menu/my_menu_template.json",
#   "num_elements": 87,
#   "num_categories": 6,
# }
```

---

## What you get

Two JSON files per menu in `outputs/<filename>/`:

- `*_template.json` — every element (text, separators, logo) with pixel position, size, font style
- `*_menu_data.json` — structured menu content (restaurant name, categories, items, prices)

Open `json_canvas_renderer.html` in a browser and load the template to see a visual preview.

---

## Accepted formats

`.pdf` `.jpg` `.jpeg` `.png` `.webp`

(PSD files must be exported to PNG first)

---

## First run note

The first image upload downloads ~1.2 GB of OCR models and compiles GPU shaders — expect a 3–5 minute wait. Every request after that is fast.
