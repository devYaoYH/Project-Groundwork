"""Evaluator-only ordered passages with a generous route corridor."""

from math import dist
from .maps import world, resample, frechet

ORACLE_VERSION = "ordered-gates-corridor-v2"


def affine_residual(giver, follower):
    """Maximum residual of the unique affine map fixed by three shared anchors.

    A fourth inconsistent shared point disproves *any* exact affine alignment.
    This diagnostic is bank-authoring/evaluator data, never a seat observation.
    """
    a, b, c = [entry["at"] for entry in giver[:3]]
    by_label = {entry["label"]: entry["at"] for entry in follower}
    fa, fb, fc = [by_label[entry["label"]] for entry in giver[:3]]
    bx, by = b[0] - a[0], b[1] - a[1]
    cx, cy = c[0] - a[0], c[1] - a[1]
    determinant = bx * cy - by * cx
    if abs(determinant) < 1e-9:
        raise ValueError("Affine diagnostic requires three noncollinear anchors")
    errors = []
    for entry in giver:
        x, y = entry["at"][0] - a[0], entry["at"][1] - a[1]
        u, v = (x * cy - y * cx) / determinant, (bx * y - by * x) / determinant
        predicted = [fa[i] + u * (fb[i] - fa[i]) + v * (fc[i] - fa[i]) for i in range(2)]
        errors.append(dist(predicted, by_label[entry["label"]]))
    return max(errors)


def _intersection_interval(a, b, c, d, half_depth):
    """Clip a route segment against a finite, oriented gate rectangle.

    A small fixed gate depth permits a route to turn inside a passage without
    needing to hit an infinitely thin line or an exact landmark midpoint.
    """
    gate_length = dist(c, d)
    if gate_length < 1e-9:
        raise ValueError("Route gates must have positive length")
    direction = [(d[i] - c[i]) / gate_length for i in range(2)]
    normal = [-direction[1], direction[0]]
    q = [a[i] - c[i] for i in range(2)]
    r = [b[i] - a[i] for i in range(2)]
    low, high = 0.0, 1.0
    for axis, minimum, maximum in ((direction, 0.0, gate_length), (normal, -half_depth, half_depth)):
        origin = sum(q[i] * axis[i] for i in range(2))
        delta = sum(r[i] * axis[i] for i in range(2))
        if abs(delta) < 1e-12:
            if origin < minimum - 1e-9 or origin > maximum + 1e-9:
                return None
        else:
            first, second = (minimum - origin) / delta, (maximum - origin) / delta
            low, high = max(low, min(first, second)), min(high, max(first, second))
            if low > high + 1e-9:
                return None
    return low, high


def ordered_passages(route, gates, half_depth):
    previous = -1.0
    for number, (c, d) in enumerate(gates):
        candidates = []
        for index, (a, b) in enumerate(zip(route, route[1:])):
            interval = _intersection_interval(a, b, c, d, half_depth)
            if interval is not None:
                low, high = index + interval[0], index + interval[1]
                progress = max(low, previous + 1e-8)
                if progress <= high + 1e-9:
                    candidates.append(progress)
        if not candidates:
            return number
        previous = min(candidates)
    return len(gates)


def score(route_local, item, *, submitted):
    reference = item["reference_route"]
    route = [world(point, item["follower_frame"]) for point in route_local]
    length = lambda points: sum(dist(a, b) for a, b in zip(points, points[1:]))
    ratio = length(route) / length(reference) if route else 0.0
    endpoint_error = max(dist(route[0], reference[0]), dist(route[-1], reference[-1])) if route else None
    tolerance = item["tolerances"]
    # Reject excessive length before the quadratic distance computation.
    error = frechet(resample(route), resample(reference)) if len(route) >= 2 and ratio <= tolerance["length_ratio"] else None
    passed = ordered_passages(route, item["ordered_gates"], tolerance["gate_half_depth"])
    complete = passed == len(item["ordered_gates"])
    return {
        "success": bool(submitted and complete and error is not None and error <= tolerance["frechet"]
                        and endpoint_error <= tolerance["endpoint"] and ratio <= tolerance["length_ratio"]),
        "route_error": error, "endpoint_error": endpoint_error, "length_ratio": ratio,
        "gates_passed": passed, "gate_count": len(item["ordered_gates"]), "gate_order_ok": complete,
        "submitted": submitted, "oracle_version": ORACLE_VERSION,
    }
