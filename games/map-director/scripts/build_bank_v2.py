"""Freeze seeded, independently jittered maps and landmark-relative route gates."""

import json
import math
import random
from pathlib import Path

from a2a_engine.content import AssetStore
from map_director.maps import render_map
from map_director.maps_v2 import affine_residual

ROOT = Path(__file__).resolve().parents[1] / "map_director" / "items"
LANDMARKS = [
    ("START cabin", [15, 10]), ("FINISH chapel", [85, 90]),
    ("oak", [15, 35]), ("lake", [45, 35]),
    ("tower", [50, 55]), ("hill", [80, 55]),
    ("bridge", [25, 75]), ("windmill", [55, 75]),
]
PAIRS = [["oak", "lake"], ["tower", "hill"], ["bridge", "windmill"]]
ORACLE = "ordered-gates-corridor-v2"


def shifted(seed, radius):
    rng = random.Random(seed)
    landmarks, shifts = [], []
    for label, point in LANDMARKS:
        angle = rng.uniform(0, 2 * math.pi)
        distance = radius * math.sqrt(rng.random())
        delta = [distance * math.cos(angle), distance * math.sin(angle)]
        landmarks.append({"label": label, "at": [point[i] + delta[i] for i in range(2)]})
        shifts.append({"label": label, "delta": delta})
    return landmarks, shifts


def geometry(landmarks, pairs):
    points = {entry["label"]: entry["at"] for entry in landmarks}
    gates = []
    reference = [points["START cabin"]]
    for a, b in pairs:
        left, right = points[a], points[b]
        gate = [[left[i] + fraction * (right[i] - left[i]) for i in range(2)]
                for fraction in (0.25, 0.75)]
        gates.append(gate)
        reference.append([(left[i] + right[i]) / 2 for i in range(2)])
    reference.append(points["FINISH chapel"])
    return gates, reference


def build_bank():
    store = AssetStore(ROOT / "assets")
    rows = []
    for radius in (4, 8):
        for seed in (17, 41):
            tasks = []
            for round_index in range(2):
                giver_seed, follower_seed = seed * 100 + round_index * 2, seed * 100 + round_index * 2 + 1
                giver, giver_shifts = shifted(giver_seed, radius)
                follower, follower_shifts = shifted(follower_seed, radius)
                if affine_residual(giver, follower) <= 1e-6:
                    raise ValueError("Jitter accidentally admits an exact affine alignment")
                pairs = PAIRS if round_index == 0 else [PAIRS[1], PAIRS[0], PAIRS[2]]
                _, target = geometry(giver, pairs)
                gates, reference = geometry(follower, pairs)
                giver_frame = {"scale": 1.0, "offset": [0, 0]}
                follower_frame = {"scale": 1.5, "offset": [10, 20]}
                task = {
                    "item_id": f"jitter_{radius}_seed_{seed}_round_{round_index + 1}",
                    "task_version": "v2", "oracle_version": ORACLE,
                    "giver_landmarks": giver, "follower_landmarks": follower,
                    "giver_frame": giver_frame, "follower_frame": follower_frame,
                    "target_route": target, "reference_route": reference,
                    "ordered_gates": gates, "gate_pairs": pairs,
                    "tolerances": {"frechet": 8.0, "endpoint": 3.0, "length_ratio": 1.6, "gate_half_depth": 1.5},
                    "noise": {"distribution": "independent-uniform-disc", "radius": radius,
                              "giver_seed": giver_seed, "follower_seed": follower_seed,
                              "giver_shifts": giver_shifts, "follower_shifts": follower_shifts},
                    "oracle_result": {"route": reference, "oracle_version": ORACLE},
                }
                task["giver_image"] = store.put(render_map(giver, giver_frame, target, direction_arrows=True), "image/png").model_dump(exclude_none=True)
                task["follower_image"] = store.put(render_map(follower, follower_frame), "image/png").model_dump(exclude_none=True)
                tasks.append(task)
            rows.append({
                "item_id": f"jitter_{radius}_seed_{seed}",
                "params": {"discrepancy": "independent_jitter", "jitter_radius": radius, "noise_seed": seed},
                "tasks": tasks,
                "oracle_result": {"oracle_version": ORACLE, "round_routes": [t["reference_route"] for t in tasks]},
            })
    bank_path = ROOT / "maps_v2.jsonl"
    bank_path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    # Authoring updates the portable release pins as well as the bank.
    import hashlib
    import yaml
    from a2a_engine.items import ItemBank
    from map_director.declaration import DECLARATION_V2

    bank = ItemBank.load(bank_path)
    declaration = DECLARATION_V2.model_dump(mode="json", exclude_none=True)
    declaration["id"] = "map_director.tiny"
    declaration["item_policy"]["bank_path"] = "../map_director/items/maps_v2.jsonl"
    declaration["item_policy"]["item_bank_sha256"] = bank.item_bank_sha256
    declaration["inputs"] = [{"id": "maps", "path": "../map_director/items/maps_v2.jsonl",
                              "sha256": hashlib.sha256(bank_path.read_bytes()).hexdigest()}]
    declaration["inputs"] += [{"id": "asset." + image.sha256, "path": "../map_director/items/assets/" + image.sha256,
                               "sha256": image.sha256} for image in bank.assets]
    release_path = ROOT.parents[1] / "environments" / "map_director_tiny_v2.yaml"
    release_path.write_text(yaml.safe_dump(declaration, sort_keys=False))


if __name__ == "__main__":
    build_bank()
