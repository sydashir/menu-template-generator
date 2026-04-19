# Full Context — Menu Template Generator

## What This App Does
A FastAPI web app that takes a restaurant menu (PDF or image), runs OCR + AI to extract all text with pixel-accurate bounding boxes, and renders an editable template on a canvas. Users can download the structured JSON output.

---

## Tech Stack
- **Backend**: Python 3.12, FastAPI, uvicorn
- **OCR**: surya-ocr 0.17.1 (FoundationPredictor + RecognitionPredictor + DetectionPredictor)
- **AI**: Anthropic Claude (claude-sonnet-4-6) — used to semantically label OCR blocks
- **Database**: MongoDB Atlas via Motor (async)
- **Frontend**: Vanilla JS, HTML canvas
- **Deployment**: Azure VM (Ubuntu), systemd service

---

## Server Details
- **VM**: Azure VM, Ubuntu
- **IP**: 20.187.152.110
- **Port**: 8000
- **App URL**: http://20.187.152.110:8000/static/index.html
- **Service name**: menu-generator
- **App directory**: /home/azureuser/menu-template-generator
- **Python venv**: /home/azureuser/menu-template-generator/venv
- **Service file**: /etc/systemd/system/menu-generator.service

---

## Service Commands
```bash
sudo systemctl start menu-generator
sudo systemctl stop menu-generator
sudo systemctl restart menu-generator
sudo journalctl -u menu-generator -f        # live logs
sudo journalctl -u menu-generator -n 50     # last 50 lines
```

---

## Key Files
```
menu-template-generator/
├── main.py                  # FastAPI app, routes, MongoDB upsert
├── pipeline.py              # orchestrates OCR → Claude → template
├── claude_extractor.py      # surya OCR + Claude API calls
├── database.py              # Motor/MongoDB async helpers
├── static/
│   ├── index.html           # upload UI, session dropdown
│   └── renderer.html        # canvas preview
├── requirements-prod.txt    # production dependencies
├── .env                     # secrets (not in git)
└── DEPLOYMENT_NOTES.md      # deployment notes
```

---

## Pipeline Flow
1. User uploads menu image/PDF via `/process` endpoint
2. `pipeline.py` calls `extract_layout_surya_som(img)`:
   - Surya OCR extracts text blocks with pixel-accurate bounding boxes
   - Set-of-Marks (SoM) annotations overlaid on image
   - Claude API call to semantically label each block (item name, price, description, section header, etc.)
3. If surya_som fails → falls back to `extract_full_layout_via_tool_use(img)` (Claude Vision only)
4. Result stored in MongoDB
5. Frontend renders template on canvas via `/menus/{id}/template` and `/menus/{id}/data`

---

## Environment Variables (.env on VM)
```
ANTHROPIC_API_KEY=sk-ant-...        # Anthropic API key
ANTHROPIC_BASE_URL=https://wispy-bonus-9bb4.meetashirr.workers.dev   # Cloudflare proxy (not working)
MONGODB_URI=mongodb+srv://...       # MongoDB Atlas connection string
MONGODB_DB=menu_generator
DEPLOY_MODE=prod
```

---

## THE MAIN PROBLEM — Anthropic API 403

### Symptom
Every Claude API call from the VM returns:
```json
{"error": {"type": "forbidden", "message": "Request not allowed"}}
```

### What we know for certain
- The API key works perfectly from local Mac
- The SAME valid key gives 403 from the Azure VM
- Both the old VM key AND the local Mac key give 403 from the VM
- Direct curl to api.anthropic.com from VM → 403
- Curl through Cloudflare Worker proxy from VM → also 403
- Curl from local Mac to api.anthropic.com → works fine

### Root Cause
Anthropic blocks Azure datacenter IP ranges. The VM IP `20.187.152.110` is an Azure datacenter IP and is blocked by Anthropic at the network level.

### What we tried
1. Changed API key → still 403
2. Created Cloudflare Worker proxy (wispy-bonus-9bb4.meetashirr.workers.dev) → still 403 (Cloudflare outbound IPs also blocked or Cloudflare passes origin IP headers)
3. Stripped CF-* headers in the Worker → still 403
4. Added `ANTHROPIC_BASE_URL` env var support in code → proxy still blocked

### What has NOT been tried
- OpenRouter.ai as API proxy (Claude through their non-blocked IPs)
- Contacting Anthropic support to whitelist the IP
- Moving VM to a different provider (DigitalOcean, Hetzner, etc.)
- Setting up a VPN on the VM

---

## Surya OCR Status — FIXED
Surya 0.17.1 has a different API than 0.4.x. After multiple fixes:

### Working initialization (claude_extractor.py):
```python
from surya.foundation import FoundationPredictor
from surya.recognition import RecognitionPredictor
from surya.detection import DetectionPredictor

_surya_foundation_predictor = FoundationPredictor()
_surya_rec_predictor = RecognitionPredictor(_surya_foundation_predictor)
_surya_det_predictor = DetectionPredictor()
```

### Working OCR call:
```python
results = _surya_rec_predictor([img], det_predictor=_surya_det_predictor)
```

Surya now extracts ~49 lines successfully from test menus. Confirmed in logs:
```
[surya] models ready (API v0.17+)
[surya] 49 lines extracted
```

The pipeline then fails at the Claude API call step.

---

## What Good Output Looks Like (local)
On local Mac (where Claude works), the pipeline produces a pixel-accurate template with:
- All menu items with exact bounding boxes
- Section headers identified
- Prices extracted
- Descriptions labeled

On VM (Claude blocked), output is an empty/partial template — just the raw surya bounding boxes with no semantic labels.

---

## Code: _get_client() in claude_extractor.py
```python
def _get_client() -> anthropic.Anthropic | None:
    global _client
    if _client is not None:
        return _client
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    kwargs: dict = {"api_key": key}
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    if base_url:
        kwargs["base_url"] = base_url
    _client = anthropic.Anthropic(**kwargs)
    return _client
```

---

## Cloudflare Worker Code (currently deployed, not working)
```javascript
export default {
  async fetch(request) {
    const url = new URL(request.url);
    url.hostname = "api.anthropic.com";
    url.protocol = "https:";
    url.port = "";

    const cleanHeaders = new Headers();
    for (const [key, value] of request.headers) {
      const k = key.toLowerCase();
      if (k.startsWith("cf-") || k === "x-forwarded-for" || k === "x-real-ip") {
        continue;
      }
      cleanHeaders.set(key, value);
    }

    return fetch(url.toString(), {
      method: request.method,
      headers: cleanHeaders,
      body: request.body,
    });
  }
}
```
Worker URL: `https://wispy-bonus-9bb4.meetashirr.workers.dev`

---

## GitHub
- Repo: https://github.com/sydashir/menu-template-generator
- Active branch: `dev`

---

## What I Need Help With
**How to make Anthropic API calls work from an Azure VM that is IP-blocked by Anthropic?**

Options being considered:
1. **OpenRouter.ai** — proxy service that provides Claude through their own API. Requires switching from `anthropic` Python SDK to `openai` SDK (OpenRouter is OpenAI-compatible). Need help implementing this.
2. **Anthropic support** — ask them to whitelist IP 20.187.152.110
3. **Different VM** — move to a provider whose IPs are not blocked

Preferred: Option 1 (OpenRouter) — need the code changes to switch all `anthropic.messages.create()` calls to use OpenRouter's OpenAI-compatible API.
