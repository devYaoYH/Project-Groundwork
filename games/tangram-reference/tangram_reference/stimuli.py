"""Deterministic rasterization of exact seven-piece tangrams (no captions)."""

from io import BytesIO
from PIL import Image, ImageDraw

# A 4x4 standard tangram: areas 4,4,2,1,1,2,2; no interior overlaps.
STANDARD = [
    [(0, 0), (4, 0), (2, 2)],
    [(0, 0), (2, 2), (0, 4)],
    [(4, 4), (2, 4), (4, 2)],
    [(4, 0), (4, 2), (3, 1)],
    [(2, 2), (3, 3), (1, 3)],
    [(3, 1), (4, 2), (3, 3), (2, 2)],
    [(0, 4), (1, 3), (3, 3), (2, 4)],
]


def polygons(index):
    pieces = [[tuple(p) for p in polygon] for polygon in STANDARD]
    # Reflect boundary triangles across the outer x=4 edge. These attach by
    # an entire edge, retain exact shape and never overlap another interior.
    if index in (1, 3):
        pieces[2] = [(8 - x, y) for x, y in STANDARD[2]]
    if index in (2, 3):
        pieces[3] = [(8 - x, y) for x, y in STANDARD[3]]
    return pieces


def figure(index, size=200):
    im = Image.new("RGB", (size, size), "white")
    draw = ImageDraw.Draw(im)
    # All images use the same world scale and center, preserving geometry.
    for polygon in polygons(index):
        draw.polygon(
            [((x + 1) * size / 7, (y + 1) * size / 7) for x, y in polygon], fill="black"
        )
    return im


def panel(indices, labels):
    width = 240
    im = Image.new("RGB", (width * len(indices), 260), "white")
    draw = ImageDraw.Draw(im)
    for i, (index, label) in enumerate(zip(indices, labels, strict=True)):
        im.paste(figure(index, 220), (i * width + 10, 30))
        draw.text((i * width + 90, 0), str(label), fill="black", font_size=22)
    output = BytesIO()
    im.save(output, format="PNG")
    return output.getvalue()
