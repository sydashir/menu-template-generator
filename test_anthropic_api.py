import os
import anthropic
from dotenv import load_dotenv

load_dotenv()

def test_model(model_name):
    print(f"Testing model: {model_name}...")
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    
    kwargs = {"api_key": key}
    if base_url:
        kwargs["base_url"] = base_url
        print(f"Using base_url: {base_url}")
        
    client = anthropic.Anthropic(**kwargs)
    
    try:
        response = client.messages.create(
            model=model_name,
            max_tokens=10,
            messages=[{"role": "user", "content": "Hello, what is your model name?"}]
        )
        print(f"✅ Success! Response: {response.content[0].text}")
        return True
    except Exception as e:
        print(f"❌ Failed: {e}")
        return False

if __name__ == "__main__":
    # Candidates based on latest 2026 research
    candidates = [
        "claude-3-5-sonnet-latest",
        "claude-3-5-sonnet-20241022",
        "claude-3-opus-latest",
        "claude-sonnet-4-6",
        "claude-opus-4-7"
    ]
    
    for model in candidates:
        if test_model(model):
            print(f"\nRecommended model name: {model}")
            # break # Test all to see what's available
        print("-" * 30)
