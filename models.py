from __future__ import annotations
from typing import List, Optional, Literal, Dict, Any
from pydantic import BaseModel, Field, model_validator


class BBox(BaseModel):
    x: float
    y: float
    w: float
    h: float


class TextStyle(BaseModel):
    font_size: float = 12.0
    font_weight: Literal["normal", "bold"] = "normal"
    font_style: Literal["normal", "italic"] = "normal"
    font_family: str = "sans-serif"
    color: str = "#000000"
    text_align: Literal["left", "center", "right"] = "left"


class LineStyle(BaseModel):
    color: str = "#000000"
    stroke_width: float = 1.5
    stroke_style: Literal["solid", "dashed", "dotted"] = "solid"


SemanticType = Literal[
    "restaurant_name", "category_header", "item_name",
    "item_description", "item_price", "tagline",
    "address", "phone", "other_text",
]

SeparatorSubtype = Literal[
    "horizontal_line", "vertical_line",
    "decorative_divider", "border", "ornament",
]


class RawBlock(BaseModel):
    text: str
    x: float
    y: float
    w: float
    h: float
    font_size: float = 12.0
    is_bold: bool = False
    is_italic: bool = False
    # Generic 5-way category used as renderer fallback.
    font_family: str = "sans-serif"
    # Canonical PDF font name with subset prefix stripped (e.g. 'BrittanySignatureRegular').
    # Only set on the PDF path; empty for OCR.
    font_family_raw: str = ""
    color: str = "#000000"
    page: int = 0
    source: Literal["pdf", "ocr"] = "ocr"


class RawLine(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float
    orientation: Literal["horizontal", "vertical"] = "horizontal"
    subtype: Optional[str] = None  # e.g. "border" for detected rectangular boxes
    color: Optional[str] = None   # hex color from PDF stroke/fill (e.g. "#c8860a" for gold)


class TextElement(BaseModel):
    id: str
    type: Literal["text"] = "text"
    subtype: SemanticType
    bbox: BBox
    content: str
    style: TextStyle = Field(default_factory=TextStyle)
    column: int = 0


class LogoElement(BaseModel):
    id: str
    type: Literal["logo"] = "logo"
    bbox: BBox
    image_data: Optional[str] = None
    image_path: Optional[str] = None
    position_hint: str = "top_center"


class ImageElement(BaseModel):
    id: str
    type: Literal["image"] = "image"
    subtype: Literal["badge", "ornament", "collage_box"] = "badge"
    bbox: BBox
    image_data: Optional[str] = None
    semantic_label: Optional[str] = None


class SeparatorElement(BaseModel):
    id: str
    type: Literal["separator"] = "separator"
    subtype: SeparatorSubtype = "horizontal_line"
    orientation: Literal["horizontal", "vertical"] = "horizontal"
    bbox: BBox
    style: LineStyle = Field(default_factory=LineStyle)
    image_data: Optional[str] = None  # Base64-encoded clean PNG from S3 (e.g. for wavy_line)
    semantic_label: Optional[str] = None # Canonical S3 label


class CanvasMeta(BaseModel):
    width: int
    height: int
    unit: str = "px"
    background_color: str = "#ffffff"


class TemplateMeta(BaseModel):
    source_file: str
    page: int = 1
    side: str = "full"
    generated_at: str
    generator_version: str = "1.0.0"
    num_columns: int = 1


class FontAsset(BaseModel):
    """A font file embedded in the source document.

    `family` is the name used in TextStyle.font_family on text elements that
    use this font (subset prefix stripped). `data_base64` is the TTF/OTF binary
    base64-encoded — the renderer registers it via the FontFace API so text
    renders byte-exact to the source.
    """
    family: str
    data_base64: str
    weight: Literal["normal", "bold"] = "normal"
    style: Literal["normal", "italic"] = "normal"
    format: Literal["truetype", "opentype"] = "truetype"
    is_subset: bool = True


class Template(BaseModel):
    version: str = "1.0.0"
    metadata: TemplateMeta
    canvas: CanvasMeta
    elements: List[Dict[str, Any]]
    fonts: List[FontAsset] = []


class MenuItem(BaseModel):
    name: str
    description: Optional[str] = None
    price: Optional[str] = None
    
    @model_validator(mode='before')
    @classmethod
    def coerce_price_to_str(cls, data: Any) -> Any:
        if isinstance(data, dict) and "price" in data and data["price"] is not None:
            data["price"] = str(data["price"])
        return data



class MenuCategory(BaseModel):
    name: str
    column: int = 0
    items: List[MenuItem] = []


class MenuData(BaseModel):
    source_file: str
    side: str = "full"
    restaurant_name: Optional[str] = None
    tagline: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    categories: List[MenuCategory] = []
    logo_detected: bool = False
    num_separators: int = 0
    num_columns: int = 1
    layout_notes: str = ""
