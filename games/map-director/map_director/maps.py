"""North-up map rendering and evaluator-only geometric oracle."""

from io import BytesIO
from math import dist, ceil
from PIL import Image, ImageDraw, ImageFont

ORACLE_VERSION = "ordered-frechet-v1"


def local(point, frame):
    return [point[i] * frame["scale"] + frame["offset"][i] for i in range(2)]


def world(point, frame):
    return [(point[i] - frame["offset"][i]) / frame["scale"] for i in range(2)]


def render_map(landmarks, frame, route=None, *, draft=False, direction_arrows=False):
    image = Image.new("RGB", (800, 800), "#fffdf7")
    draw = ImageDraw.Draw(image)

    def font_at(size):
        from pathlib import Path

        for candidate in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
        ):
            if Path(candidate).exists():
                return ImageFont.truetype(candidate, size)
        return ImageFont.load_default(size=size)

    font = font_at(18)
    small = font_at(15)

    def pixel(point):
        return (70 + point[0] * 6.4, 730 - point[1] * 6.4)

    draw.text(
        (65, 18),
        "NORTH ↑     "
        + ("YOUR DRAWING" if draft else ("TARGET ROUTE" if route else "YOUR MAP")),
        fill="#182334",
        font=font,
    )
    for n in range(0, 101, 10):
        draw.line((70 + n * 6.4, 90, 70 + n * 6.4, 730), fill="#e0e3e5")
        draw.line((70, 730 - n * 6.4, 710, 730 - n * 6.4), fill="#e0e3e5")
        draw.text(
            (57 + n * 6.4, 740),
            str(round(local([n, 0], frame)[0], 2)),
            fill="#334155",
            font=small,
        )
        draw.text(
            (12, 721 - n * 6.4),
            str(round(local([0, n], frame)[1], 2)),
            fill="#334155",
            font=small,
        )
    draw.text(
        (330, 774), "x → east; y ↑ north (your local units)", fill="#334155", font=small
    )
    if route:
        points = [pixel(p) for p in route]
        draw.line(points, fill="#dc2626" if not draft else "#7c3aed", width=7)
        if direction_arrows:
            for a, b in zip(points, points[1:]):
                length = dist(a, b)
                if length < 20:
                    continue
                direction = [(b[i] - a[i]) / length for i in range(2)]
                normal = [-direction[1], direction[0]]
                center = [(a[i] + b[i]) / 2 for i in range(2)]
                tip = [center[i] + 10 * direction[i] for i in range(2)]
                back = [center[i] - 10 * direction[i] for i in range(2)]
                draw.polygon([tuple(tip), tuple(back[i] + 8 * normal[i] for i in range(2)),
                              tuple(back[i] - 8 * normal[i] for i in range(2))],
                             fill="#dc2626" if not draft else "#7c3aed")
        for p in points:
            draw.ellipse(
                (p[0] - 4, p[1] - 4, p[0] + 4, p[1] + 4),
                fill="#dc2626" if not draft else "#7c3aed",
            )
    for landmark in landmarks:
        x, y = pixel(landmark["at"])
        draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill="#0f766e")
        draw.text((x + 9, y - 24), landmark["label"], fill="#15283d", font=font)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def resample(route, step=1.0):
    if not route:
        return []
    output = [route[0]]
    for a, b in zip(route, route[1:]):
        count = max(1, ceil(dist(a, b) / step))
        output.extend(
            [
                [a[i] + (b[i] - a[i]) * j / count for i in range(2)]
                for j in range(1, count + 1)
            ]
        )
    return output


def frechet(a, b):
    previous = [float("inf")] * len(b)
    for i, p in enumerate(a):
        current = []
        for j, q in enumerate(b):
            d = dist(p, q)
            value = (
                d
                if i == j == 0
                else max(
                    d,
                    min(
                        previous[j],
                        current[j - 1] if j else float("inf"),
                        previous[j - 1] if j else float("inf"),
                    ),
                )
            )
            current.append(value)
        previous = current
    return previous[-1]


def score(route_local, item, *, submitted):
    target = item["target_route"]
    route = [world(p, item["follower_frame"]) for p in route_local]
    length = lambda pts: sum(dist(a, b) for a, b in zip(pts, pts[1:]))
    error = frechet(resample(route), resample(target)) if len(route) >= 2 else None
    endpoints = (
        max(dist(route[0], target[0]), dist(route[-1], target[-1])) if route else None
    )
    ratio = length(route) / length(target) if route else 0
    tolerance = item["tolerances"]
    success = bool(
        submitted
        and error is not None
        and error <= tolerance["frechet"]
        and endpoints <= tolerance["endpoint"]
        and ratio <= tolerance["length_ratio"]
    )
    return {
        "success": success,
        "route_error": error,
        "endpoint_error": endpoints,
        "length_ratio": ratio,
        "submitted": submitted,
        "oracle_version": ORACLE_VERSION,
    }
