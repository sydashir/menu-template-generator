import cv2
import numpy as np
from PIL import Image
import os

def detect_graphical_candidates_debug(img_path):
    img = Image.open(img_path)
    arr = np.array(img.convert("RGB"))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape

    # 1. Circle Detection (Hough Circles)
    blurred_circle = cv2.medianBlur(gray, 5)
    circles = cv2.HoughCircles(
        blurred_circle, cv2.HOUGH_GRADIENT, dp=1, minDist=100,
        param1=50, param2=30, minRadius=40, maxRadius=300
    )
    
    debug_img = arr.copy()
    
    if circles is not None:
        circles = np.uint16(np.around(circles))
        for i in circles[0, :]:
            cx, cy, r = i
            cv2.circle(debug_img, (cx, cy), r, (0, 255, 0), 4)
            cv2.putText(debug_img, "Circle", (cx-r, cy-r-10), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

    # 2. Bordered Box Detection (Contours)
    thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
                                 cv2.THRESH_BINARY_INV, 15, 4)
    
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 11))
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
    
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < 5000 or area > (h * w * 0.3): continue
        x, y, cw, ch = cv2.boundingRect(cnt)
        cv2.rectangle(debug_img, (x, y), (x+cw, y+ch), (255, 0, 0), 4)
        cv2.putText(debug_img, "Box", (x, y-10), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 0, 0), 2)

    output_path = "opencv_debug.png"
    cv2.imwrite(output_path, cv2.cvtColor(debug_img, cv2.COLOR_RGB2BGR))
    print(f"Debug image saved to {output_path}")

if __name__ == "__main__":
    # Use one of the menu templates if available
    menu_dir = "Menu Template"
    if os.path.exists(menu_dir):
        files = [f for f in os.listdir(menu_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
        if files:
            detect_graphical_candidates_debug(os.path.join(menu_dir, files[0]))
        else:
            print("No images found in Menu Template")
    else:
        print("Menu Template dir not found")
