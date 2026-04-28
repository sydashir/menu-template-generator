import sys

with open("claude_extractor.py", "r") as f:
    lines = f.readlines()

new_lines = []
skip = False
for line in lines:
    if "    # Block list for Claude — absolute pixel coords" in line:
        skip = True
        # Insert the unified block
        new_lines.append("""    # Block list for Claude — absolute pixel coords in Claude's image space (sw×sh)
    # so Claude can anchor decorative element bboxes precisely relative to OCR blocks.
    lines = []
    for i, b in enumerate(surya_blocks):
        x1, y1, x2, y2 = b["bbox"]
        # Convert from original (upscaled) image coords → Claude's send_img coords
        cx1 = x1 / scale_x if scale_x != 1.0 else x1
        cy1 = y1 / scale_y if scale_y != 1.0 else y1
        cw  = (x2 - x1) / scale_x if scale_x != 1.0 else (x2 - x1)
        ch  = (y2 - y1) / scale_y if scale_y != 1.0 else (y2 - y1)
        text = b["text"] if len(b["text"]) <= 80 else b["text"][:77] + "..."
        lines.append(
            f"[{i + 1}] \\\"{text}\\\" — x={cx1:.0f} y={cy1:.0f} w={cw:.0f} h={ch:.0f}"
        )
    block_list = "\\n".join(lines)
    
    # Add graphical candidate list
    g_lines = []
    for i, g in enumerate(graphic_candidates):
        x1, y1, x2, y2 = g["bbox"]
        cx1, cy1 = x1 / scale_x, y1 / scale_y
        cw, ch = (x2 - x1) / scale_x, (y2 - y1) / scale_y
        g_lines.append(f"[G{i + 1}] {g['type']} — x={cx1:.0f} y={cy1:.0f} w={cw:.0f} h={ch:.0f}")
    graphic_list = "\\n".join(g_lines)

    sw, sh = send_img.size
    user_msg = (
        f"Both images are {sw}×{sh}px. All bbox values must be in this pixel space.\\n\\n"
        "Follow the 6-step process from the system prompt exactly.\\n\\n"
        "═══ STEP 1 — SKELETON SCAN ═══\\n"
        "Scan the High-Resolution Quadrants now. Identify every section/category header.\\n\\n"
        "═══ STEP 2 — OCR BLOCKS ═══\\n"
        f"Surya OCR extracted {len(surya_blocks)} blocks:\\n"
        f"{block_list}\\n\\n"
        "═══ STEP 3 — GRAPHICAL CANDIDATES ═══\\n"
        "Pre-pass detected potential graphical regions (Magenta boxes G1, G2, etc.):\\n"
        f"{graphic_list or '(none)'}\\n"
        "Identify these in the graphic_labels tool field.\\n\\n"
        "═══ STEP 4 — DECORATIVE ELEMENTS ═══\\n"
        "SECTION HEADERS: For every cursive/script header from Step 1 with no numbered OCR block.\\n\\n"
        "═══ STEP 5 — LOGO BBOX ═══\\n"
        "Draw ONE bbox tightly around the PRIMARY restaurant branding in IMAGE 1.\\n\\n"
        "═══ STEP 6 — GRAPHIC ELEMENTS ═══\\n"
        "Scan the Quadrants for ANY OTHER non-text graphical regions not already labeled.\\n"
        "CRITICAL — use exact semantic_label slugs:\\n"
        "  Food Network → badge/food_network\\n"
        "  OpenTable / Diners' Choice → badge/opentable_diners_choice\\n"
        "  YouTube → badge/youtube   Hulu → badge/hulu\\n"
        "  TripAdvisor → badge/tripadvisor   Yelp → badge/yelp\\n"
    )

    # Encode annotated image (IMAGE 2 — for OCR block spatial reference)
    buf = io.BytesIO()
    send_img.convert("RGB").save(buf, format="JPEG", quality=85)
    annotated_b64 = base64.standard_b64encode(buf.getvalue()).decode()

    # Encode clean image (IMAGE 1 — for accurate text reading and decorative element location)
    clean_buf = io.BytesIO()
    clean_send.convert("RGB").save(clean_buf, format="JPEG", quality=85)
    clean_b64 = base64.standard_b64encode(clean_buf.getvalue()).decode()

    # Build the multi-image message for Claude
    content_blocks = [
        {"type": "text", "text": "I am providing 4 high-resolution overlapping quadrants of the menu to ensure small logos are visible."},
    ]
    
    for q in quadrants:
        content_blocks.append({"type": "text", "text": f"Quadrant {q['id']}:"})
        content_blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": q["b64"]}})

    content_blocks.extend([
        {"type": "text", "text": "IMAGE 1 — Clean original (full view):"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": clean_b64}},
        {"type": "text", "text": "IMAGE 2 — Annotated (spatial reference):"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": annotated_b64}},
        {"type": "text", "text": user_msg}
    ])
""")
        continue
    
    if "    try:" in line and skip:
        # Re-start adding lines from the try block
        skip = False
    
    if not skip:
        new_lines.append(line)

with open("claude_extractor.py", "w") as f:
    f.writelines(new_lines)
