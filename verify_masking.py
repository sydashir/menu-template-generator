import json

def verify_logo_masking():
    with open("test_results/SRQ_2_fixed/SRQ_2_fixed_template.json", "r") as f:
        template = json.load(f)
    
    logos = [e for e in template["elements"] if e["type"] == "logo"]
    if not logos:
        print("No logo found!")
        return
    
    logo = logos[0]
    lbd = logo["bbox"]
    lx1, ly1 = lbd["x"], lbd["y"]
    lx2, ly2 = lx1 + lbd["w"], ly1 + lbd["h"]
    
    print(f"Logo BBox: {lx1}, {ly1} to {lx2}, {ly2}")
    
    inside = []
    for el in template["elements"]:
        if el["type"] == "text":
            bd = el["bbox"]
            cx = bd["x"] + bd["w"] / 2
            cy = bd["y"] + bd["h"] / 2
            if lx1 <= cx <= lx2 and ly1 <= cy <= ly2:
                inside.append(el)
    
    if inside:
        print(f"FAILED: Found {len(inside)} text elements inside logo:")
        for el in inside:
            print(f"  - {el['content']} at {el['bbox']}")
    else:
        print("SUCCESS: No text elements found inside logo.")

if __name__ == "__main__":
    verify_logo_masking()
