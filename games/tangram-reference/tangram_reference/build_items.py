"""Regenerate immutable example stimuli and six-round session bank."""

import json
from itertools import permutations
from pathlib import Path
from a2a_engine.content import AssetStore
from .stimuli import panel, polygons


def main():
    folder = Path(__file__).parent / "items"
    store = AssetStore(folder / "assets")
    orders = [
        [0, 2, 1, 3],
        [1, 3, 0, 2],
        [3, 1, 2, 0],
        [2, 0, 3, 1],
        [0, 3, 1, 2],
        [2, 1, 0, 3],
    ]
    # Frozen layouts are sampled during authoring, not hidden runtime stimuli.
    galleries = [
        [2, 0, 3, 1],
        [1, 2, 0, 3],
        [0, 3, 2, 1],
        [3, 2, 1, 0],
        [1, 0, 3, 2],
        [2, 3, 0, 1],
    ]
    rounds = []
    for r, (target, gallery) in enumerate(zip(orders, galleries, strict=True), 1):
        labels = [f"h{r}{chr(97 + i)}" for i in range(4)]
        rounds.append(
            {
                "target": target,
                "gallery": gallery,
                "handles": labels,
                "director_image": store.put(
                    panel(target, [1, 2, 3, 4]), "image/png"
                ).model_dump(exclude_none=True),
                "matcher_image": store.put(
                    panel(gallery, labels), "image/png"
                ).model_dump(exclude_none=True),
                "gallery_variants": [
                    {
                        "gallery": list(variant),
                        "matcher_image": store.put(
                            panel(list(variant), labels), "image/png"
                        ).model_dump(exclude_none=True),
                    }
                    for variant in permutations(range(4))
                ],
            }
        )
    row = {
        "item_id": "seven_piece_four_figures_v1",
        "params": {"inventory_size": 4},
        "geometry": [polygons(i) for i in range(4)],
        "rounds": rounds,
        "oracle_result": {"target_orders": orders, "exact_success": 1.0},
    }
    (folder / "sessions_v1.jsonl").write_text(
        json.dumps(row, separators=(",", ":")) + "\n"
    )


if __name__ == "__main__":
    main()
