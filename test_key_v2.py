import os
import anthropic
from dotenv import load_dotenv

# Load .env manually to ensure we see exactly what is in there
with open(".env", "r") as f:
    for line in f:
        if "ANTHROPIC_API_KEY" in line:
            key = line.split("=")[1].strip().strip('"').strip("'")
            os.environ["ANTHROPIC_API_KEY"] = key
            print(f"Loaded key (length {len(key)}) starting with {key[:10]}...")

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

models = [
    "claude-3-7-sonnet-20250219",
    "claude-3-5-sonnet-20241022",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
    "claude-5-sonnet-preview"
]

for m in models:
    print(f"Trying {m}...")
    try:
        res = client.messages.create(
            model=m,
            max_tokens=5,
            messages=[{"role": "user", "content": "hi"}]
        )
        print(f"✅ Success with {m}: {res.content[0].text}")
        break
    except Exception as e:
        print(f"❌ {m} failed: {e}")
