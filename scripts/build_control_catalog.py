"""Stage published release data and static replay assets for the viewer image."""

import json
import shutil
from pathlib import Path

source = Path("/source-games")
destination = Path("/catalog")
for manifest in sorted(source.glob("*/runtime/release.manifest.json")):
    release = json.loads(manifest.read_text())
    relative = manifest.relative_to(source)
    target = destination / "games" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(manifest, target)
    policy = release["surface"].get("item_policy") or {}
    bank = policy.get("bank_path")
    if bank:
        bank_relative = Path(bank)
        if bank_relative.is_absolute() or ".." in bank_relative.parts or bank_relative.parts[0] != "games":
            raise ValueError(f"invalid published item bank path: {bank}")
        bank_source = source / Path(*bank_relative.parts[1:])
        bank_target = destination / bank_relative
        bank_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bank_source, bank_target)

for path in sorted(source.glob("*/experiments/*.yaml")):
    target = destination / "games" / path.relative_to(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, target)

for path in sorted(Path("/source-experiments").glob("*.yaml")):
    target = destination / "experiments" / path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, target)

static_extensions = {".html", ".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".woff", ".woff2"}
for path in source.rglob("*"):
    if not path.is_file() or path.suffix.lower() not in static_extensions:
        continue
    target = destination / "games" / path.relative_to(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, target)
