import os
import sys
from dotenv import load_dotenv

# Force load .env
load_dotenv(override=True)

def check_api():
    print("\n--- 1. Testing Anthropic API ---")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("❌ ERROR: ANTHROPIC_API_KEY is missing.")
        return False
    
    print(f"Key loaded: {api_key[:10]}... (Length: {len(api_key)})")
    
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        # Using the exact model name from claude_extractor.py
        model_name = "claude-sonnet-4-6"
        print(f"Pinging model: {model_name}...")
        response = client.messages.create(
            model=model_name,
            max_tokens=10,
            messages=[{"role": "user", "content": "Ping!"}]
        )
        print("✅ API SUCCESS! Model is reachable and key is valid.")
        return True
    except Exception as e:
        print(f"❌ API FAILED: {type(e).__name__} - {e}")
        return False

def check_s3():
    print("\n--- 2. Testing S3 Asset Library ---")
    try:
        from s3_asset_library import _get_s3, resolve_asset, _S3_BUCKET, _S3_PREFIX
        s3 = _get_s3()
        if not s3:
            print("❌ S3 client failed to initialize. Check AWS credentials.")
            return False
            
        print(f"S3 Client initialized. Target Bucket: {_S3_BUCKET}, Prefix: {_S3_PREFIX}")
        
        # Try to resolve a known asset
        test_slug = "badge/youtube"
        print(f"Attempting to resolve '{test_slug}'...")
        asset_bytes = resolve_asset(test_slug)
        
        if asset_bytes:
            print(f"✅ S3 SUCCESS! Fetched {test_slug} ({len(asset_bytes)} bytes).")
            return True
        else:
            print(f"⚠️ S3 WARNING: Client connected, but '{test_slug}' not found.")
            return False
    except Exception as e:
        print(f"❌ S3 FAILED: {type(e).__name__} - {e}")
        return False

def check_cv():
    print("\n--- 3. Testing CV Graphics Detector ---")
    try:
        from PIL import Image
        import numpy as np
        import cv2
        from claude_extractor import detect_graphical_candidates
        
        # Create a test image
        img_arr = np.ones((800, 800, 3), dtype=np.uint8) * 255
        cv2.circle(img_arr, (400, 400), 50, (0, 0, 0), -1) # Draw a fake badge
        img = Image.fromarray(img_arr)
        
        print("Running detect_graphical_candidates...")
        candidates = detect_graphical_candidates(img)
        print(f"✅ CV SUCCESS! Found {len(candidates)} candidates without crashing.")
        return True
    except Exception as e:
        print(f"❌ CV FAILED: {type(e).__name__} - {e}")
        return False

if __name__ == "__main__":
    print("Starting Comprehensive System Check...")
    api_ok = check_api()
    s3_ok = check_s3()
    cv_ok = check_cv()
    
    print("\n==============================")
    if api_ok and s3_ok and cv_ok:
        print("🎉 ALL SYSTEMS GO! YOU ARE 100% READY TO TEST.")
        sys.exit(0)
    else:
        print("⚠️ SOME SYSTEMS FAILED. REVIEW LOGS ABOVE.")
        sys.exit(1)
