"""Resolve an existing immutable science source tag before any runtime build."""
import argparse
import json
from pathlib import Path
import re
import subprocess

try:
    from .common import command
except ImportError:
    from common import command


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output-dir", default=".platform-build")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40,64}", args.source_sha):
        raise ValueError("Runtime source must be a complete commit SHA")
    if not args.image.endswith(":" + args.source_sha):
        raise ValueError("Runtime tag must match its immutable source SHA")
    target = Path(args.output_dir)
    target.mkdir(parents=True, exist_ok=True)
    check = ["artifacts", "docker", "images", "describe", args.image,
             "--project", args.project, "--format=json"]
    image = args.image
    try:
        raw = command(check, capture=True)
        reused = True
    except subprocess.CalledProcessError as error:
        if "NOT_FOUND" not in (error.stderr or ""):
            raise RuntimeError("Runtime source tag lookup failed; refusing to rebuild or overwrite it") from error
        reused = False
    if reused:
        info = json.loads(raw)
        digest = info.get("image_summary", info.get("imageSummary", {})).get("digest")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest or ""):
            raise RuntimeError("Existing runtime tag has no valid immutable digest")
        image = args.image.rsplit(":", 1)[0] + "@" + digest
        (target / "runtime-existing").touch()
    else:
        (target / "runtime-existing").unlink(missing_ok=True)
    (target / "runtime-image").write_text(image)
    print(json.dumps({"runtime_source_sha": args.source_sha, "runtime_image": image, "reused": reused}))


if __name__ == "__main__":
    main()
