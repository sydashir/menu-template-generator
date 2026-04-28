import os
from google import genai
from dotenv import load_dotenv

load_dotenv()
key = (os.environ.get("GOOGLE_API_KEY") or 
       os.environ.get("GEMINI_API_KEY") or 
       os.environ.get("GOOGLE_GENAI_API_KEY"))

if not key:
    print("No key found")
    exit(1)

client = genai.Client(api_key=key)
try:
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=["hi"]
    )
    print(f"✅ Gemini works: {response.text}")
except Exception as e:
    print(f"❌ Gemini failed: {e}")
