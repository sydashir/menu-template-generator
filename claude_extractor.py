import io
import os
import json
import base64

import anthropic
from dotenv import load_dotenv
from PIL import Image

from models import MenuData, MenuCategory, MenuItem

load_dotenv()

_client: anthropic.Anthropic | None = None

_PROMPT = """\
Extract all menu content from this restaurant menu image.
Return a JSON object with this exact structure. Return only the JSON — no markdown, no explanation:
{
  "restaurant_name": "string or null",
  "tagline": "string or null",
  "address": "string or null",
  "phone": "string or null",
  "categories": [
    {
      "name": "category or section name",
      "column": 0,
      "items": [
        {
          "name": "item name",
          "description": "item description or null",
          "price": "price string or null"
        }
      ]
    }
  ]
}
For multi-column layouts use column=0 for left column, column=1 for right column.
Keep prices as strings: "$12" becomes "12", ranges like "18/21" stay as "18/21".\
"""


def _get_client() -> anthropic.Anthropic | None:
    global _client
    if _client is not None:
        return _client
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    _client = anthropic.Anthropic(api_key=key)
    return _client


def extract_menu_via_claude(img: Image.Image) -> dict | None:
    """Send image to Claude vision and return parsed menu JSON, or None if unavailable."""
    client = _get_client()
    if client is None:
        return None

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    image_b64 = base64.standard_b64encode(buf.getvalue()).decode("utf-8")

    try:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": _PROMPT},
                ],
            }],
        )
    except anthropic.APIError:
        return None

    text = response.content[0].text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1].lstrip("json").strip() if len(parts) > 1 else text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def build_menu_data_from_claude(
    data: dict,
    source_file: str,
    side: str,
    num_separators: int,
    num_columns: int,
) -> MenuData:
    categories = []
    for cat_d in data.get("categories", []):
        cat = MenuCategory(name=cat_d.get("name", ""), column=cat_d.get("column", 0))
        for item_d in cat_d.get("items", []):
            cat.items.append(MenuItem(
                name=item_d.get("name", ""),
                description=item_d.get("description"),
                price=item_d.get("price"),
            ))
        categories.append(cat)

    return MenuData(
        source_file=source_file,
        side=side,
        restaurant_name=data.get("restaurant_name"),
        tagline=data.get("tagline"),
        address=data.get("address"),
        phone=data.get("phone"),
        categories=categories,
        num_separators=num_separators,
        num_columns=num_columns,
        layout_notes=(
            f"{num_columns}-column layout, {len(categories)} sections detected via Claude vision."
        ),
    )
