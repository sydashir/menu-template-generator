import sys

with open("claude_extractor.py", "r") as f:
    lines = f.readlines()

start_marker = '    # Encode annotated image (IMAGE 2 — for OCR block spatial reference)'
end_marker = '    try:'
# Find the start and end of the broken section
start_idx = -1
end_idx = -1

for i, line in enumerate(lines):
    if start_marker in line and start_idx == -1:
        start_idx = i
    if end_marker in line and start_idx != -1:
        end_idx = i
        break

if start_idx != -1 and end_idx != -1:
    new_section = [
        '    # Encode annotated image (IMAGE 2 — for OCR block spatial reference)\n',
        '    buf = io.BytesIO()\n',
        '    send_img.convert("RGB").save(buf, format="JPEG", quality=85)\n',
        '    annotated_b64 = base64.standard_b64encode(buf.getvalue()).decode()\n',
        '\n',
        '    # Encode clean image (IMAGE 1 — for accurate text reading and decorative element location)\n',
        '    clean_buf = io.BytesIO()\n',
        '    clean_send.convert("RGB").save(clean_buf, format="JPEG", quality=85)\n',
        '    clean_b64 = base64.standard_b64encode(clean_buf.getvalue()).decode()\n',
        '\n',
        '    # Build the multi-image message for Claude\n',
        '    content_blocks = [\n',
        '        {"type": "text", "text": "I am providing 4 high-resolution overlapping quadrants of the menu to ensure small logos are visible."},\n',
        '    ]\n',
        '    \n',
        '    for q in quadrants:\n',
        '        content_blocks.append({"type": "text", "text": f"Quadrant {q[\'id\']}:"})\n',
        '        content_blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": q["b64"]}})\n',
        '\n',
        '    content_blocks.extend([\n',
        '        {"type": "text", "text": "IMAGE 1 — Clean original (full view):"},\n',
        '        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": clean_b64}},\n',
        '        {"type": "text", "text": "IMAGE 2 — Annotated (spatial reference):"},\n',
        '        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": annotated_b64}},\n',
        '        {"type": "text", "text": user_msg}\n',
        '    ])\n',
        '\n'
    ]
    lines[start_idx:end_idx] = new_section
    with open("claude_extractor.py", "w") as f:
        f.writelines(lines)
    print("Fixed syntax and indentation")
else:
    print(f"Markers not found: {start_idx}, {end_idx}")
