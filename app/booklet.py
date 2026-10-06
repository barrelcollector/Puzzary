"""Two-up, left-bound booklet imposition for folded duplex sheets."""
from io import BytesIO

from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import NameObject


def booklet_pairs(page_count: int) -> list[tuple[int, int]]:
    if page_count < 1:
        raise ValueError("A booklet needs at least one page")
    padded = ((page_count + 3) // 4) * 4
    pairs = []
    for sheet in range(padded // 4):
        low, high = sheet * 2, padded - 1 - sheet * 2
        pairs.extend([(high, low), (low + 1, high - 1)])
    return pairs


def booklet_layout(sheet: dict) -> dict:
    height = sheet["page_height_mm"]
    if height is None:
        raise ValueError("Booklets require a fixed sheet height; choose Letter, A4, or a custom sheet")
    width, height = sorted((sheet["page_width_mm"], height), reverse=True)
    margin = min(sheet["page_margin_mm"], max((min(width / 2, height) - 10) / 2, 0))
    return {**sheet, "page_width_mm": width / 2, "page_height_mm": height,
            "page_margin_mm": margin, "content_width_mm": width / 2 - 2 * margin}


def impose_booklet(data: bytes, sheet: dict) -> bytes:
    logical = booklet_layout(sheet)
    reader = PdfReader(BytesIO(data))
    writer = PdfWriter()
    width, height = logical["page_width_mm"] * 2 * 72 / 25.4, logical["page_height_mm"] * 72 / 25.4
    for pair in booklet_pairs(len(reader.pages)):
        side = writer.add_blank_page(width=width, height=height)
        for slot, index in enumerate(pair):
            if index >= len(reader.pages):
                continue
            source = reader.pages[index]
            box = source.mediabox
            scale = min((width / 2) / float(box.width), height / float(box.height))
            x = slot * width / 2 + (width / 2 - float(box.width) * scale) / 2
            y = (height - float(box.height) * scale) / 2
            transform = Transformation().translate(-float(box.left), -float(box.bottom)).scale(scale).translate(x, y)
            side.merge_transformed_page(source, transform)
    writer.create_viewer_preferences().update({
        NameObject("/Duplex"): NameObject("/DuplexFlipShortEdge"),
        NameObject("/PrintScaling"): NameObject("/None"),
    })
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
