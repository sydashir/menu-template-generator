import json
from hybrid_engine import validate_graphic_elements

def test_hybrid_logic():
    print("Testing Hybrid Engine Logic...")
    
    # Mock text elements
    text_elements = [
        {"type": "text", "subtype": "restaurant_name", "bbox": {"x": 100, "y": 50, "w": 200, "h": 50}, "content": "The Gourmet Hub"},
        {"type": "text", "subtype": "category_header", "bbox": {"x": 50, "y": 200, "w": 300, "h": 40}, "content": "Starters"},
        {"type": "text", "subtype": "item_name", "bbox": {"x": 50, "y": 260, "w": 150, "h": 20}, "content": "Bruschetta"},
    ]
    
    # Mock raw CV extractions
    raw_cv_lines = [
        # Line between header and item (Valid separator)
        {"bbox": {"x": 50, "y": 245, "w": 300, "h": 2}, "orientation": "horizontal"}
    ]
    
    raw_cv_contours = [
        # Blob at the top (Potential Logo)
        {"bbox": {"x": 150, "y": 10, "w": 100, "h": 30}},
        # Blob overlapping with text (Should be filtered)
        {"bbox": {"x": 100, "y": 55, "w": 50, "h": 20}},
    ]
    
    matched_assets = [
        # Matched badge from template matching
        {"type": "image", "subtype": "badge", "semantic_label": "badge/michelin", "bbox": {"x": 300, "y": 20, "w": 40, "h": 40}, "id": "matched_michelin_1"}
    ]
    
    canvas_w, canvas_h = 1000, 1500
    
    results = validate_graphic_elements(
        text_elements=text_elements,
        raw_cv_lines=raw_cv_lines,
        raw_cv_contours=raw_cv_contours,
        matched_assets=matched_assets,
        canvas_w=canvas_w,
        canvas_h=canvas_h
    )
    
    print(f"Results: {json.dumps(results, indent=2)}")
    
    # Verify results
    types = [r["type"] for r in results]
    assert "logo" in types, "Logo not detected"
    assert "separator" in types, "Separator not detected"
    assert "image" in types, "Matched badge/ornament not detected"
    
    # Ensure overlap filtering worked (only 1 image/ornament should remain, or the logo)
    # 2 contours provided: 1 logo, 1 should be filtered.
    images = [r for r in results if r["type"] == "image"]
    # 1 from matched_assets, 0 from valid_contours (because the only valid one became logo)
    assert len(images) == 1, f"Expected 1 image, found {len(images)}"
    
    print("✅ Hybrid logic test PASSED!")

if __name__ == "__main__":
    test_hybrid_logic()
