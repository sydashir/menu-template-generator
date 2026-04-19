# Deployment Notes — Menu Template Generator

## Infrastructure
- **Azure VM**: `20.187.152.110:8000`
- **OS**: Ubuntu 24.04 (Noble)
- **User**: `azureuser`
- **App dir**: `/home/azureuser/menu-template-generator`
- **Branch on VM**: `dev`
- **Service**: `menu-generator.service` (systemd)
- **MongoDB**: Atlas — `mongodb+srv://menu_app:...@cluster0.e0lxuwm.mongodb.net/menu_generator`

## Service Commands
```bash
sudo systemctl start|stop|restart|status menu-generator
sudo journalctl -u menu-generator -f          # live logs
sudo journalctl -u menu-generator -n 50 --no-pager  # last 50 lines
```

## Deploy New Code
```bash
cd ~/menu-template-generator
git pull origin dev
sudo systemctl restart menu-generator
```

## Systemd Service File
`/etc/systemd/system/menu-generator.service`
```ini
[Unit]
Description=Menu Generator
After=network.target

[Service]
User=azureuser
WorkingDirectory=/home/azureuser/menu-template-generator
EnvironmentFile=/home/azureuser/menu-template-generator/.env
ExecStart=/home/azureuser/menu-template-generator/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## .env on VM
```
MONGODB_URI=mongodb+srv://menu_app:<pass>@cluster0.e0lxuwm.mongodb.net/menu_generator?appName=Cluster0
MONGODB_DB=menu_generator
ANTHROPIC_API_KEY=<key>
```
> Edit with: `nano ~/menu-template-generator/.env` → Ctrl+X → Y → Enter

## Python Environment
```bash
source ~/menu-template-generator/venv/bin/activate
pip install -r requirements-prod.txt
```

---

## Known Issues & Status

### 1. Claude API — 403 "Request not allowed" ❌ UNRESOLVED
- **Symptom**: Every Claude API call from VM returns 403 forbidden
- **Confirmed**: `curl` directly to Anthropic API also returns 403
- **Cause**: Azure VM's IP (`20.187.152.110`) is blocked by Anthropic
- **Key is fine**: Same key works locally, new key also gives 403 from VM
- **Fix options**:
  - Contact Anthropic support at `support.anthropic.com` — ask to whitelist IP `20.187.152.110`
  - Recreate VM in a US region (`eastus` or `westus2`) — US Azure IPs less likely blocked
  - Add outbound proxy (DigitalOcean droplet in US as relay)
- **Impact**: Pipeline falls back to Claude Vision tool_use which also fails → garbage output

### 2. Surya OCR — `'EfficientViTConfig' object has no attribute 'bbox_size'` ❌ IN PROGRESS
- **Symptom**: Surya model load fails on VM
- **Cause**: `RecognitionPredictor(det_predictor)` reads `det_predictor.model.config.bbox_size` before model weights are downloaded (lazy init)
- **VM has**: `surya-ocr==0.17.1`, `transformers==4.57.6`
- **Local has**: `surya-ocr==0.4.5` (different API, works locally)
- **Fix applied**: Added dummy inference on `DetectionPredictor` to force model load before passing to `RecognitionPredictor`
- **Code change in**: `claude_extractor.py` → `_load_surya_models()` function
- **Status**: Fix committed, needs to be pushed and tested on VM

### 3. Model — Switched from Opus to Sonnet ✅ DONE
- Changed all `claude-opus-4-6` → `claude-sonnet-4-6` in `claude_extractor.py`
- Committed and pushed to dev

---

## Architecture

### Pipeline Flow (image input)
1. `extract_layout_surya_som(img)` — Surya OCR → pixel-accurate blocks → SoM annotations → Claude Sonnet
2. If Surya fails → `extract_full_layout_via_tool_use(img)` — Claude Vision only (lower quality)
3. Both paths require working Claude API

### MongoDB Flow
- `POST /process` → pipeline runs → saves to MongoDB via `upsert_menu()`
- `GET /menus` → list all saved menus
- `GET /menus/{id}/template` + `GET /menus/{id}/data` → fetch for canvas preview
- `seed_mongo.py` — one-time seeder for existing processed outputs

### UI Flow
- Upload file → process → MongoDB save → auto-preview in iframe
- "Previously Processed" dropdown → session-only (files processed in current browser session)
- Select from dropdown → loads canvas preview via `renderer.html?id={mongo_id}`

---

## Branch Strategy
- `main` — deployment-ready code (deployment files, MongoDB, UI)
- `dev` — active development + what's deployed on VM
- Dev has improved pipeline: single Surya pass, no parallel ensemble, box detection, snap headers

## Key Files
| File | Purpose |
|------|---------|
| `main.py` | FastAPI app, all endpoints |
| `pipeline.py` | Main processing pipeline |
| `claude_extractor.py` | Surya OCR + SoM + Claude extraction |
| `separator.py` | Line/box detection (OpenCV) |
| `models.py` | Pydantic models |
| `database.py` | Motor/MongoDB async client |
| `seed_mongo.py` | One-time DB seeder |
| `static/index.html` | Main UI |
| `static/renderer.html` | Canvas preview renderer |
| `requirements-prod.txt` | VM dependencies |

---

## Next Steps (Priority Order)
1. **Fix Claude API 403** — contact Anthropic support or recreate VM in US region
2. **Verify Surya fix** — push dummy-inference fix, test on VM
3. **Test full pipeline** — upload image, confirm Surya runs + Claude processes
4. **Verify Previously Processed dropdown** — process a file, confirm it appears, click → preview loads
5. **Dev → main merge** — once everything works on dev, bring to main
