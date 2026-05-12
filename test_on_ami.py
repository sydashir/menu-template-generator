import os
import json
from pipeline import process

def test_ami_dinner():
    file_path = "/Users/ashir/Downloads/AMI FFL DINNER MENU Combined (4).pdf"
    output_dir = "outputs/AMI_FFL_DINNER_TEST"
    
    print(f"Processing {file_path}...")
    try:
        results = process(file_path, output_dir)
        print(f"Successfully processed. Results saved to {output_dir}")
        for r in results:
            print(f"Page {r.get('page')}: template={r.get('template')}")
            
            # Load the first template to check for graphic elements
            with open(r['template'], 'r') as f:
                template = json.load(f)
                graphics = [e for e in template['elements'] if e['type'] in ('logo', 'separator', 'image')]
                print(f"  Found {len(graphics)} graphic elements in template.")
                for g in graphics[:5]:
                    print(f"    - {g['type']} ({g.get('subtype', 'N/A')}): bbox={g['bbox']}")

    except Exception as e:
        print(f"Error processing file: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_ami_dinner()
