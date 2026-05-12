
import cv2
import numpy as np
from PIL import Image
from claude_extractor import detect_graphical_candidates

def test_graphic_detection():
    # Create a dummy image with a circle and a rectangle
    img_arr = np.ones((1000, 1000, 3), dtype=np.uint8) * 255
    # Draw a circle (potential badge)
    cv2.circle(img_arr, (500, 500), 50, (0, 0, 0), -1)
    # Draw a rectangle (potential collage box)
    cv2.rectangle(img_arr, (100, 100), (400, 400), (0, 0, 0), 5)
    
    img = Image.fromarray(img_arr)
    candidates = detect_graphical_candidates(img)
    
    print(f"Found {len(candidates)} candidates")
    for i, c in enumerate(candidates):
        print(f"Candidate {i+1}: Type={c['type']}, BBox={c['bbox']}")

if __name__ == "__main__":
    test_graphic_detection()
