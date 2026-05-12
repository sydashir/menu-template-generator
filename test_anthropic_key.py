
import os
import anthropic
from dotenv import load_dotenv

def test_key():
    # Force load .env (override existing environment variables)
    load_dotenv(override=True)
    
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip().strip('"').strip("'")
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    
    if not api_key:
        print("❌ ERROR: ANTHROPIC_API_KEY not found in .env file!")
        return

    # Check for hidden/non-printable characters
    import string
    printable = set(string.printable)
    cleaned_key = "".join(filter(lambda x: x in printable, api_key))
    if len(cleaned_key) != len(api_key):
        print(f"⚠️ WARNING: Key contains {len(api_key) - len(cleaned_key)} non-printable characters! Cleaning them...")
        api_key = cleaned_key

    # Check for common typo: missing 's' in 'sk-ant'
    if api_key.startswith("k-ant"):
        print("⚠️ WARNING: Your key starts with 'k-ant' instead of 'sk-ant'. Adding the 's' for this test...")
        api_key = "s" + api_key

    print(f"Testing key starting with: {api_key[:10]}...")
    if base_url:
        print(f"Using custom BASE_URL: {base_url[:30]}...")
    else:
        print("Using default Anthropic BASE_URL (https://api.anthropic.com)")

    client = anthropic.Anthropic(api_key=api_key, base_url=base_url if base_url else None)

    # Models to test - exhaustive variations
    models_to_test = [
        "claude-3-5-sonnet-20241022",
        "claude-3-opus-20240229",
        "claude-3-5-sonnet-latest",
        "claude-sonnet-4-6"  # Used in your project code
    ]

    for model in models_to_test:
        print(f"\n--- Testing model: {model} ---")
        try:
            response = client.messages.create(
                model=model,
                max_tokens=10,
                messages=[{"role": "user", "content": "Hello"}]
            )
            print(f"✅ SUCCESS! Response: {response.content[0].text}")
        except anthropic.AuthenticationError:
            print(f"❌ AUTH ERROR (401): The API Key itself is invalid.")
            break # No point testing other models if key is bad
        except anthropic.NotFoundError:
            print(f"⚠️ NOT FOUND (404): The model name '{model}' is not recognized.")
        except Exception as e:
            print(f"❌ OTHER ERROR: {type(e).__name__}: {str(e)}")

if __name__ == "__main__":
    test_key()
