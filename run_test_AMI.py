from pipeline import process
from pathlib import Path
import json

def test_ami_brunch():
    image_path = "Menu Template/AMI_brunch_Lunch_Menu.JPG"
    output_dir = "test_results/AMI_fixed"
    file_stem = "AMI_fixed"
    
    print(f"Processing {image_path}...")
    results = process(image_path, output_dir, file_stem)
    
    print("\nResults:")
    for res in results:
        print(f"Side: {res['side']}, Elements: {res['num_elements']}, Categories: {res['num_categories']}")
        print(f"Template path: {res['template']}")
        print(f"Menu data path: {res['menu_data']}")

if __name__ == "__main__":
    test_ami_brunch()
