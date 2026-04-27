import os
import anthropic
from dotenv import load_dotenv

load_dotenv()
key = os.environ.get("ANTHROPIC_API_KEY", "").strip().strip('"').strip("'")
client = anthropic.Anthropic(api_key=key)

models = [
    "claude-3-5-sonnet-latest",
    "claude-3-5-sonnet-20241022",
    "claude-sonnet-4-6"
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
    except Exception as e:
        print(f"❌ {m} failed: {e}")
