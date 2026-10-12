"""Author deterministic, pinned synthetic stimuli (not original HCRC maps)."""

import json
from pathlib import Path
from a2a_engine.content import AssetStore
from map_director.maps import render_map, ORACLE_VERSION

ROOT = Path(__file__).resolve().parents[1] / "map_director" / "items"
store = AssetStore(ROOT / "assets")
landmarks = [
    {"label": label, "at": point}
    for label, point in [
        ("START cabin", [10, 10]),
        ("oak", [50, 10]),
        ("hill", [10, 50]),
        ("windmill", [50, 50]),
        ("lake", [90, 50]),
        ("bridge", [50, 90]),
        ("FINISH chapel", [90, 90]),
    ]
]
route_a = [[10, 10], [10, 50], [50, 50], [50, 90], [90, 90]]
route_b = [[10, 10], [50, 10], [50, 50], [50, 90], [90, 90]]
rows = []
for ident, discrepancy, route in [
    ("missing_renamed", "missing_renamed", route_a),
    ("duplicate", "duplicate", route_b),
    ("displaced", "displaced", route_a),
    ("matched", "none", route_a),
]:
    follower = json.loads(json.dumps(landmarks))
    if discrepancy == "missing_renamed":
        follower = [x for x in follower if x["label"] != "windmill"]
        next(x for x in follower if x["label"] == "FINISH chapel")["label"] = (
            "FINISH church"
        )
    if discrepancy == "duplicate":
        follower.append({"label": "bridge", "at": [20, 90]})
    if discrepancy == "displaced":
        next(x for x in follower if x["label"] == "bridge")["at"] = [60, 80]
    gf = {"scale": 1.0, "offset": [0, 0]}
    ff = {"scale": 1.5, "offset": [10, 20]}
    gi = store.put(render_map(landmarks, gf, route), "image/png").model_dump(
        exclude_none=True
    )
    fi = store.put(render_map(follower, ff), "image/png").model_dump(exclude_none=True)
    rows.append(
        {
            "item_id": ident,
            "params": {"discrepancy": discrepancy},
            "giver_image": gi,
            "follower_image": fi,
            "giver_landmarks": landmarks,
            "follower_landmarks": follower,
            "giver_frame": gf,
            "follower_frame": ff,
            "target_route": route,
            "tolerances": {"frechet": 3.0, "endpoint": 2.0, "length_ratio": 1.15},
            "oracle_result": {"route": route, "oracle_version": ORACLE_VERSION},
        }
    )
# A bank row is an entire repeated-dyad episode, with fixed ordered task rounds.
sessions = []
for ident, pair in [
    ("discrepant_pair", rows[:2]),
    ("displaced_control_pair", rows[2:]),
]:
    sessions.append(
        {
            "item_id": ident,
            "params": {
                "discrepancy": "+".join(x["params"]["discrepancy"] for x in pair)
            },
            "tasks": pair,
            "oracle_result": {
                "oracle_version": ORACLE_VERSION,
                "round_routes": [x["target_route"] for x in pair],
            },
        }
    )
(ROOT / "maps_v1.jsonl").write_text(
    "".join(json.dumps(x, sort_keys=True) + "\n" for x in sessions)
)
