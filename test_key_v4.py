import os
import anthropic
from dotenv import load_dotenv

load_dotenv()
key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# Remove quotes if they exist
if (key.startswith('"') and key.endswith('"')) or (key.startswith("'") and key.endswith("'")):
    key = key[1:-1]

print(f"Key used: {key[:15]}... (length: {len(key)})")
client = anthropic.Anthropic(api_key=key)

models = ["claude-sonnet-4-6"]

for m in models:
    print(f"Trying {m}...")
    try:
        res = client.messages.create(
            model=m,
            max_tokens=5,
            messages=[{"role": "user", "content": "hi"}]
        )
        print(f"✅ Success with {m}: {res.content[0].text}")
    except Exception as e:
        print(f"❌ {m} failed: {e}")
