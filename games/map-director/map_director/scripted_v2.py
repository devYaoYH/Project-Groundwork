"""Deterministic pilot-layout fixture: own raster markers plus public text only.

This policy knows the pilot's named landmark bands and axis formats. It is for
protocol smoke checks, not evidence of general visual or linguistic grounding.
"""

import base64
import re
from io import BytesIO
from PIL import Image
from .maps import local


def markers(image):
    pixels = image.load()
    remaining = {(x, y) for x in range(70, 711, 2) for y in range(90, 731, 2)
                 if pixels[x, y][:3] == (15, 118, 110)}
    centers = []
    while remaining:
        component, pending = [], [remaining.pop()]
        while pending:
            point = pending.pop()
            component.append(point)
            for delta in ((2, 0), (-2, 0), (0, 2), (0, -2)):
                adjacent = (point[0] + delta[0], point[1] + delta[1])
                if adjacent in remaining:
                    remaining.remove(adjacent)
                    pending.append(adjacent)
        x = sum(p[0] for p in component) / len(component)
        y = sum(p[1] for p in component) / len(component)
        centers.append([(x - 70) / 6.4, (730 - y) / 6.4])
    if len(centers) != 8:
        raise ValueError("V2 scripted fixture requires the eight pilot markers")
    bands = [("START cabin",), ("oak", "lake"), ("tower", "hill"),
             ("bridge", "windmill"), ("FINISH chapel",)]
    centers.sort(key=lambda point: point[1])
    named, cursor = {}, 0
    for names in bands:
        band = sorted(centers[cursor:cursor + len(names)], key=lambda point: point[0])
        named.update(zip(names, band))
        cursor += len(names)
    return named


def act(role, observation):
    image = Image.open(BytesIO(base64.b64decode(observation["content"][1]["data"]))).convert("RGB")
    points = markers(image)
    if role == "giver":
        start = points["START cabin"]
        pairs = [("oak", "lake"), ("tower", "hill")]
        def first_leg(pair):
            center = [(points[pair[0]][i] + points[pair[1]][i]) / 2 for i in range(2)]
            hits = 0
            for fraction in (0.12, 0.18, 0.24, 0.30):
                point = [start[i] + fraction * (center[i] - start[i]) for i in range(2)]
                x, y = round(70 + point[0] * 6.4), round(730 - point[1] * 6.4)
                if any((lambda color: color[0] > 150 and color[1] < 100 and color[2] < 100)(image.getpixel((x + dx, y + dy)))
                       for dx in range(-3, 4) for dy in range(-3, 4)):
                    hits += 1
            return hits
        pairs.sort(key=first_leg, reverse=True)
        pairs.append(("bridge", "windmill"))
        instructions = "; then ".join(f"between {a} and {b}" for a, b in pairs)
        return {"text": f"From START cabin pass {instructions}; finish at FINISH chapel. Use the middle of the gaps on your own map."}
    pairs = re.findall(r"between (\w+) and (\w+)", observation["incoming"])
    if len(pairs) != 3 or any(name not in points for pair in pairs for name in pair):
        return {"text": "Please identify the three ordered landmark pairs."}
    route = [points["START cabin"]] + [[(points[a][i] + points[b][i]) / 2 for i in range(2)] for a, b in pairs] + [points["FINISH chapel"]]
    return {"text": "Drawing through those gaps in order and submitting.",
            "route": [local(point, {"scale": 1.5, "offset": [10, 20]}) for point in route], "submit": True}
